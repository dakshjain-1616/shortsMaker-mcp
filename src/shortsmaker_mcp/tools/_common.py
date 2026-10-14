"""Types, risk annotations, and request helpers shared by every tool module.

The tools are a deliberately narrow adapter over the existing user-facing API. Every write
requires an explicit ``confirm=True`` argument so a client cannot accidentally turn a read/plan
conversation into a credit-spending or publishing action.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..backend_client import BackendAPIError, BackendClient, BackendUnavailable
from ..config import APP_URL, BILLING_URL
from ..oauth_auth import current_principal

Niche = Annotated[
    str,
    Field(
        min_length=1,
        max_length=10_000,
        description="The topic or niche for the generated short.",
    ),
]

JobId = Annotated[UUID, Field(description="A video id, from list_videos or create_video.")]

PostId = Annotated[UUID, Field(description="A post id, from list_posts or publish_video.")]

ConnectionId = Annotated[UUID, Field(description="A connected social account id, from list_connections.")]

WorkflowId = Annotated[UUID, Field(description="An Autopilot series id, from list_series.")]

WorkflowSlotId = Annotated[UUID, Field(description="A scheduled series slot id, from list_series_slots.")]

Confirm = Annotated[
    bool,
    Field(
        description=(
            "Set true only after the user has explicitly reviewed and approved this action, "
            "including its cost, destination, or external side effect."
        )
    ),
]

IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description=(
            "A key you make up for this one video, e.g. 'rome-30s-1'. Retrying the same video "
            "(after a timeout) uses the SAME key, which never charges twice; every different "
            "video needs a NEW key."
        ),
    ),
]

# Reads only touch the signed-in user's own workspace, so they are not open-world.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# Claude Code asks the user on every call of a tool with this hint, even in auto modes. Used on
# the tools that spend credits, post publicly or delete. Other clients ignore it.
ASK_EVERY_TIME = {"anthropic/requiresUserInteraction": True}

def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")

def _json_value(value: Any) -> Any:
    """Convert values to the JSON-safe representation expected by httpx."""
    if isinstance(value, (datetime, UUID)):
        return _json_default(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value

def _optional_params(**values: Any) -> dict[str, Any]:
    return {
        key: _json_value(value)
        for key, value in values.items()
        if value is not None
    }

def _bounded_text(
    value: str | None,
    name: str,
    max_length: int,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ToolError(f"{name} is required.")
        return None
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise ToolError(f"{name} is required.")
        return None
    if len(cleaned) > max_length:
        raise ToolError(f"{name} must be {max_length} characters or fewer.")
    return cleaned

def _clean_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    if len(tags) > 100:
        raise ToolError("tags must contain 100 items or fewer.")
    return [
        _bounded_text(tag, "tag", 200, required=True)  # type: ignore[arg-type]
        for tag in tags
    ]

def _dedupe_ids(connection_ids: list[UUID]) -> list[str]:
    values = list(dict.fromkeys(str(value) for value in connection_ids))
    if not values:
        raise ToolError("connection_ids must contain at least one connected account.")
    if len(values) > 20:
        raise ToolError("connection_ids must contain 20 accounts or fewer.")
    return values

def _dict_result(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {"result": result}

_URL = re.compile(r"https?://\S+")

# Fallbacks for when the backend gives no sentence of its own. 401/403/429/5xx always use these:
# 401 and 403 here mean connector configuration, not something the user can fix in chat.
_FALLBACK = {
    401: "Faceless Video Maker could not verify this connector's backend access. Ask the connector administrator to check its configuration.",
    402: "Not enough credits for this.",
    403: f"This action isn't available through the connector yet. The user can do it on the website ({APP_URL}).",
    404: "Not found.",
    409: "Not allowed in the item's current state.",
    429: "Too many requests. Wait a minute before trying again; do not retry in a loop.",
}
# A next step for the model, added after the backend's own sentence.
_NEXT_STEP = {
    402: f" Show the user the price and their balance (get_credits). They can add credits at {BILLING_URL}; you cannot buy credits.",
    404: " Use the matching list tool (list_videos, list_series, list_posts, list_connections) to find a valid id.",
}


def _public_text(text: str) -> str:
    """Backend sentences are written for users, but strip links (signed URLs carry credentials)."""
    return _URL.sub("[link]", text).strip()[:300]


def _backend_error(exc: Exception, write: bool = False) -> ToolError:
    if isinstance(exc, BackendUnavailable):
        if write:
            # A timed-out write may still have happened; blind retries could double-charge or double-post.
            return ToolError(
                "Faceless Video Maker did not answer in time, so this may or may not have gone through. "
                "Check list_videos or list_posts before trying again; a create_video retry with the SAME "
                "idempotency_key is always safe."
            )
        return ToolError("Faceless Video Maker is temporarily unavailable. Tell the user and try again in a minute.")
    if isinstance(exc, BackendAPIError):
        status = exc.status_code
        if status >= 500:
            return ToolError("Faceless Video Maker could not complete that request. Try again shortly.")
        if status in (401, 403, 429) or exc.detail.startswith("Request failed ("):
            message = _FALLBACK.get(status, "Faceless Video Maker rejected this request. Check the values and try again.")
        else:
            message = _public_text(exc.detail)
        if message and message[-1] not in ".!?":
            message += "."
        return ToolError(message + _NEXT_STEP.get(status, ""))
    return ToolError("Faceless Video Maker could not complete that request.")

def _require_confirmation(confirm: bool, action: str) -> None:
    if not confirm:
        raise ToolError(
            f"{action} was not performed. First show the user the exact action and its "
            "cost/destination, obtain explicit approval, then call again with confirm=true."
        )

def _public_result(value: Any) -> Any:
    """Hide a video job's ``error`` (engine internals) but keep ``last_error`` readable.

    ``last_error`` on series, slots, posts and connections is a product sentence ("Paused. Add
    credits to resume.") that answers the user's "why"; links are stripped as they may be signed.
    """
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key == "error" and item:
                cleaned[key] = f"This video failed. The user can see details at {APP_URL}/dashboard."
            elif key == "last_error" and isinstance(item, str):
                cleaned[key] = _public_text(item)
            else:
                cleaned[key] = _public_result(item)
        return cleaned
    return value


async def _api(
    backend: BackendClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    try:
        principal = current_principal()
    except RuntimeError as exc:
        raise ToolError("Authentication is missing for this MCP request.") from exc

    required_scope = "mcp:read" if method.upper() == "GET" else "mcp:write"
    if required_scope not in principal.scopes:
        raise ToolError(f"This tool requires {required_scope} authorization.")

    try:
        token = backend.bridge_token(principal)
        return _public_result(await backend.request(method, path, token, params=params, body=body))
    except (BackendUnavailable, BackendAPIError) as exc:
        raise _backend_error(exc, write=method.upper() != "GET") from exc
