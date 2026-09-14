"""Unit tests for the governance-gate audit event shape."""

from uuid import uuid4

from syntara.agent_orchestrator.audit.gate import (
    GateDecision,
    GateToolCallEvent,
    GateToolCallHandler,
)
from syntara.audit.emitter import AuditActorContext
from syntara.core.models.principal import PrincipalType


def test_gate_tool_event_preserves_project_and_invocation_context() -> None:
    invocation_id = uuid4()
    integration_id = uuid4()
    actor_id = uuid4()
    event = GateToolCallEvent(
        decision=GateDecision.ALLOWED,
        tool_name="get_incident_logs",
        integration_id=integration_id,
        resource_type="tool",
        action="read",
        actor_context=AuditActorContext(
            actor_id=actor_id,
            actor_username="incident-triage-trigger",
            actor_type=PrincipalType.SERVICE_ACCOUNT,
        ),
        session_id="a4-verification",
        invocation_id=invocation_id,
        resource_project="incident-response",
    )

    audit_event = GateToolCallHandler().handle(event)
    structured_data = audit_event.structured_data.model_dump()

    assert audit_event.actor_id == actor_id
    assert audit_event.actor_type == PrincipalType.SERVICE_ACCOUNT
    assert structured_data["tool_name"] == "get_incident_logs"
    assert structured_data["integration_id"] == str(integration_id)
    assert structured_data["resource_type"] == "tool"
    assert structured_data["action"] == "read"
    assert structured_data["resource_project"] == "incident-response"
    assert structured_data["session_id"] == "a4-verification"
    assert structured_data["invocation_id"] == str(invocation_id)
