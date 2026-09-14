"""HTTP entry point for the network- and filesystem-confined agent harness."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from syntara.agent_orchestrator.harness.config import harness_settings
from syntara.agent_orchestrator.harness.schemas import HarnessInvocationRequest, HarnessToolDefinition
from syntara.agent_orchestrator.models.agent_state import AgentStateFactory
from syntara.agent_orchestrator.services.agent_loop import AgentLoop
from syntara.audit.emitter import AuditActorContext

app = FastAPI(title="Syntara Agent Harness", docs_url=None, redoc_url=None, openapi_url=None)

_END = object()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Return process liveness without touching an external dependency."""
    return {"status": "ok"}


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=harness_settings.gate_ca_path,
        timeout=harness_settings.request_timeout_seconds,
    )


def _gate_llm(request: HarnessInvocationRequest, token: str, client: httpx.AsyncClient) -> ChatOpenAI:
    base_url = f"{harness_settings.gate_base_url}/llm/{request.llm_integration_id}/openai/v1"
    return ChatOpenAI(
        model=request.model_name,
        api_key=SecretStr(token),
        base_url=base_url,
        stream_usage=True,
        default_headers={
            "X-Syntara-Credential-ID": request.llm_credential_id,
            "X-Syntara-Session-ID": request.session_id,
            "X-Syntara-Invocation-ID": str(request.invocation_id),
        },
        http_async_client=client,
    )


def _gate_tool(
    definition: HarnessToolDefinition,
    request: HarnessInvocationRequest,
    token: str,
    client: httpx.AsyncClient,
) -> BaseTool:
    async def call_tool(**arguments: Any) -> Any:  # noqa: ANN401
        url = (
            f"{harness_settings.gate_base_url}/mcp/{definition.integration_id}/tools/"
            f"{quote(definition.name, safe='')}/call"
        )
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "arguments": arguments,
                "credential_id": definition.credential_id,
                "resource_type": definition.resource_type,
                "action": definition.action,
                "resource_project": definition.resource_project,
                "session_id": request.session_id,
                "invocation_id": str(request.invocation_id),
            },
        )
        response.raise_for_status()
        return response.json()

    return StructuredTool.from_function(
        coroutine=call_tool,
        name=definition.name,
        description=definition.description or definition.name,
        args_schema=definition.input_schema,
    )


async def _run_invocation(
    request: HarnessInvocationRequest,
    token: str,
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, Any]:
    async with _http_client() as client:
        llm = _gate_llm(request, token, client)
        tools = [_gate_tool(definition, request, token, client) for definition in request.tools]
        state = AgentStateFactory.create_initial_state(
            prompt=request.original_prompt,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            actor_context=AuditActorContext(),
            metadata=request.metadata,
            execution_id=request.execution_id,
            request_id=request.request_id,
            response_schema=request.response_schema,
        )
        state["prompt"] = request.prompt
        state["context_package"] = request.context_package
        state["current_agent"] = "generic_agent"
        return await AgentLoop(llm, tools, request.system_prompt).execute(state, emit)


@app.post("/invocations")
async def invoke(
    request: HarnessInvocationRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Run one invocation and return progress/result records as NDJSON."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")

    async def records() -> AsyncGenerator[bytes, None]:
        queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def run() -> None:
            try:
                result = await _run_invocation(request, token, emit)
                await queue.put({"type": "result", "data": result})
            except Exception as exc:  # noqa: BLE001
                await queue.put(
                    {
                        "type": "error",
                        "data": {"error_type": type(exc).__name__, "message": "Harness execution failed"},
                    }
                )
            finally:
                await queue.put(_END)

        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not _END:
                yield f"{json.dumps(item, separators=(',', ':'))}\n".encode()
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(records(), media_type="application/x-ndjson")


def main() -> None:
    """Run the harness ASGI server."""
    import uvicorn  # noqa: PLC0415

    uvicorn.run(app, host="0.0.0.0", port=8090)  # noqa: S104


if __name__ == "__main__":
    main()
