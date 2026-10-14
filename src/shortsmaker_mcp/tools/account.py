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
        """Use when the user asks how many credits they have, or a price needs checking against the balance.

        Do: if the balance is too low, give the billing link from the server instructions.
        Don't: offer to buy credits; this connector cannot buy credits or change billing.
        Returns: ``balance`` and recent credit status.
        """
        return _dict_result(await _api(backend, "GET", "/api/payments/credit-status"))

    @server.tool(
        title="Check my subscription",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_subscription() -> dict[str, Any]:
        """Use when the user asks about their plan, renewal date or cancellation.

        Don't: offer to change or cancel it; that happens on the website (billing link).
        Returns: current subscription, renewal and cancellation status.
        """
        return _dict_result(await _api(backend, "GET", "/api/payments/subscription"))

    @server.tool(
        title="List subscription plans",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_plans() -> dict[str, Any]:
        """Use when the user asks what plans exist or what they cost.

        Don't: offer to subscribe or pay; plan changes and payments are done on the website
        (the billing link is in the server instructions).
        Returns: active plans and prices.
        """
        return _dict_result(await _api(backend, "GET", "/api/payments/plans"))

    @server.tool(
        title="Get account features",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_account_features() -> dict[str, Any]:
        """Use when a choice from get_video_options seems unavailable to this account.

        Don't: call it for normal video making; get_video_options already has the choices.
        Returns: the account's product feature flags.
        """
        return _dict_result(await _api(backend, "GET", "/api/features"))
