"""The `AgentRuntime` boundary between Syntara-owned control-plane code and the consumed engine.

Above the boundary (owned by Syntara, does not change when the engine is swapped):
invocation persistence, Redis stream publishing to WebSocket clients, the Temporal
completion callback, and credential resolution at rest (raw secrets are never held
by whatever sits below the boundary).

Below the boundary (the engine — swappable per `AgentRuntimeEngine`): the reasoning
loop itself and the tool/model calls it makes. `InProcessRuntime` is the baseline;
`SandboxedRuntime` runs the shared topology in the hardened harness. Later engines
(kagent, OpenClaw) implement this same protocol so callers never change.
"""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from syntara.agent_orchestrator.models.context_data import InvocationContextData
from syntara.audit.emitter import AuditActorContext


@runtime_checkable
class AgentRuntime(Protocol):
    """An engine capable of executing a single agent invocation.

    Mirrors `OrchestrationService.execute`'s signature exactly so that wrapping
    an engine behind this boundary is never itself a source of behavior change.
    """

    async def execute(
        self,
        prompt: str,
        session_id: str,
        invocation_id: UUID,
        actor_context: AuditActorContext,
        ctx: InvocationContextData,
        execution_id: UUID | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one agent invocation and return the result dict for DB storage."""
        ...
