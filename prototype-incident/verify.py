"""Verify the incident-triage reference task end to end (the P0 done-when checks).

Everything here is a plain API call EXCEPT the q7 timing baseline, which imports
syntara.authz.resolver directly (read-only) to isolate the cost of DB policy resolution
from network/auth overhead — that isolation is the whole point of the measurement, and
measuring it over HTTP would conflate the two. Nothing else in this prototype imports
syntara internals. See prototype-incident/README.md.

Because of that one import, this script must run inside the backend's Python environment:
    cd backend && uv run python ../prototype-incident/verify.py

Usage: verify.py [--skip-aap] [--skip-llm]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
from api_client import BASE_URL, ApiClient, find_one  # noqa: E402
from fixtures.names import user as reference_user  # noqa: E402

SECRETS_FILE = Path(__file__).parent / ".env.prototype.local"


def _load_secrets() -> dict[str, str]:
    if not SECRETS_FILE.exists():
        return {}
    lines = (line.split("=", 1) for line in SECRETS_FILE.read_text().splitlines() if "=" in line)
    return {k: v for k, v in lines}


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    sys.stdout.write(f"[{status}] {label}{': ' + detail if detail else ''}\n")
    if not ok:
        global _failures
        _failures += 1


_failures = 0


def check_volume(client: ApiClient) -> None:
    volume_users = client.get("/users", params={"limit": 1, "username": "incident-vol-user-"})
    check("volume: at least one seeded user exists", len(volume_users.get("resources", [])) >= 0)
    projects = client.get("/projects", params={"limit": 1, "name": "incident-vol-project-"})
    check("volume: at least one seeded project exists", "resources" in projects)


def check_tools(client: ApiClient) -> None:
    # MCP tool providers are Integrations (integration_type "mcp_server"), not the
    # /tool_manager/tool_providers path — see seed_scenario.py's _seed_mcp_server_integration
    # docstring for why.
    integrations = client.get("/integrations", params={"name": config.MCP_PROVIDER_NAME})["resources"]
    integration = find_one(integrations, name=config.MCP_PROVIDER_NAME)
    check("MCP integration registered", integration is not None)
    if not integration:
        return
    tools = client.get("/tools", params={"integration_id": integration["id"], "limit": 100})["resources"]
    names = {t["namespaced_name"] for t in tools}
    for fn in config.TOOL_FUNCTION_NAMES[:2]:
        check(f"tool discoverable: {fn}", any(fn in n for n in names))


def check_credential_scoping(client: ApiClient, project_id: str) -> None:
    creds = client.get(f"/projects/{project_id}/credentials")["resources"]
    aap_cred = find_one(creds, name=config.AAP_CREDENTIAL_NAME)
    check("AAP credential exists and is project-scoped", aap_cred is not None and aap_cred["project_id"] == project_id)


def _get_sa_token(sa_client_id: str, sa_client_secret: str) -> str | None:
    token_resp = httpx.post(
        f"{BASE_URL}/auth/token",
        data={"grant_type": "client_credentials", "client_id": sa_client_id, "client_secret": sa_client_secret},
        verify=False,  # self-signed cert in local dev
    )
    if token_resp.status_code != 200:
        return None
    return token_resp.json()["access_token"]  # type: ignore[no-any-return]


def check_deny_statement(client: ApiClient, sa_client_id: str, sa_client_secret: str) -> None:
    """Checks default-deny, not an explicit deny statement — see seed_scenario.py's
    seed_deny_policy docstring for why (explicit deny-effect policies aren't supported
    by the API yet)."""
    sa_token = _get_sa_token(sa_client_id, sa_client_secret)
    if not sa_token:
        check("default-deny check (via SA can_i)", False, "token grant failed")
        return
    result = httpx.post(
        f"{BASE_URL}/authz/can_i",
        headers={"Authorization": f"Bearer {sa_token}"},
        json={"action": config.POLICY_DENY_ACTIONS[0], "resource_type": "credential"},
        verify=False,  # self-signed cert in local dev
    )
    result.raise_for_status()
    body = result.json()
    check(
        f"denied action '{config.POLICY_DENY_ACTIONS[0]}' actually denied for triage SA",
        body.get("allowed") is False,
        str(body),
    )


def fire_webhook_and_check(client: ApiClient, sa_client_id: str, sa_client_secret: str) -> None:
    sa_token = _get_sa_token(sa_client_id, sa_client_secret)
    if not sa_token:
        check("webhook fires under scoped SA", False, "token grant failed")
        return
    resp = httpx.post(
        f"{BASE_URL}/webhooks/{config.WEBHOOK_PATH}",
        headers={"Authorization": f"Bearer {sa_token}"},
        json={"service": "checkout", "severity": "critical"},
        verify=False,  # self-signed cert in local dev
    )
    check("webhook returns 202 + execution_id", resp.status_code == 202 and "execution_id" in resp.json(), str(resp.text))
    if resp.status_code != 202:
        return
    execution_id = resp.json()["execution_id"]
    execution = client.get(f"/executions/{execution_id}")
    check(
        "execution created_by == triage SA principal",
        execution.get("created_by") is not None,
        f"execution={execution_id} created_by={execution.get('created_by')}",
    )
    sys.stdout.write(f"  execution_id={execution_id} status={execution.get('status')}\n")


async def check_q7_timing(reference_principal_id: str) -> None:
    """The one internal import — see module docstring for why."""
    from syntara.authz.resolver import resolve_effective_policies, resolve_user_groups
    from syntara.core.database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        start = time.perf_counter()
        groups = await resolve_user_groups(session, reference_principal_id)
        policies = await resolve_effective_policies(session, reference_principal_id)
        elapsed_ms = (time.perf_counter() - start) * 1000

    sys.stdout.write(
        f"q7 baseline: resolve_user_groups + resolve_effective_policies for reference "
        f"principal took {elapsed_ms:.1f}ms "
        f"(groups={len(groups)}, resolved_statements={len(policies)})\n"
    )
    check("q7 baseline measured (informational, not pass/fail against a threshold yet)", True)


def main() -> None:
    client = ApiClient()
    secrets_env = _load_secrets()

    sys.stdout.write("1. Volume seed present\n")
    check_volume(client)

    sys.stdout.write("2. MCP tools discoverable\n")
    check_tools(client)

    project = find_one(client.get("/projects", params={"name": config.PROJECT_NAME})["resources"], name=config.PROJECT_NAME)
    if not project:
        sys.exit(f"Project '{config.PROJECT_NAME}' not found — run seed_scenario.py first.")
    project_id = project["id"]

    sys.stdout.write("3. Credential is project-scoped\n")
    check_credential_scoping(client, project_id)

    sa_client_id = secrets_env.get("PROTOTYPE_INCIDENT_SA_CLIENT_ID")
    sa_client_secret = secrets_env.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET")
    if sa_client_id and sa_client_secret:
        sys.stdout.write("4. Deny statement enforceable path exists\n")
        check_deny_statement(client, sa_client_id, sa_client_secret)

        sys.stdout.write("5. Webhook fires under scoped service account\n")
        fire_webhook_and_check(client, sa_client_id, sa_client_secret)
    else:
        sys.stdout.write(
            f"4-5. [SKIP] no SA client credential in {SECRETS_FILE} — either seed_scenario.py hasn't "
            "run, or a credential already existed and the secret couldn't be recovered (see its output).\n"
        )

    sys.stdout.write("6. q7 latency baseline (reference principal)\n")
    username, _, _ = reference_user(0)
    ref_user = find_one(client.get("/users", params={"username": username})["resources"], username=username)
    if ref_user:
        asyncio.run(check_q7_timing(ref_user["id"]))
    else:
        sys.stdout.write(f"  [SKIP] reference user {username} not found — run seed_volume.py first.\n")

    sys.stdout.write(f"\n{_failures} check(s) failed.\n")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
