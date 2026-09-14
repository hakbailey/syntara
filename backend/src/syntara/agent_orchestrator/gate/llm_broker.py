"""LLM completion brokering for the agent governance gate.

Supports both buffered calls and transparent SSE forwarding. The streaming
path is used by the sandboxed harness's OpenAI-compatible client.
"""

from collections.abc import AsyncGenerator
from typing import Any

import httpx


async def broker_llm_completion(
    *,
    base_url: str,
    api_key: str,
    request_body: dict[str, Any],
    insecure_skip_tls_verify: bool = False,
) -> dict[str, Any]:
    """Forward a chat-completion request to the real provider with the real key injected.

    The caller never sees `api_key` — it is resolved by the gate and injected
    here, so it never reaches the process that made this request.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        verify=not insecure_skip_tls_verify,
        timeout=httpx.Timeout(120.0),
    ) as client:
        response = await client.post("/chat/completions", json=request_body)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


async def stream_llm_completion(
    *,
    base_url: str,
    api_key: str,
    request_body: dict[str, Any],
    insecure_skip_tls_verify: bool = False,
) -> AsyncGenerator[bytes, None]:
    """Forward an OpenAI-compatible SSE response without buffering it."""
    async with (
        httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            verify=not insecure_skip_tls_verify,
            timeout=httpx.Timeout(300.0),
        ) as client,
        client.stream("POST", "/chat/completions", json=request_body) as response,
    ):
        response.raise_for_status()
        async for chunk in response.aiter_raw():
            yield chunk
