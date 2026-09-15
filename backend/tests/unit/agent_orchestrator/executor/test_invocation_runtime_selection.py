"""Tests for runtime selection and sandbox gate prerequisites."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr

from syntara.agent_orchestrator.exceptions import LLMConfigurationError
from syntara.agent_orchestrator.executor.invocation_executor import InvocationExecutor
from syntara.agent_orchestrator.models.context_data import InvocationContextData, InvocationMetadata
from syntara.audit.emitter import AuditActorContext
from syntara.core.models.principal import PrincipalType
from syntara.service_accounts.models.service_account import ServiceAccountStatus
from syntara.service_accounts.models.service_account_credential import ServiceAccountCredentialStatus

RuntimeEngine = Literal["in_process", "sandboxed"]


def _make_executor(session: MagicMock | None = None) -> tuple[InvocationExecutor, MagicMock]:
    """Build an executor with a controllable async session context."""
    session = session or MagicMock()

    @asynccontextmanager
    async def session_context() -> AsyncGenerator[MagicMock, None]:
        yield session

    executor = InvocationExecutor.__new__(InvocationExecutor)
    executor.get_async_session_context = session_context
    executor.session_factory = session_context  # type: ignore[assignment]
    return executor, session


def _context(runtime_engine: RuntimeEngine | None) -> InvocationContextData:
    """Build invocation context with the fields required by LLM initialization."""
    return InvocationContextData(
        runtime_engine=runtime_engine,
        metadata=InvocationMetadata(
            credential_id=SecretStr("credential-1"),
            llm_model_id=str(uuid4()),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_runtime_engine", "deployment_runtime_engine", "expected_runtime_engine"),
    [
        ("in_process", "sandboxed", "in_process"),
        ("sandboxed", "in_process", "sandboxed"),
        (None, "in_process", "in_process"),
        (None, "sandboxed", "sandboxed"),
    ],
)
async def test_init_orchestration_selects_context_or_deployment_runtime(
    context_runtime_engine: RuntimeEngine | None,
    deployment_runtime_engine: RuntimeEngine,
    expected_runtime_engine: RuntimeEngine,
) -> None:
    """Explicit invocation context wins over the deployment default."""
    executor, _ = _make_executor()
    invocation = MagicMock(id=uuid4(), project_id=uuid4())
    settings = MagicMock(
        agent_runtime_engine=deployment_runtime_engine,
        agent_gate_base_url="https://gate.example/api/v1/agent-gate",
    )
    llm = MagicMock(model_name="test-model", openai_api_base="https://llm.example/v1")
    runtime = object()

    with (
        patch("syntara.agent_orchestrator.executor.invocation_executor.get_settings", return_value=settings),
        patch.object(executor, "_validate_credentials_eagerly", new=AsyncMock()),
        patch.object(
            executor,
            "_resolve_llm_model_and_integration",
            new=AsyncMock(return_value=("test-model", "https://llm.example/v1", "custom", False, None)),
        ),
        patch.object(executor, "_resolve_llm_api_key", new=AsyncMock(return_value="api-key")),
        patch.object(
            executor,
            "_get_actor_context_for_invocation",
            new=AsyncMock(
                return_value=AuditActorContext(
                    actor_id=uuid4(),
                    actor_type=PrincipalType.SERVICE_ACCOUNT,
                )
            ),
        ),
        patch.object(executor, "_issue_gate_token", new=AsyncMock(return_value="gate-token")) as issue_gate_token,
        patch.object(executor, "_resolve_llm_integration_id", new=AsyncMock(return_value=uuid4())),
        patch(
            "syntara.agent_orchestrator.executor.invocation_executor.get_openrouter_llm",
            new=AsyncMock(return_value=(llm, None)),
        ),
        patch("syntara.agent_orchestrator.executor.invocation_executor.ContextManagerPlanner"),
        patch("syntara.agent_orchestrator.executor.invocation_executor.OrchestrationService"),
        patch(
            "syntara.agent_orchestrator.executor.invocation_executor.get_agent_runtime",
            return_value=runtime,
        ) as get_agent_runtime,
    ):
        result = await executor._init_orchestration(invocation, _context(context_runtime_engine))

    assert result is not None
    assert result[0] is runtime
    assert get_agent_runtime.call_args is not None
    assert get_agent_runtime.call_args.kwargs["engine"] == expected_runtime_engine
    if expected_runtime_engine == "sandboxed":
        issue_gate_token.assert_awaited_once()
    else:
        issue_gate_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_sandboxed_runtime_rejects_human_actor() -> None:
    """Sandboxed initialization cannot mint a gate token for a human actor."""
    executor, _ = _make_executor()
    actor = AuditActorContext(actor_id=uuid4(), actor_type=PrincipalType.USER)

    with pytest.raises(LLMConfigurationError, match="requires a service-account invocation"):
        await executor._issue_gate_token(actor)


@pytest.mark.asyncio
async def test_sandboxed_runtime_issues_gate_token_for_active_service_account() -> None:
    """An active service account with a usable credential can authorize the gate."""
    service_account_id = uuid4()
    credential_id = uuid4()
    service_account = MagicMock(
        id=service_account_id,
        name="incident-triage-trigger",
        status=ServiceAccountStatus.ACTIVE,
        token_version=3,
    )
    credential = MagicMock(
        id=credential_id,
        status=ServiceAccountCredentialStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    query_result = MagicMock()
    query_result.all.return_value = [credential]
    session = MagicMock()
    session.get = AsyncMock(return_value=service_account)
    session.exec = AsyncMock(return_value=query_result)
    executor, _ = _make_executor(session)
    actor = AuditActorContext(
        actor_id=service_account_id,
        actor_username=service_account.name,
        actor_type=PrincipalType.SERVICE_ACCOUNT,
    )

    with patch(
        "syntara.agent_orchestrator.executor.invocation_executor.TokenService.create_access_token",
        return_value="gate-token",
    ) as create_access_token:
        token = await executor._issue_gate_token(actor)

    assert token == "gate-token"  # noqa: S105 - test token
    create_access_token.assert_called_once_with(
        subject_id=service_account_id,
        username=service_account.name,
        token_version=3,
        credential_id=credential_id,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_account_status", "credentials", "message"),
    [
        (ServiceAccountStatus.DISABLED, [], "unavailable or disabled"),
        (ServiceAccountStatus.ACTIVE, [], "no active credential"),
    ],
)
async def test_sandboxed_runtime_rejects_unusable_service_account(
    service_account_status: ServiceAccountStatus,
    credentials: list[object],
    message: str,
) -> None:
    """Disabled accounts and accounts without active credentials fail closed."""
    service_account_id = uuid4()
    service_account = MagicMock(
        id=service_account_id,
        name="incident-triage-trigger",
        status=service_account_status,
    )
    query_result = MagicMock()
    query_result.all.return_value = credentials
    session = MagicMock()
    session.get = AsyncMock(return_value=service_account)
    session.exec = AsyncMock(return_value=query_result)
    executor, _ = _make_executor(session)
    actor = AuditActorContext(actor_id=service_account_id, actor_type=PrincipalType.SERVICE_ACCOUNT)

    with pytest.raises(LLMConfigurationError, match=message):
        await executor._issue_gate_token(actor)
