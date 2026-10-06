"""ShortsMaker MCP tools and user-controlled workflow prompts, grouped by domain."""

from mcp.server import MCPServer

from ..backend_client import BackendClient
from . import account, prompts, publishing, video, workflows


def register_tools(server: MCPServer, backend: BackendClient) -> None:
    """Register every tool and prompt on ``server``; each reaches the API through ``backend``."""
    for module in (video, account, publishing, workflows, prompts):
        module.register(server, backend)
