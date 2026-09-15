"""A4 verification for the prototype's real sandbox and gate path.

This is intentionally separate from ``verify.py``'s seed/P0 checks.  It checks
the properties that cannot be inferred from the Syntara workflow result: the
harness has only its private network, the seccomp predicates are the intended
ones, the escape probes are denied, and gate allow/deny requests are emitted
with the expected decision in the API container's audit log.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
from api_client import BASE_URL, ApiClient, find_one  # noqa: E402  # type: ignore[import-untyped]
from verify import _get_sa_token, _load_secrets  # noqa: E402  # type: ignore[import-untyped]

ROOT = Path(__file__).parents[1]
RuntimeEngine = Literal["in_process", "sandboxed"]
SECCOMP_CHECK = ROOT / "backend/containers/agent-sandbox/verify_seccomp.py"
WEBHOOK_TRACE_POLL_ATTEMPTS = 150
AUDIT_LOG_POLL_ATTEMPTS = 180
AUDIT_LOG_POLL_INTERVAL = 2.0
# Keep each Podman read bounded; scanning the full ten-minute API log on every
# poll can take longer than the audit worker needs to publish the event.
AUDIT_LOG_LOOKBACK = "2m"


def _run(
    label: str,
    command: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"== {label}: {' '.join(command)}")
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
        env=env,
        input=input_text,
    )


@dataclass(frozen=True)
class WebhookEvidence:
    """Evidence collected from one webhook-triggered triage run."""

    execution_id: str
    invocation_id: str
    structured_result: dict[str, Any]
    activities: dict[str, Any]


def _resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a paginated API response's resource list."""
    resources = payload.get("resources", [])
    return resources if isinstance(resources, list) else []


def _as_uuid(value: object) -> str | None:
    """Return a canonical UUID string, or ``None`` for unrelated values."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _triage_activity(activities: dict[str, Any]) -> dict[str, Any] | None:
    """Find the agentic triage activity in an activities response."""
    for activity in _resources(activities):
        if not isinstance(activity, dict):
            continue
        if (
            activity.get("activity_name") == "triage"
            or activity.get("node_type") == "agentic"
        ):
            return activity
    return None


def _invocation_id_from_activities(activities: dict[str, Any]) -> str | None:
    """Extract the deferred agent invocation ID from triage output data."""
    activity = _triage_activity(activities)
    output_data = activity.get("output_data") if activity else None
    if not isinstance(output_data, dict):
        return None
    for key in ("invocation_id", "id"):
        if invocation_id := _as_uuid(output_data.get(key)):
            return invocation_id
    return None


def _structured_result_from_activities(
    activities: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract the agent result payload persisted on the triage activity."""
    activity = _triage_activity(activities)
    output_data = activity.get("output_data") if activity else None
    if not isinstance(output_data, dict):
        return None
    result = output_data.get("result")
    return result if isinstance(result, dict) else None


def _has_triage_tool_trace(activities: dict[str, Any]) -> bool:
    """Return whether the persisted triage result contains a real tool call."""
    result = _structured_result_from_activities(activities)
    if not result:
        return False
    trace = result.get("agent_trace")
    steps = trace.get("steps") if isinstance(trace, dict) else None
    return isinstance(steps, list) and any(
        isinstance(step, dict)
        and step.get("type") == "tool_call"
        and isinstance(step.get("tool_name"), str)
        and step["tool_name"] in config.TOOL_FUNCTION_NAMES
        for step in steps
    )


def _activity_snapshot(activities: dict[str, Any]) -> str:
    """Summarize activity state without including potentially sensitive output."""
    return (
        ", ".join(
            f"{activity.get('activity_name')}:{activity.get('status')}"
            for activity in _resources(activities)
            if isinstance(activity, dict)
        )
        or "none"
    )


