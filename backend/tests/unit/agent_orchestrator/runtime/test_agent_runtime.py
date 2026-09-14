"""Unit tests for the AgentRuntime boundary: InProcessRuntime and the factory."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from syntara.agent_orchestrator.models import InvocationContextData
from syntara.agent_orchestrator.runtime import AgentRuntime, InProcessRuntime, SandboxedRuntime, get_agent_runtime
from syntara.audit.emitter import AuditActorContext


class TestInProcessRuntime:
    """InProcessRuntime must forward execute() to the wrapped OrchestrationService unchanged."""

    @pytest.mark.asyncio
    async def test_execute_forwards_all_kwargs_and_returns_result(self) -> None:
        mock_service = AsyncMock()
        mock_service.execute.return_value = {"content": "done"}
        runtime = InProcessRuntime(mock_service)

        invocation_id = uuid4()
        execution_id = uuid4()
        actor_context = AuditActorContext()
        ctx = InvocationContextData()
        response_schema: dict[str, Any] = {"type": "object"}

        result = await runtime.execute(
            prompt="hello",
            session_id="session-1",
            invocation_id=invocation_id,
            actor_context=actor_context,
            ctx=ctx,
            execution_id=execution_id,
            response_schema=response_schema,
        )

        assert result == {"content": "done"}
        mock_service.execute.assert_awaited_once_with(
            prompt="hello",
            session_id="session-1",
            invocation_id=invocation_id,
            actor_context=actor_context,
            ctx=ctx,
            execution_id=execution_id,
            response_schema=response_schema,
        )

    def test_satisfies_agent_runtime_protocol(self) -> None:
        runtime = InProcessRuntime(AsyncMock())
        assert isinstance(runtime, AgentRuntime)


class TestGetAgentRuntime:
    """get_agent_runtime() is the only place an engine gets selected."""

    def test_returns_in_process_runtime_wrapping_the_given_service(self) -> None:
        mock_service = AsyncMock()
        runtime = get_agent_runtime(mock_service)

        assert isinstance(runtime, InProcessRuntime)
        assert runtime._orchestration_service is mock_service

    def test_returns_sandboxed_runtime_with_explicit_options(self) -> None:
        mock_service = AsyncMock()
        runtime = get_agent_runtime(
            mock_service,
            engine="sandboxed",
            sandbox_options={
                "harness_relay_url": "https://syntara/api/v1/agent-gate",
                "gate_token": "scoped-token",
                "model_name": "test/model",
                "llm_integration_id": uuid4(),
                "llm_credential_id": str(uuid4()),
                "project_id": uuid4(),
                "integration_credentials": {},
            },
        )

        assert isinstance(runtime, SandboxedRuntime)

    def test_sandboxed_runtime_requires_options(self) -> None:
        with pytest.raises(ValueError, match="sandbox_options"):
            get_agent_runtime(AsyncMock(), engine="sandboxed")
