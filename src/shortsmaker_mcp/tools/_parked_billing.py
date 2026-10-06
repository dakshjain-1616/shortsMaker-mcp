"""PARKED: billing write tools and billing prompt, intentionally not registered.

Billing changes (top-ups, plan changes, billing portal, cancellation) are done by the user on the
ShortsMaker website for now; the MCP only reads credits, subscription, and plans. This module is
kept, with its tests in ``tests/test_parked_billing.py``, so the tools can be re-enabled by adding
``_parked_billing`` to the modules registered in ``tools/__init__.py``.
"""

from __future__ import annotations

import math
from typing import Annotated, Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backend_client import BackendClient
from ._common import (
    IDEMPOTENT_WRITE,
    WRITE,
    Confirm,
    _api,
    _dict_result,
    _require_confirmation,
)


def _money_amount(value: float) -> float:
    try:
        finite = math.isfinite(value)
    except (TypeError, ValueError):
        finite = False
    if isinstance(value, bool) or not finite:
        raise ToolError("amount_usd must be a finite number.")
    if round(value, 2) != value:
        raise ToolError("amount_usd must have at most two decimal places.")
    if value < 5 or value > 500:
        raise ToolError("amount_usd must be between $5.00 and $500.00.")
    return value


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="Buy credit top-up",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_buy_credits(
        amount_usd: Annotated[
            float,
            Field(description="One-time top-up amount in USD; allowed range is $5.00 to $500.00."),
        ],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Create a Stripe checkout URL for a one-time credit top-up.

        This creates a checkout session; it does not grant credits until the user completes
        payment. Before calling, show the exact dollar amount and explain that the returned URL
        must be opened by the user. This is still a financial action and requires confirm=true.
        """
        amount = _money_amount(amount_usd)
        _require_confirmation(confirm, f"Buying a ${amount:.2f} credit top-up")
        return _dict_result(await _api(
            backend,
            "POST",
            "/api/payments/buy-credits",
            body={"amount_usd": amount},
        ))

    @server.tool(
        title="Start or change subscription",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_start_subscription(
        pack_id: Annotated[UUID, Field(description="Plan ID returned by list_subscription_plans.")],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Start checkout for a plan or schedule a switch to a different subscription plan.

        Read the plan list and current subscription first. A new subscription returns a checkout
        URL and requires user payment; an existing subscription may change at the next billing
        cycle without an immediate checkout. Show the selected plan and its price before calling.
        """
        _require_confirmation(confirm, f"Starting or changing subscription plan {pack_id}")
        return _dict_result(await _api(
            backend,
            "POST",
            "/api/payments/subscribe",
            body={"pack_id": str(pack_id)},
        ))

    @server.tool(
        title="Open billing portal",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def shortsmaker_open_billing_portal(confirm: Confirm = False) -> dict[str, Any]:
        """Create a one-time Stripe billing-portal URL for the signed-in user.

        The portal may allow changing payment methods, viewing invoices, or canceling a plan.
        Return the URL to the user and never claim that a billing change has happened yet.
        """
        _require_confirmation(confirm, "Opening the billing portal")
        return _dict_result(await _api(backend, "POST", "/api/payments/billing-portal"))

    @server.tool(
        title="Cancel subscription",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_cancel_subscription(confirm: Confirm = False) -> dict[str, Any]:
        """Immediately cancel the active subscription after explicit user approval.

        The backend cancels in Stripe immediately; remaining credits stay usable until their
        normal expiry. Read subscription status first and show the plan being canceled.
        """
        _require_confirmation(confirm, "Canceling the active subscription")
        return _dict_result(await _api(backend, "DELETE", "/api/payments/subscription"))

    @server.prompt(
        name="shortsmaker_billing_workflow",
        title="Review ShortsMaker billing",
        description="Guide the assistant through read-only billing review before any payment action.",
    )
    def shortsmaker_billing_workflow() -> list[dict[str, Any]]:
        """Return a user-controlled billing review prompt with a hard approval boundary."""
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        "Help me review my ShortsMaker billing. First read credit status, current "
                        "subscription, and available plans. Explain prices, credits, renewal or "
                        "cancellation behavior, and whether an action opens checkout or changes "
                        "billing. Ask for explicit approval of the exact action before calling "
                        "buy_credits, start_subscription, open_billing_portal, or "
                        "cancel_subscription with confirm=true. Never claim payment completed "
                        "just because a checkout URL was created."
                    ),
                },
            }
        ]
