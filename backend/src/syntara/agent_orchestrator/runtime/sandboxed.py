"""Control-plane adapter for the isolated, HTTP-streaming agent harness."""

import json
from typing import Any
from uuid import UUID

import httpx
from langchain_core.tools import BaseTool

from syntara.agent_orchestrator.agents.generic_agent import _default_task_agent_prompt
from syntara.agent_orchestrator.agents.orchestrator_agent import OrchestratorAgent
from syntara.agent_orchestrator.harness.schemas import HarnessInvocationRequest, HarnessToolDefinition
from syntara.agent_orchestrator.models.agent_state import AgentStateFactory
from syntara.agent_orchestrator.models.context_data import InvocationContextData
from syntara.agent_orchestrator.services.error_handler import classify_streaming_error
from syntara.agent_orchestrator.services.orchestration_service import OrchestrationService
from syntara.agent_orchestrator.services.stream_publisher import stream_publisher
from syntara.agent_orchestrator.services.streaming_service import get_invocation_stream_id
from syntara.agent_orchestrator.utils.context_helpers import extract_request_id
from syntara.agent_orchestrator.utils.workflow_signal_client import WorkflowSignalClient
from syntara.audit.emitter import AuditActorContext
from syntara.core.cache.stream import StreamClient
from syntara.core.tls.http_client import build_internal_http_client
from syntara.settings.cache.settings_cache import get_runtime_settings


class SandboxedRuntime:
    """Prepare control-plane data, stream a harness run, and republish events."""

    def __init__(
        self,
        orchestration_service: OrchestrationService,
        *,
        harness_relay_url: str,
        gate_token: str,
        model_name: str,
        llm_integration_id: UUID,
        llm_credential_id: str,
        project_id: UUID,
        integration_credentials: dict[UUID, str],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Store above-boundary services and opaque credential references."""
        self._service = orchestration_service
        self._harness_relay_url = harness_relay_url.rstrip("/")
        self._gate_token = gate_token
        self._model_name = model_name
        self._llm_integration_id = llm_integration_id
        self._llm_credential_id = llm_credential_id
        self._project_id = project_id
        self._integration_credentials = integration_credentials
        self._http_client = http_client

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
        """Run one invocation below the boundary and preserve control-plane behavior."""
        stream_id = get_invocation_stream_id(invocation_id)
        request_id = extract_request_id(ctx)
        state = AgentStateFactory.create_initial_state(
            prompt=prompt,
            session_id=session_id,
            invocation_id=invocation_id,
            actor_context=actor_context,
            metadata=ctx.to_state_dict(),
            execution_id=execution_id,
            request_id=request_id,
            response_schema=response_schema,
        )
        prepared_state = await OrchestratorAgent(self._service.context_manager).execute(state)
        tools = await self._service._get_tools(  # noqa: SLF001 -- temporary A3 extraction seam
            session_id,
            invocation_id,
            execution_id,
            request_id,
            ctx.activity_id,
            ctx.activity_name,
        )
        system_prompt = await get_runtime_settings().get_str(
            "agentic.task_agent_system_prompt",
            default=_default_task_agent_prompt(),
        )
        request = HarnessInvocationRequest(
            prompt=prepared_state["prompt"],
            original_prompt=prepared_state["original_prompt"],
            system_prompt=system_prompt,
            session_id=session_id,
            invocation_id=invocation_id,
            execution_id=execution_id,
            request_id=request_id,
            response_schema=response_schema,
            context_package=prepared_state.get("context_package"),
            metadata=ctx.audit_safe_metadata(),
            model_name=self._model_name,
            llm_integration_id=self._llm_integration_id,
            llm_credential_id=self._llm_credential_id,
            tools=[self._serialize_tool(tool) for tool in tools],
        )

        owns_client = self._http_client is None
        # Use the internal service client for the API-side relay so enabled
        # S2S mTLS is applied to host-side and worker-side sandbox calls alike.
        client = self._http_client or build_internal_http_client(timeout=httpx.Timeout(300.0))
        result: dict[str, Any] | None = None
        async with StreamClient() as stream_client:
            try:
                async with client.stream(
                    "POST",
                    f"{self._harness_relay_url}/harness/invocations",
                    headers={"Authorization": f"Bearer {self._gate_token}"},
                    json=request.model_dump(mode="json"),
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        event = json.loads(line)
                        event_type = event.get("type")
                        data = event.get("data")
                        if event_type in {"delta", "tool_call", "tool_result"} and isinstance(data, dict):
                            await stream_publisher.publish(stream_client, stream_id, event_type, invocation_id, data)
                        elif event_type == "result" and isinstance(data, dict):
                            result = data
                        elif event_type == "error":
                            error_type = (
                                data.get("error_type", "HarnessError") if isinstance(data, dict) else "HarnessError"
                            )
                            msg = f"Sandboxed harness failed: {error_type}"
                            raise RuntimeError(msg)  # noqa: TRY301

                if result is None:
                    msg = "Sandboxed harness stream ended without a result"
                    raise RuntimeError(msg)  # noqa: TRY301
                result.setdefault("response_metadata", {})["stream_id"] = stream_id
                await stream_publisher.publish_completion(stream_client, stream_id, invocation_id)
                await self._service._handle_completion_callback(  # noqa: SLF001 -- control-plane adapter reuse
                    prepared_state,
                    invocation_id,
                    ctx,
                    signal_result=result,
                )
                return result
            except Exception as exc:
                error_data = classify_streaming_error(exc, invocation_id=invocation_id)
                await stream_publisher.publish(
                    stream_client,
                    stream_id,
                    "error",
                    invocation_id,
                    error_data.model_dump(exclude_none=True),
                )
                callback_url = ctx.callback_url.get_secret_value() if ctx.callback_url else None
                await WorkflowSignalClient.send_failure_signal(callback_url, invocation_id, exc)
                raise
            finally:
                if owns_client:
                    await client.aclose()

    def _serialize_tool(self, tool: BaseTool) -> HarnessToolDefinition:
        metadata = tool.metadata or {}
        integration_id = UUID(str(metadata["integration_id"]))
        args_schema = tool.args_schema
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            input_schema = args_schema.model_json_schema()
        elif isinstance(args_schema, dict):
            input_schema = args_schema
        else:
            input_schema = {"type": "object", "properties": {}}
        return HarnessToolDefinition(
            name=tool.name,
            description=tool.description,
            input_schema=input_schema,
            integration_id=integration_id,
            credential_id=self._integration_credentials.get(integration_id),
            # Agent-discovered MCP tools are read-only investigation tools. The
            # separate execution:run permission belongs to the post-approval
            # remediation HTTP node, not to the sandbox tool boundary.
            resource_type="tool",
            action="read",
            resource_project=str(self._project_id),
        )
