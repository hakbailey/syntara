"""In-process `AgentRuntime`: wraps `OrchestrationService` unchanged.

This is the unsandboxed baseline. It runs in the same process/container as the
caller (today: the `temporal-worker`), with no isolation and no gate in front of
its tool/model calls. It exists as a behavior-preserving fallback and comparison
point once a sandboxed engine is introduced — not as a target end state.
"""

from typing import Any
from uuid import UUID

from syntara.agent_orchestrator.models.context_data import InvocationContextData
from syntara.agent_orchestrator.services.orchestration_service import OrchestrationService
from syntara.audit.emitter import AuditActorContext


class InProcessRuntime:
    """Forwards `execute` to an already-constructed `OrchestrationService`."""

    def __init__(self, orchestration_service: OrchestrationService) -> None:
        """Wrap an already-constructed `OrchestrationService`."""
        self._orchestration_service = orchestration_service

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
        """Delegate to the wrapped `OrchestrationService.execute`."""
        return await self._orchestration_service.execute(
            prompt=prompt,
            session_id=session_id,
            invocation_id=invocation_id,
            actor_context=actor_context,
            ctx=ctx,
            execution_id=execution_id,
            response_schema=response_schema,
        )