def _canonical_structured_content(value: object) -> str:
    """Canonicalize the stable structured recommendation for parity checks."""
    if isinstance(value, dict) and isinstance(value.get("content"), dict):
        value = value["content"]
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _configured_secret_values(secrets: dict[str, str]) -> list[str]:
    """Return configured secret values without ever printing them."""
    values = {
        config.LLM_API_KEY,
        config.AAP_OAUTH_TOKEN,
        config.AAP_PASSWORD,
        config.AAP_MCP_TOKEN,
        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET"),
    }
    return sorted(value for value in values if value)


def check_seccomp() -> bool:
    result = _run("seccomp predicate validation", [sys.executable, str(SECCOMP_CHECK)])
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode == 0


def check_harness(
    container: str, *, skip_attack: bool, secrets: dict[str, str]
) -> bool:
    inspect = _run("harness container inspect", ["podman", "inspect", container])
    if inspect.returncode:
        print(inspect.stderr, file=sys.stderr, end="")
        return False
    data = json.loads(inspect.stdout)[0]
    networks = set(data.get("NetworkSettings", {}).get("Networks", {}))
    env = "\n".join(data.get("Config", {}).get("Env", []))
    mounts = data.get("Mounts", [])
    ok = True
    if networks != {"syntara_agent-sandbox"} and networks != {"agent-sandbox"}:
        print(f"[FAIL] harness networks are not private-only: {sorted(networks)}")
        ok = False
    else:
        print(f"[PASS] harness network boundary: {sorted(networks)}")
    leaked = [
        line.split("=", 1)[0]
        for line in env.splitlines()
        if any(
            secret in line.upper()
            for secret in ("API_KEY", "SECRET", "PASSWORD", "TOKEN")
        )
    ]
    if leaked:
        print(f"[FAIL] secret-like harness environment variables: {leaked}")
        ok = False
    else:
        print("[PASS] harness environment contains no secret-like variables")
    allowed_mounts = {
        "/run/trust/agent-gate-ca.pem",
        "/opt/agent-sandbox/attack_suite.py",
    }
    non_public = [
        mount.get("Destination")
        for mount in mounts
        if mount.get("Destination") not in allowed_mounts
    ]
    if non_public:
        print(f"[FAIL] unexpected harness mounts: {non_public}")
        ok = False
    else:
        print("[PASS] harness mounts only the public gate CA and read-only A4 probe")
    secret_values = _configured_secret_values(secrets)
    filesystem_probe = (
        "import json, os, sys; "
        "markers=json.load(sys.stdin); leaks=[]; "
        "roots=('/opt/app-root','/tmp','/run','/etc'); "
        "\nfor root in roots:\n"
        "  for directory, dirs, files in os.walk(root):\n"
        "    dirs[:]=[d for d in dirs if d not in ('proc','sys','dev','__pycache__')]\n"
        "    for name in files:\n"
        "      path=os.path.join(directory,name)\n"
        "      try:\n"
        "        if os.path.getsize(path)>2_000_000: continue\n"
        "        data=open(path,'rb').read()\n"
        "      except (OSError, ValueError): continue\n"
        "      if any(marker.encode() in data for marker in markers): leaks.append(path)\n"
        "print(json.dumps(leaks))"
    )
    probe = _run(
        "harness filesystem secret scan",
        ["podman", "exec", "-i", container, "python", "-c", filesystem_probe],
        input_text=json.dumps(secret_values),
    )
    try:
        filesystem_leaks = json.loads(probe.stdout.strip() or "[]")
    except json.JSONDecodeError:
        filesystem_leaks = ["<probe returned invalid output>"]
    if probe.returncode or filesystem_leaks:
        print(
            f"[FAIL] configured secret values present in harness filesystem: {filesystem_leaks}"
        )
        if probe.stderr:
            print(probe.stderr, file=sys.stderr, end="")
        ok = False
    else:
        print("[PASS] configured secret values absent from harness filesystem")
    if not skip_attack:
        attack = _run(
            "containment attack suite",
            [
                "podman",
                "exec",
                "--user",
                "1001:0",
                container,
                "python",
                "/opt/agent-sandbox/attack_suite.py",
            ],
        )
        print(attack.stdout, end="")
        if attack.returncode:
            print(attack.stderr, file=sys.stderr, end="")
            ok = False
    return ok


