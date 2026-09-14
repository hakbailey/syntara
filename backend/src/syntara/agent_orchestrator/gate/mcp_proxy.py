"""MCP tool-call proxying for the agent governance gate.

Reuses `MCPProvider` (the same client the in-process loop uses today via
`ToolRetriever`) rather than reimplementing the MCP wire protocol. What
changes is *where* it runs: here, in the gate, with the bearer token
resolved and injected by the gate itself instead of by whatever is calling in.
"""

from typing import Any
from uuid import UUID

from syntara.tool_manager.exceptions import ToolNotFoundError
from syntara.tool_manager.lib.providers.mcp.mcp_provider import MCPProvider


async def call_mcp_tool(
    *,
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    bearer_token: str | None,
    integration_id: UUID,
    insecure_skip_tls_verify: bool = False,
    ca_certificate: str | None = None,
) -> Any:  # noqa: ANN401
    """Invoke one named tool on the real MCP server behind this integration.

    The caller never sees `bearer_token` — it is resolved by the gate and
    injected here, so it never reaches the process that made this request.

    Raises:
        ToolNotFoundError: If no tool with this name is exposed by the server.

    """
    provider = MCPProvider(
        base_url=base_url,
        api_key=bearer_token,
        integration_id=integration_id,
        insecure_skip_tls_verify=insecure_skip_tls_verify,
        ca_certificate=ca_certificate,
    )
    try:
        tools = await provider.get_base_tools()
        tool = next((t for t in tools if t.name == tool_name), None)
        if tool is None:
            msg = f"Tool '{tool_name}' not found on integration '{integration_id}'."
            raise ToolNotFoundError(msg)
        return await tool.ainvoke(arguments)
    finally:
        await provider.close()
