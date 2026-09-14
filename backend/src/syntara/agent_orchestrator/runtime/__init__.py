"""The `AgentRuntime` boundary: engine selection for agent invocations."""

from syntara.agent_orchestrator.runtime.factory import get_agent_runtime
from syntara.agent_orchestrator.runtime.in_process import InProcessRuntime
from syntara.agent_orchestrator.runtime.protocol import AgentRuntime
from syntara.agent_orchestrator.runtime.sandboxed import SandboxedRuntime

__all__ = [
    "AgentRuntime",
    "InProcessRuntime",
    "SandboxedRuntime",
    "get_agent_runtime",
]