def check_gate(client: ApiClient, secrets: dict[str, str]) -> bool:
    project = find_one(
        client.get("/projects", params={"name": config.PROJECT_NAME})["resources"],
        name=config.PROJECT_NAME,
    )
    integration = find_one(
        client.get("/integrations", params={"name": config.MCP_PROVIDER_NAME})[
            "resources"
        ],
        name=config.MCP_PROVIDER_NAME,
    )
    if not project or not integration:
        print("[FAIL] seeded project or MCP integration is missing")
        return False
    token = _get_sa_token(
        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_ID", ""),
        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET", ""),
    )
    if not token:
        print("[FAIL] could not mint triage service-account token")
        return False
    headers = {"Authorization": f"Bearer {token}"}
    invocation_id = str(uuid4())
    body = {
        "arguments": {"service": "checkout"},
        "resource_type": "tool",
        "action": "read",
        "resource_project": project["id"],
        "session_id": "a4-verification",
        "invocation_id": invocation_id,
    }
    print(f"gate verification invocation_id={invocation_id}")
    allowed = client._http.post(  # noqa: SLF001 - verifier intentionally uses one raw request
        f"{BASE_URL}/agent-gate/mcp/{integration['id']}/tools/{quote(config.TOOL_FUNCTION_NAMES[0], safe='')}/call",
        headers=headers,
        json=body,
    )
    denied_body = {**body, "resource_type": "credential", "action": "read"}
    denied = client._http.post(
        f"{BASE_URL}/agent-gate/mcp/{integration['id']}/tools/{quote(config.TOOL_FUNCTION_NAMES[0], safe='')}/call",
        headers=headers,
        json=denied_body,
    )
    ok = allowed.status_code < 300 and denied.status_code == 403
    print(
        f"[{'PASS' if allowed.status_code < 300 else 'FAIL'}] gate allow decision: HTTP {allowed.status_code}"
    )
    print(
        f"[{'PASS' if denied.status_code == 403 else 'FAIL'}] gate deny decision: HTTP {denied.status_code}"
    )
    decisions = _gate_audit_decisions(invocation_id)
    audit_ok = decisions >= {"allowed", "denied"}
    print(
        f"[{'PASS' if audit_ok else 'FAIL'}] gate audit decisions: {sorted(decisions)}"
    )
    if not ok:
        print(f"allow={allowed.text}\ndeny={denied.text}", file=sys.stderr)
    return ok and audit_ok


