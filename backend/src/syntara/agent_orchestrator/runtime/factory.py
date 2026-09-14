"""Factory for selecting an `AgentRuntime` implementation.

The factory currently selects the in-process baseline or the hardened sandboxed
harness. Later engines (kagent, OpenClaw) plug into the same seam.
"""

from typing import Any, Literal

from syntara.agent_orchestrator.runtime.in_process import InProcessRuntime
from syntara.agent_orchestrator.runtime.protocol import AgentRuntime
from syntara.agent_orchestrator.runtime.sandboxed import SandboxedRuntime
from syntara.agent_orchestrator.services.orchestration_service import OrchestrationService

AgentRuntimeEngine = Literal["in_process", "sandboxed"]


def get_agent_runtime(
    orchestration_service: OrchestrationService,
    *,
    engine: AgentRuntimeEngine = "in_process",
    sandbox_options: dict[str, Any] | None = None,
) -> AgentRuntime:
    """Return the `AgentRuntime` for the given engine.

    Args:
        orchestration_service: The in-process orchestration service, already
            constructed with this invocation's resolved LLM/tool configuration.
        engine: Which engine to run behind the boundary.
        sandbox_options: Constructor arguments for `SandboxedRuntime`.

    """
    if engine == "sandboxed":
        if sandbox_options is None:
            msg = "sandbox_options are required for the sandboxed runtime"
            raise ValueError(msg)
        return SandboxedRuntime(orchestration_service, **sandbox_options)
    return InProcessRuntime(orchestration_service)
