"""The parked billing tools are not registered in the service; these keep them verified so they
can be re-enabled by adding ``_parked_billing`` to ``tools/__init__.py``."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp.oauth_auth import Principal, _principal
from shortsmaker_mcp.tools import _parked_billing


def _registered_tools():
    server = MCPServer("test")
    backend = AsyncMock()
    backend.bridge_token = Mock(return_value="bridge-token")
    _parked_billing.register(server, backend)
    return server, backend


def test_billing_tools_are_not_part_of_the_service():
    from shortsmaker_mcp import tools

    server = MCPServer("test")
    tools.register_tools(server, AsyncMock())

    names = set(server._tool_manager._tools) | set(server._prompt_manager._prompts)
    assert not names & {
        "shortsmaker_buy_credits",
        "shortsmaker_start_subscription",
        "shortsmaker_open_billing_portal",
        "shortsmaker_cancel_subscription",
        "shortsmaker_billing_workflow",
    }
    assert "shortsmaker_get_credit_status" in names  # reading credits stays available


@pytest.mark.parametrize("amount", [4.99, 500.01, float("nan"), float("inf"), 10.001])
def test_credit_top_up_rejects_unsafe_amounts_before_backend_call(amount):
    server, backend = _registered_tools()
    tool = server._tool_manager._tools["shortsmaker_buy_credits"]

    with pytest.raises(ToolError):
        asyncio.run(tool.fn(amount, confirm=True))

    backend.request.assert_not_awaited()


def test_credit_top_up_requires_confirmation_and_forwards_amount():
    server, backend = _registered_tools()
    tool = server._tool_manager._tools["shortsmaker_buy_credits"]

    with pytest.raises(ToolError, match="explicit approval"):
        asyncio.run(tool.fn(25.0, confirm=False))
    backend.request.assert_not_awaited()

    backend.request.return_value = {"success": True, "checkout_url": "https://checkout.example"}
    context = _principal.set(Principal("user-1", frozenset({"mcp:read", "mcp:write"}), "client-1"))
    try:
        result = asyncio.run(tool.fn(25.0, confirm=True))
    finally:
        _principal.reset(context)

    assert result["checkout_url"] == "https://checkout.example"
    backend.request.assert_awaited_once_with(
        "POST",
        "/api/payments/buy-credits",
        "bridge-token",
        params=None,
        body={"amount_usd": 25.0},
    )



def test_billing_prompt_forbids_claiming_payment_completed():
    server, _ = _registered_tools()
    prompt = server._prompt_manager._prompts["shortsmaker_billing_workflow"]

    assert "Never claim payment completed" in prompt.fn()[0]["content"]["text"]