def check_webhook_reference(
    client: ApiClient, secrets: dict[str, str]
) -> WebhookEvidence | None:
    """Trigger the real reference workflow and collect correlated triage evidence."""
    token = _get_sa_token(
        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_ID", ""),
        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET", ""),
    )
    if not token:
        print("[FAIL] could not mint triage service-account token for webhook")
        return None
    response = client._http.post(  # noqa: SLF001 - verifier drives the external webhook
        f"{BASE_URL}/webhooks/{config.WEBHOOK_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        json={"service": "checkout", "severity": "critical"},
    )
    if response.status_code != 202:
        print(
            f"[FAIL] reference webhook returned HTTP {response.status_code}: {response.text}"
        )
        return None
    execution_id = response.json().get("execution_id")
    if not execution_id:
        print("[FAIL] reference webhook response did not include execution_id")
        return None
    print(f"reference execution_id={execution_id}")

    activities: dict[str, Any] = {}
    execution: dict[str, Any] = {}
    for _ in range(WEBHOOK_TRACE_POLL_ATTEMPTS):
        execution = client.get(f"/executions/{execution_id}")
        # Query the triage activity directly. The default activity list is
        # paginated/sorted for the UI and can briefly omit the completed agent
        # activity while the parent execution is transitioning to approval.
        activities = client.get(
            f"/executions/{execution_id}/activities",
            params={"activity_name": "triage", "limit": 100},
        )
        triage_trace_seen = _has_triage_tool_trace(activities)
        if triage_trace_seen:
            encoded = json.dumps(
                {"execution": execution, "activities": activities}, default=str
            )
            secrets_found = [
                name
                for name, value in (
                    ("LLM API key", config.LLM_API_KEY),
                    ("AAP token", config.AAP_OAUTH_TOKEN),
                    ("AAP password", config.AAP_PASSWORD),
                    ("AAP MCP token", config.AAP_MCP_TOKEN),
                    (
                        "service-account client secret",
                        secrets.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET"),
                    ),
                )
                if value and value in encoded
            ]
            if secrets_found:
                print(
                    f"[FAIL] secrets present in execution/activity history: {secrets_found}"
                )
                return None
            invocation_id = _invocation_id_from_activities(activities)
            structured_result = _structured_result_from_activities(activities)
            if not invocation_id or structured_result is None:
                continue
            integrations = client.get(
                "/integrations", params={"name": config.MCP_PROVIDER_NAME}
            )
            mcp_integration = find_one(
                _resources(integrations), name=config.MCP_PROVIDER_NAME
            )
            if not mcp_integration:
                print("[FAIL] seeded MCP integration is missing")
                return None
            gate_events = _gate_audit_events(
                invocation_id, required_actions={"gate_tool_call", "gate_llm_call"}
            )
            allowed_tools = [
                event
                for event in gate_events
                if event.get("event_action") == "gate_tool_call"
                and isinstance(event.get("structured_data"), dict)
                and event["structured_data"].get("decision") == "allowed"
            ]
            brokered_llm = [
                event
                for event in gate_events
                if event.get("event_action") == "gate_llm_call"
            ]
            audit_ok = bool(allowed_tools and brokered_llm)
            context_ok = any(
                event.get("actor_type") == "service_account"
                and event.get("actor_id")
                and event["structured_data"].get("tool_name")
                in config.TOOL_FUNCTION_NAMES
                and event["structured_data"].get("integration_id")
                == str(mcp_integration["id"])
                and event["structured_data"].get("resource_project")
                == config.PROJECT_NAME
                and event["structured_data"].get("resource_type") == "tool"
                and event["structured_data"].get("action") == "read"
                and event["structured_data"].get("session_id")
                and event["structured_data"].get("invocation_id") == invocation_id
                for event in allowed_tools
            )
            print(
                f"[{'PASS' if audit_ok else 'FAIL'}] real webhook gate audit correlation: "
                f"tools={len(allowed_tools)} llm={len(brokered_llm)} invocation_id={invocation_id}"
            )
            print(
                f"[{'PASS' if context_ok else 'FAIL'}] real webhook gate audit context: "
                "service-account actor, tool, integration, project, session"
            )
            if not audit_ok or not context_ok:
                return None
            print("[PASS] real webhook triage activity and tool trace observed")
            print(
                "[PASS] configured LLM/AAP secret values absent from execution/activity history"
            )
            return WebhookEvidence(
                execution_id=str(execution_id),
                invocation_id=invocation_id,
                structured_result=structured_result,
                activities=activities,
            )
        if execution.get("status") in {"failed", "cancelled", "completed"}:
            break
        time.sleep(2)
    print(
        f"[FAIL] webhook execution did not expose a triage tool trace "
        f"(status={execution.get('status')}, activities={_activity_snapshot(activities)})"
    )
    return None


