"""Tool-level OAuth declarations and scope challenges for remote MCP clients."""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel

from .oauth_auth import current_principal, resource_metadata_url


def _required_scope(tool: Tool) -> str:
    return "mcp:read" if tool.annotations and tool.annotations.read_only_hint else "mcp:write"


class ToolAuthMiddleware:
    """Use the SDK's public middleware hook to publish OAuth extensions on the wire.

    HTTP bearer validation still protects every request. A valid but narrower grant gets a
    tool-level challenge so clients such as ChatGPT can offer a scope upgrade.
    """

    def __init__(self, resource_url: str | None, list_tools: Callable[[], Awaitable[list[Tool]]]):
        self.metadata_url = resource_metadata_url(resource_url)
        self.list_tools = list_tools

    async def __call__(self, ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            tool = next((item for item in await self.list_tools() if item.name == name), None)
            if tool is not None:
                scope = _required_scope(tool)
                if scope not in current_principal().scopes:
                    challenge = (
                        f'Bearer resource_metadata="{self.metadata_url}", '
                        f'scope="{scope}", error="insufficient_scope", '
                        'error_description="Additional ShortsMaker permission is required"'
                    )
                    return CallToolResult(
                        content=[TextContent(text=f"This tool requires {scope} authorization.")],
                        isError=True,
                        _meta={"mcp/www_authenticate": [challenge]},
                    )
        result = await call_next(ctx)
        if ctx.method != "tools/list":
            return result
        document: dict[str, Any] = (
            result.model_dump(by_alias=True, mode="json", exclude_none=True)
            if isinstance(result, BaseModel)
            else dict(result or {})
        )
        for tool in document.get("tools", []):
            scope = (
                "mcp:read" if (tool.get("annotations") or {}).get("readOnlyHint") else "mcp:write"
            )
            schemes = [{"type": "oauth2", "scopes": [scope]}]
            tool["securitySchemes"] = schemes
            tool["_meta"] = {**tool.get("_meta", {}), "securitySchemes": schemes}
        return document
