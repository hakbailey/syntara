"""Audit events for the agent governance gate (MCP tool-call proxy + LLM broker)."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from syntara.agent_orchestrator.audit import extract_actor_fields
from syntara.audit.emitter import AuditActorContext
from syntara.audit.handler import AuditEventHandler
from syntara.audit.models.audit_event import AuditEvent, EventCategory, EventSeverity, EventStatus
from syntara.audit.models.structured_data import AuditContextData


class GateDecision(StrEnum):
    """Outcome of a request proxied through the agent governance gate."""

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


@dataclass
class GateToolCallEvent:
    """An agent tool call proxied through the governance gate's MCP proxy."""

    decision: GateDecision
    tool_name: str
    integration_id: UUID
    resource_type: str
    action: str
    actor_context: AuditActorContext | None = None
    denied_by: str | None = None
    error_type: str | None = None
    session_id: str | None = None
    invocation_id: UUID | None = None
    resource_project: str | None = None


class GateToolCallHandler(AuditEventHandler[GateToolCallEvent]):
    """Map GateToolCallEvent to a normalized AuditEvent."""

    def handle(self, event: GateToolCallEvent) -> AuditEvent:
        """Map GateToolCallEvent to AuditEvent."""
        actor_id, actor_username, actor_type = extract_actor_fields(event.actor_context)

        if event.decision == GateDecision.DENIED:
            severity, status_, message = (
                EventSeverity.WARNING,
                EventStatus.ERROR,
                f"Gate blocked tool call: {event.tool_name} ({event.resource_type}:{event.action})",
            )
        elif event.decision == GateDecision.ERROR:
            severity, status_, message = (
                EventSeverity.ERROR,
                EventStatus.ERROR,
                f"Gate tool call errored: {event.tool_name}",
            )
        else:
            severity, status_, message = (
                EventSeverity.INFO,
                EventStatus.SUCCESS,
                f"Gate allowed tool call: {event.tool_name} ({event.resource_type}:{event.action})",
            )

        structured_data = AuditContextData(
            data_type="gate_tool_call",
            error_type=event.error_type,
            decision=event.decision.value,
            tool_name=event.tool_name,
            integration_id=str(event.integration_id),
            resource_type=event.resource_type,
            action=event.action,
            denied_by=event.denied_by,
            session_id=event.session_id,
            invocation_id=str(event.invocation_id) if event.invocation_id else None,
            resource_project=event.resource_project,
        )

        return AuditEvent(
            event_category=EventCategory.AGENT_INTERACTION,
            event_severity=severity,
            event_status=status_,
            event_action="gate_tool_call",
            event_message=message,
            source_component="syntara.agent_orchestrator.gate",
            structured_data=structured_data,
            actor_id=actor_id,
            actor_username=actor_username,
            actor_type=actor_type,
            resource_urn=f"urn:syntara:integration:{event.integration_id}",
            resource_name=event.tool_name,
        )


@dataclass
class GateLLMCallEvent:
    """An LLM completion request brokered through the governance gate."""

    decision: GateDecision
    integration_id: UUID
    actor_context: AuditActorContext | None = None
    error_type: str | None = None
    session_id: str | None = None
    invocation_id: UUID | None = None


class GateLLMCallHandler(AuditEventHandler[GateLLMCallEvent]):
    """Map GateLLMCallEvent to a normalized AuditEvent."""

    def handle(self, event: GateLLMCallEvent) -> AuditEvent:
        """Map GateLLMCallEvent to AuditEvent."""
        actor_id, actor_username, actor_type = extract_actor_fields(event.actor_context)

        if event.decision == GateDecision.ERROR:
            severity, status_, message = (
                EventSeverity.ERROR,
                EventStatus.ERROR,
                "Gate LLM completion errored",
            )
        else:
            severity, status_, message = (
                EventSeverity.INFO,
                EventStatus.SUCCESS,
                "Gate brokered LLM completion",
            )

        structured_data = AuditContextData(
            data_type="gate_llm_call",
            error_type=event.error_type,
            decision=event.decision.value,
            integration_id=str(event.integration_id),
            session_id=event.session_id,
            invocation_id=str(event.invocation_id) if event.invocation_id else None,
        )

        return AuditEvent(
            event_category=EventCategory.AGENT_INTERACTION,
            event_severity=severity,
            event_status=status_,
            event_action="gate_llm_call",
            event_message=message,
            source_component="syntara.agent_orchestrator.gate",
            structured_data=structured_data,
            actor_id=actor_id,
            actor_username=actor_username,
            actor_type=actor_type,
            resource_urn=f"urn:syntara:integration:{event.integration_id}",
        )