def _gate_audit_events(
    invocation_id: str,
    *,
    required_actions: set[str] | None = None,
    required_decisions: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Read emitted gate events from the API container's audit log.

    The transactional outbox is only a staging buffer: the audit worker
    publishes rows to stdout/OTEL and then deletes them.  Reading that table
    makes an end-to-end check race the worker, so this verifier uses the
    operator-visible audit log as its source of truth.
    """
    for _ in range(AUDIT_LOG_POLL_ATTEMPTS):
        events = _gate_audit_events_from_logs(invocation_id)
        actions = {str(event.get("event_action")) for event in events}
        decisions = {
            structured["decision"]
            for event in events
            if isinstance((structured := event.get("structured_data")), dict)
            and isinstance(structured.get("decision"), str)
        }
        if (
            events
            and (required_actions is None or required_actions <= actions)
            and (required_decisions is None or required_decisions <= decisions)
        ):
            return events
        time.sleep(AUDIT_LOG_POLL_INTERVAL)
    return _gate_audit_events_from_logs(invocation_id)


def _gate_audit_events_from_logs(invocation_id: str) -> list[dict[str, Any]]:
    """Recover correlated gate events after the transactional outbox drains."""
    result = subprocess.run(
        ["podman", "logs", "--since", AUDIT_LOG_LOOKBACK, "syntara_syntara_1"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        return []
    events: list[dict[str, Any]] = []
    # Podman may route the container's stderr-backed application logger to
    # ``podman logs`` stderr.  Audit evidence can therefore be in either
    # captured stream.
    for line in (result.stdout + result.stderr).splitlines():
        if invocation_id not in line:
            continue

        if "event_action=gate_" not in line:
            continue
        action_match = re.search(r"event_action=([^\s]+)", line)
        structured_text = line.partition(" structured_data=")[2].rsplit(
            " workflow_id=", 1
        )[0]
        if not action_match or not structured_text:
            continue
        try:
            structured = ast.literal_eval(structured_text)
        except (SyntaxError, ValueError):
            continue
        if (
            not isinstance(structured, dict)
            or structured.get("invocation_id") != invocation_id
        ):
            continue
        event: dict[str, Any] = {
            "event_action": action_match.group(1),
            "structured_data": structured,
        }
        for field in ("actor_id", "actor_type", "actor_username"):
            field_match = re.search(rf"\b{field}=([^\s]+)", line)
            if field_match:
                event[field] = field_match.group(1)
        events.append(event)
    return events


def _gate_audit_decisions(invocation_id: str) -> set[str]:
    """Read tool-call gate decisions for an invocation."""
    events = _gate_audit_events(
        invocation_id,
        required_actions={"gate_tool_call"},
        required_decisions={"allowed", "denied"},
    )
    return {
        structured["decision"]
        for event in events
        if event.get("event_action") == "gate_tool_call"
        and isinstance((structured := event.get("structured_data")), dict)
        and isinstance(structured.get("decision"), str)
    }


def _run_standalone_runtime_case(
    evidence: WebhookEvidence,
    *,
    label: str,
    runtime_engine: RuntimeEngine | None,
    deployment_engine: RuntimeEngine,
    expect_sandbox_gate: bool,
) -> bool:
    """Run one real callback-free invocation and verify its runtime-specific evidence."""
    with tempfile.TemporaryDirectory(prefix="prototype-a4-") as directory:
        expected_path = Path(directory) / "webhook-result.json"
        expected_path.write_text(json.dumps(evidence.structured_result, default=str))
        environment = os.environ.copy()
        environment["APP_AGENT_RUNTIME_ENGINE"] = deployment_engine
        environment["APP_AGENT_GATE_BASE_URL"] = f"{BASE_URL}/agent-gate"
        environment["APP_AGENT_ORCHESTRATOR_BASE_URL"] = BASE_URL
        # Tool discovery reads its own base URL setting.  The compose worker
        # receives both URLs; the host-side parity subprocess must receive the
        # same API endpoint instead of falling back to http://localhost:8000.
        environment["APP_TOOL_MANAGER_BASE_URL"] = BASE_URL
        dev_certs = ROOT / "backend/.secrets/certs"
        environment.update(
            {
                "APP_S2S_TLS_ENABLED": "true",
                "APP_S2S_TLS_CA_CERT_PATH": str(dev_certs / "ca.pem"),
                "APP_S2S_TLS_CERT_PATH": str(dev_certs / "worker.crt"),
                "APP_S2S_TLS_KEY_PATH": str(dev_certs / "worker.key"),
                "APP_S2S_TLS_CN_ALLOWLIST": json.dumps(
                    [
                        "backend.ao.svc",
                        "worker.ao.svc",
                        "background-worker.ao.svc",
                        "temporal.ao.svc",
                    ]
                ),
            }
        )
        command = [
            sys.executable,
            str(ROOT / "prototype-incident/run_standalone.py"),
            "--invocation-id",
            evidence.invocation_id,
        ]
        if runtime_engine is None:
            command.append("--deployment-default")
        else:
            command.extend(["--runtime-engine", runtime_engine])
        command.extend(["--compare-to", str(expected_path)])
        result = _run(label, command, env=environment)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr, end="")
            return False
        expected_selection = (
            "deployment_default" if runtime_engine is None else runtime_engine
        )
        if f"runtime_selection={expected_selection}" not in result.stdout:
            print(f"[FAIL] {label} reported the wrong runtime selection")
            return False
        standalone_invocation_id = next(
            (
                line.split("=", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.startswith("standalone_invocation_id=")
            ),
            None,
        )
        if not standalone_invocation_id or not _as_uuid(standalone_invocation_id):
            print(f"[FAIL] {label} did not report its cloned invocation ID")
            return False
        if expect_sandbox_gate:
            standalone_events = _gate_audit_events(
                standalone_invocation_id,
                required_actions={"gate_tool_call", "gate_llm_call"},
            )
            gate_ok = any(
                event.get("event_action") == "gate_tool_call"
                and isinstance(event.get("structured_data"), dict)
                and event["structured_data"].get("decision") == "allowed"
                for event in standalone_events
            ) and any(
                event.get("event_action") == "gate_llm_call"
                for event in standalone_events
            )
            print(
                f"[{'PASS' if gate_ok else 'FAIL'}] {label} gate audit correlation: "
                f"invocation_id={standalone_invocation_id}"
            )
            return gate_ok

        standalone_events = _gate_audit_events_from_logs(standalone_invocation_id)
        gate_events = [
            event
            for event in standalone_events
            if str(event.get("event_action", "")).startswith("gate_")
        ]
        no_gate_ok = not gate_events
        print(
            f"[{'PASS' if no_gate_ok else 'FAIL'}] {label} bypassed sandbox gate: "
            f"gate_events={len(gate_events)}"
        )
        return no_gate_ok


def check_standalone_parity(evidence: WebhookEvidence) -> bool:
    """Exercise explicit runtime overrides and both deployment-default engines."""
    cases = (
        {
            "label": "standalone explicit sandboxed runtime",
            "runtime_engine": "sandboxed",
            "deployment_engine": "in_process",
            "expect_sandbox_gate": True,
        },
        {
            "label": "standalone explicit in-process runtime",
            "runtime_engine": "in_process",
            "deployment_engine": "sandboxed",
            "expect_sandbox_gate": False,
        },
        {
            "label": "standalone sandboxed deployment default",
            "runtime_engine": None,
            "deployment_engine": "sandboxed",
            "expect_sandbox_gate": True,
        },
        {
            "label": "standalone in-process deployment default",
            "runtime_engine": None,
            "deployment_engine": "in_process",
            "expect_sandbox_gate": False,
        },
    )
    results = [_run_standalone_runtime_case(evidence, **case) for case in cases]
    return all(results)


def check_approval_and_remediation(
    client: ApiClient, evidence: WebhookEvidence
) -> bool:
    """Approve the real triage run and verify its downstream remediation execution."""
    approval: dict[str, Any] | None = None
    execution: dict[str, Any] = {}
    for _ in range(60):
        execution = client.get(f"/executions/{evidence.execution_id}")
        approvals = client.get(
            "/approvals",
            params={"status": "pending", "execution_id": evidence.execution_id},
        )
        resources = [
            resource
            for resource in _resources(approvals)
            if resource.get("execution_id") == evidence.execution_id
        ]
        if resources:
            approval = resources[0]
            break
        if execution.get("status") in {"failed", "cancelled", "completed"}:
            break
        time.sleep(2)

    if not approval:
        print(
            f"[FAIL] pending approval not found for execution {evidence.execution_id} "
            f"(status={execution.get('status')})"
        )
        return False

    client.patch(
        f"/approvals/{approval['id']}",
        json={"status": "approved", "notes": "Prototype A automated verification"},
    )
    print(f"[PASS] approval accepted: {approval['id']}")

    triage_status = ""
    for _ in range(90):
        execution = client.get(f"/executions/{evidence.execution_id}")
        triage_status = str(execution.get("status", ""))
        if triage_status in {"failed", "cancelled"}:
            print(f"[FAIL] triage execution ended {triage_status} after approval")
            return False
        if triage_status == "completed":
            break
        time.sleep(2)
    else:
        print(
            f"[FAIL] triage execution did not complete after approval: {triage_status}"
        )
        return False

    workflows = client.get(
        "/workflows", params={"name": config.REMEDIATION_WORKFLOW_NAME}
    )
    remediation_workflow = find_one(
        _resources(workflows), name=config.REMEDIATION_WORKFLOW_NAME
    )
    if not remediation_workflow:
        print(
            f"[FAIL] remediation workflow not found: {config.REMEDIATION_WORKFLOW_NAME}"
        )
        return False

    expected_content = evidence.structured_result.get("content", {})
    expected_template = (
        expected_content.get("job_template_name")
        if isinstance(expected_content, dict)
        else None
    )
    if not expected_template:
        print("[FAIL] triage recommendation did not include job_template_name")
        return False
    remediation_execution: dict[str, Any] | None = None
    for _ in range(90):
        executions = client.get(
            "/executions",
            params={"workflow_id": remediation_workflow["id"], "limit": 50},
        )
        candidates = _resources(executions)
        for candidate in candidates:
            input_data = candidate.get("input_data") or {}
            if input_data.get("job_template_name") == expected_template:
                remediation_execution = candidate
                break
        if remediation_execution:
            break
        time.sleep(2)

    if not remediation_execution:
        print(
            "[FAIL] remediation execution was not created with the triage recommendation"
        )
        return False
    remediation_id = remediation_execution["id"]
    for _ in range(180):
        remediation_execution = client.get(f"/executions/{remediation_id}")
        status = str(remediation_execution.get("status", ""))
        if status in {"failed", "cancelled"}:
            print(f"[FAIL] remediation execution ended {status}: {remediation_id}")
            return False
        if status == "completed":
            print(f"[PASS] remediation execution completed: {remediation_id}")
            return True
        time.sleep(2)
    print(f"[FAIL] remediation execution did not complete: {remediation_id}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container",
        default=os.environ.get("AGENT_HARNESS_CONTAINER", "syntara_agent-harness_1"),
    )
    parser.add_argument("--skip-attack", action="store_true")
    parser.add_argument("--skip-harness", action="store_true")
    parser.add_argument("--skip-gate", action="store_true")
    parser.add_argument("--skip-webhook", action="store_true")
    parser.add_argument(
        "--skip-standalone",
        action="store_true",
        help="Skip callback-free standalone parity (explicit environment limitation)",
    )
    parser.add_argument(
        "--skip-remediation",
        action="store_true",
        help="Skip approval/remediation (explicit external-dependency limitation)",
    )
    args = parser.parse_args()
    secrets = _load_secrets()
    checks = [check_seccomp()]
    if not args.skip_harness:
        checks.append(
            check_harness(args.container, skip_attack=args.skip_attack, secrets=secrets)
        )
    if not args.skip_gate:
        checks.append(check_gate(ApiClient(), secrets))
    if not args.skip_webhook:
        evidence = check_webhook_reference(ApiClient(), secrets)
        checks.append(evidence is not None)
        if evidence is not None:
            if args.skip_standalone:
                print("[SKIP] standalone parity (explicit --skip-standalone)")
            else:
                checks.append(check_standalone_parity(evidence))
            if args.skip_remediation:
                print("[SKIP] approval/remediation (explicit --skip-remediation)")
            else:
                checks.append(check_approval_and_remediation(ApiClient(), evidence))
    failed = len(checks) - sum(checks)
    print(f"\nA4: {failed} check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
