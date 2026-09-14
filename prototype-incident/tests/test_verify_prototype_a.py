"""Pure regression tests for the A4 operator verifier."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from verify_prototype_a import (  # noqa: E402
    WebhookEvidence,
    _as_uuid,
    _canonical_structured_content,
    _configured_secret_values,
    _gate_audit_events_from_logs,
    _has_triage_tool_trace,
    _invocation_id_from_activities,
    _structured_result_from_activities,
    check_approval_and_remediation,
)


INVOCATION_ID = "755897c8-3b7d-40b7-8bc9-60bbea1813b1"


def test_invocation_and_structured_result_are_extracted_from_agentic_activity() -> None:
    activities = {
        "resources": [
            {
                "activity_name": "triage",
                "node_type": "agentic",
                "output_data": {
                    "invocation_id": INVOCATION_ID,
                    "result": {"content": {"job_template_name": "remediate"}},
                },
            }
        ]
    }

    assert _invocation_id_from_activities(activities) == INVOCATION_ID
    assert _structured_result_from_activities(activities) == {
        "content": {"job_template_name": "remediate"}
    }


def test_triage_tool_trace_requires_a_real_configured_tool_call() -> None:
    activities = {
        "resources": [
            {
                "activity_name": "triage",
                "output_data": {
                    "result": {
                        "agent_trace": {
                            "steps": [
                                {"type": "reasoning", "content": "inspect"},
                                {
                                    "type": "tool_call",
                                    "tool_name": "get_incident_logs",
                                },
                            ]
                        }
                    }
                },
            }
        ]
    }

    assert _has_triage_tool_trace(activities)

    activities["resources"][0]["output_data"]["result"]["agent_trace"]["steps"][1][
        "tool_name"
    ] = "unconfigured_tool"
    assert not _has_triage_tool_trace(activities)


@pytest.mark.parametrize("value", [None, "not-a-uuid", 42])
def test_invalid_invocation_ids_are_ignored(value: object) -> None:
    assert _as_uuid(value) is None


def test_structured_parity_ignores_wrapper_metadata() -> None:
    webhook = {"content": {"summary": "same", "job_template_name": "remediate"}}
    standalone = {
        "content": {"job_template_name": "remediate", "summary": "same"},
        "trace": {"duration_ms": 123},
    }

    assert _canonical_structured_content(webhook) == _canonical_structured_content(
        standalone
    )


def test_configured_secret_values_are_deduplicated_and_not_printed() -> None:
    values = _configured_secret_values(
        {
            "PROTOTYPE_INCIDENT_SA_CLIENT_SECRET": "sa-secret",
        }
    )

    assert "sa-secret" in values
    assert values == sorted(set(values))


def test_gate_audit_log_parser_reads_structured_data_from_container_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = (
        "event_action=gate_tool_call actor_id=actor-1 actor_type=service_account "
        "actor_username=incident-triage structured_data="
        "{'data_type': 'gate_tool_call', 'invocation_id': '"
        f"{INVOCATION_ID}"
        "', 'decision': 'allowed', 'tool_name': 'get_incident_logs'} workflow_id=None"
    )
    unrelated = line.replace(INVOCATION_ID, "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=f"{unrelated}\n{line}\n", stderr=""
        ),
    )

    events = _gate_audit_events_from_logs(INVOCATION_ID)

    assert events == [
        {
            "event_action": "gate_tool_call",
            "actor_id": "actor-1",
            "actor_type": "service_account",
            "actor_username": "incident-triage",
            "structured_data": {
                "data_type": "gate_tool_call",
                "invocation_id": INVOCATION_ID,
                "decision": "allowed",
                "tool_name": "get_incident_logs",
            },
        }
    ]


class _ApprovalFailureClient:
    def __init__(self, execution_statuses: list[str], *, approval: bool = True) -> None:
        self.execution_statuses = iter(execution_statuses)
        self.approval = approval
        self.patches: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        if path.startswith("/executions/"):
            return {"status": next(self.execution_statuses)}
        if path == "/approvals":
            return {
                "resources": (
                    [
                        {
                            "id": "approval-1",
                            "execution_id": "execution-1",
                        }
                    ]
                    if self.approval
                    else []
                )
            }
        if path == "/workflows":
            return {"resources": [{"id": "remediation-workflow", "name": "remediation"}]}
        if path == "/executions":
            return {
                "resources": [
                    {
                        "id": "remediation-1",
                        "input_data": {"job_template_name": "Remediate"},
                    }
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, **kwargs: object) -> dict[str, object]:
        self.patches.append((path, kwargs["json"]))  # type: ignore[arg-type]
        return {}


def _failure_evidence() -> WebhookEvidence:
    return WebhookEvidence(
        execution_id="execution-1",
        invocation_id=INVOCATION_ID,
        structured_result={
            "content": {"job_template_name": "Remediate", "limit": ""}
        },
        activities={},
    )


def test_approval_path_reports_triage_failure_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("verify_prototype_a.time.sleep", lambda _: None)
    client = _ApprovalFailureClient(["paused", "failed"])

    assert not check_approval_and_remediation(client, _failure_evidence())
    assert client.patches == [
        (
            "/approvals/approval-1",
            {"status": "approved", "notes": "Prototype A automated verification"},
        )
    ]


def test_approval_path_reports_missing_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("verify_prototype_a.time.sleep", lambda _: None)
    client = _ApprovalFailureClient(["failed"], approval=False)

    assert not check_approval_and_remediation(client, _failure_evidence())
    assert client.patches == []


def test_remediation_path_reports_failed_downstream_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("verify_prototype_a.time.sleep", lambda _: None)
    client = _ApprovalFailureClient(["paused", "completed", "failed"])

    assert not check_approval_and_remediation(client, _failure_evidence())
