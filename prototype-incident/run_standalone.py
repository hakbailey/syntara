"""Run a copy of a prototype invocation through the sandbox runtime directly.

This deliberately bypasses Temporal as the *trigger*.  It calls the same
``InvocationExecutor`` used by the worker, so ``APP_AGENT_RUNTIME_ENGINE``
selects the same ``SandboxedRuntime.execute`` path and the same gate token and
credential-reference handling. The source invocation must already contain the
seeded triage context (the easiest source is a completed webhook run). The source
is copied with workflow callback fields removed, so this check cannot resume or
signal the original workflow.

Examples::

    uv run --project ../backend python run_standalone.py --invocation-id <uuid> \
      --result-out /tmp/triage-standalone.json

    uv run --project ../backend python run_standalone.py --invocation-id <uuid> \
      --compare-to /tmp/triage-webhook.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

BACKEND_SRC = Path(__file__).parents[1] / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))
RuntimeEngine = Literal["in_process", "sandboxed"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invocation-id", required=True, type=UUID)
    runtime_selection = parser.add_mutually_exclusive_group()
    runtime_selection.add_argument(
        "--runtime-engine",
        choices=("in_process", "sandboxed"),
        help="Override the invocation runtime context for this parity run.",
    )
    runtime_selection.add_argument(
        "--deployment-default",
        action="store_true",
        help="Remove any invocation runtime override and use APP_AGENT_RUNTIME_ENGINE.",
    )
    parser.add_argument(
        "--result-out",
        type=Path,
        help="Write the structured result for later parity checks",
    )
    parser.add_argument(
        "--compare-to", type=Path, help="Compare the structured result with a prior run"
    )
    return parser


def _apply_runtime_selection(
    context_data: dict[str, object],
    *,
    runtime_engine: RuntimeEngine | None,
    deployment_default: bool,
) -> None:
    """Apply an explicit runtime override or restore deployment-default behavior."""
    if runtime_engine is not None:
        context_data["runtime_engine"] = runtime_engine
    elif deployment_default:
        context_data.pop("runtime_engine", None)


async def _load_result(invocation_id: UUID) -> dict[str, object]:
    from syntara.agent_orchestrator.models.invocation import Invocation  # type: ignore[import-untyped]
    from syntara.core.database.session import get_db  # type: ignore[import-untyped]

    async with contextlib.asynccontextmanager(get_db)() as session:
        invocation = await session.get(Invocation, invocation_id)
    if invocation is None:
        raise RuntimeError(f"Invocation {invocation_id} was not found")
    if invocation.result is None:
        raise RuntimeError(f"Invocation {invocation_id} completed without a result")
    return dict(invocation.result)


async def _clone_invocation(
    source_id: UUID,
    *,
    runtime_engine: RuntimeEngine | None,
    deployment_default: bool,
) -> UUID:
    from syntara.agent_orchestrator.models.invocation import Invocation
    from syntara.core.database.session import get_db

    async with contextlib.asynccontextmanager(get_db)() as session:
        source = await session.get(Invocation, source_id)
        if source is None:
            raise RuntimeError(f"Invocation {source_id} was not found")
        context_data = dict(source.context_data)
        for key in (
            "callback_url",
            "workflow_id",
            "activity_id",
            "activity_name",
            "execution_id",
        ):
            context_data.pop(key, None)
        _apply_runtime_selection(
            context_data,
            runtime_engine=runtime_engine,
            deployment_default=deployment_default,
        )
        clone = Invocation(
            created_by=source.created_by,
            project_id=source.project_id,
            prompt=source.prompt,
            session_id=f"standalone-{source.session_id}",
            context_data=context_data,
            model_name=source.model_name,
        )
        session.add(clone)
        await session.commit()
        return cast(UUID, clone.id)


async def _run(
    source_id: UUID,
    *,
    runtime_engine: RuntimeEngine | None,
    deployment_default: bool,
) -> tuple[UUID, dict[str, object]]:
    from syntara.agent_orchestrator.executor.invocation_executor import (  # type: ignore[import-untyped]
        InvocationExecutor,
    )
    from syntara.core.database.session import AsyncSessionLocal
    from syntara.settings.cache.settings_cache import (
        SettingsCache,
        set_runtime_settings,
    )

    # Worker startup normally installs this process-wide cache.  The direct
    # parity process bypasses worker lifecycle, so initialise a DB-backed,
    # non-Redis cache before the executor resolves runtime settings.
    set_runtime_settings(
        SettingsCache(session_factory=AsyncSessionLocal, redis_enabled=False)
    )
    invocation_id = await _clone_invocation(
        source_id,
        runtime_engine=runtime_engine,
        deployment_default=deployment_default,
    )
    await InvocationExecutor().execute_invocation(invocation_id)
    return invocation_id, await _load_result(invocation_id)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _structured_content(value: object) -> object:
    """Return the stable structured triage payload, excluding trace metadata."""
    if isinstance(value, dict) and isinstance(value.get("content"), dict):
        return value["content"]
    return value


def _parity_contract(value: object) -> object:
    """Return the deterministic remediation contract used for parity.

    Independent LLM calls can choose different investigative tools and phrase
    the narrative differently.  The fields that drive the approved
    remediation workflow must still match exactly.
    """
    content = _structured_content(value)
    if not isinstance(content, dict):
        return content
    return {
        field: content.get(field)
        for field in ("job_template_id", "job_template_name", "limit")
    }


def _configure_local_signing_keys() -> None:
    """Use the development signing keys when running outside the containers."""
    secrets_dir = Path(__file__).parents[1] / "backend" / ".secrets"
    os.environ.setdefault(
        "APP_JWT_PRIVATE_KEY_PATH", str(secrets_dir / "jwt-primary.pem")
    )
    os.environ.setdefault(
        "APP_JWT_BACKUP_KEYS",
        json.dumps(
            [
                {
                    "key_id": "orchestrator-backup",
                    "key_path": str(secrets_dir / "jwt-backup.pem"),
                }
            ]
        ),
    )
    os.environ.setdefault(
        "APP_SECRET_ENCRYPTION_KEY_PATH", str(secrets_dir / "encryption-key")
    )


def main() -> int:
    args = _parser().parse_args()
    # The host process does not inherit the Temporal worker's compose env.  Make
    # the parity command safe by selecting the A4 engine unless the operator
    # explicitly supplied another value for comparison.
    os.environ.setdefault("APP_AGENT_RUNTIME_ENGINE", "sandboxed")
    _configure_local_signing_keys()
    standalone_id, result = asyncio.run(
        _run(
            args.invocation_id,
            runtime_engine=args.runtime_engine,
            deployment_default=args.deployment_default,
        )
    )
    if args.deployment_default:
        print("runtime_selection=deployment_default")
    elif args.runtime_engine:
        print(f"runtime_selection={args.runtime_engine}")
    else:
        print("runtime_selection=source_context")
    print(f"standalone_invocation_id={standalone_id}")
    encoded = json.dumps(result, indent=2, sort_keys=True, default=str)
    print(encoded)
    if args.result_out:
        args.result_out.write_text(encoded + "\n")
    if args.compare_to:
        expected = json.loads(args.compare_to.read_text())
        if _canonical(_parity_contract(result)) != _canonical(
            _parity_contract(expected)
        ):
            print(
                "[FAIL] standalone result differs from the webhook result",
                file=sys.stderr,
            )
            return 1
        print("[PASS] standalone result matches the webhook result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
