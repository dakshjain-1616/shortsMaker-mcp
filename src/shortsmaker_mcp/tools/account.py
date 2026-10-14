"""Read-only credit, subscription, and plan tools. Billing changes are done on the website."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..backend_client import BackendClient
from ._common import READ_ONLY, _api, _dict_result


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="Check my credits",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_credits() -> dict[str, Any]:
        """Read the signed-in user's current credit balance and recent credit status.

        This MCP cannot buy credits or change billing. If the balance is too low, tell the user to
        add credits on the website (the billing link is in the server instructions).
        """
        return _dict_result(await _api(backend, "GET", "/api/payments/credit-status"))

    @server.tool(
        title="Check my subscription",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_subscription() -> dict[str, Any]:
        """Read the signed-in user's current subscription, renewal, and cancellation status."""
        return _dict_result(await _api(backend, "GET", "/api/payments/subscription"))

    @server.tool(
        title="List subscription plans",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_plans() -> dict[str, Any]:
        """List active subscription plans and prices. Read-only: plan changes and payments are done by
        the user on the website (the billing link is in the server instructions), not through this MCP."""
        return _dict_result(await _api(backend, "GET", "/api/payments/plans"))

    @server.tool(
        title="Get account features",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_account_features() -> dict[str, Any]:
        """Read account-level product feature flags used by the Faceless Video Maker composer."""
        return _dict_result(await _api(backend, "GET", "/api/features"))
