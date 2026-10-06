"""Read-only credit, subscription, and plan tools. Billing changes are done on the website."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..backend_client import BackendClient
from ._common import READ_ONLY, _api, _dict_result


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="Get credit status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_credit_status() -> dict[str, Any]:
        """Read the signed-in user's current credit balance and recent credit status.

        This MCP cannot buy credits or change billing. If the balance is too low, tell the user to
        top up on the ShortsMaker website.
        """
        return _dict_result(await _api(backend, "GET", "/api/payments/credit-status"))

    @server.tool(
        title="Get subscription status",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_subscription() -> dict[str, Any]:
        """Read the signed-in user's current subscription, renewal, and cancellation status."""
        return _dict_result(await _api(backend, "GET", "/api/payments/subscription"))

    @server.tool(
        title="List subscription plans",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_list_subscription_plans() -> dict[str, Any]:
        """List active subscription plans and prices. Read-only: plan changes and payments are done by
        the user on the ShortsMaker website, not through this MCP."""
        return _dict_result(await _api(backend, "GET", "/api/payments/plans"))

    @server.tool(
        title="Get account features",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_account_features() -> dict[str, Any]:
        """Read account-level product feature flags used by the ShortsMaker composer."""
        return _dict_result(await _api(backend, "GET", "/api/features"))
