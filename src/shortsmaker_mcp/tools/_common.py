"""Types, risk annotations, and request helpers shared by every tool module.

The tools are a deliberately narrow adapter over the existing user-facing API. Every write
requires an explicit ``confirm=True`` argument so a client cannot accidentally turn a read/plan
conversation into a credit-spending or publishing action.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..backend_client import BackendAPIError, BackendClient, BackendUnavailable
from ..oauth_auth import current_principal

Niche = Annotated[
    str,
    Field(
        min_length=1,
        max_length=10_000,
        description="The topic or niche for the generated short.",
    ),
]

JobId = Annotated[UUID, Field(description="The ShortsMaker video job UUID.")]

PostId = Annotated[UUID, Field(description="The ShortsMaker scheduled-post UUID.")]

ConnectionId = Annotated[UUID, Field(description="A connected social-account UUID.")]

WorkflowId = Annotated[UUID, Field(description="A ShortsMaker recurring-workflow UUID.")]

WorkflowSlotId = Annotated[UUID, Field(description="A scheduled workflow-slot UUID.")]

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
        description="A fresh client-generated key that makes a retried create request safe.",
    ),
]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
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

def _backend_error(exc: Exception) -> ToolError:
    if isinstance(exc, BackendUnavailable):
        return ToolError("ShortsMaker API is unavailable. Try again shortly.")
    if isinstance(exc, BackendAPIError):
        if exc.status_code == 401:
            return ToolError("Your ShortsMaker access token expired or is invalid.")
        if exc.status_code == 402:
            return ToolError(exc.detail)
        if exc.status_code == 429:
            return ToolError("ShortsMaker rate limit reached. Try again shortly.")
        if exc.status_code >= 500:
            return ToolError("ShortsMaker could not complete that request. Try again shortly.")
        return ToolError(exc.detail)
    return ToolError("ShortsMaker could not complete that request.")

def _require_confirmation(confirm: bool, action: str) -> None:
    if not confirm:
        raise ToolError(
            f"{action} was not performed. First show the user the exact action and its "
            "cost/destination, obtain explicit approval, then call again with confirm=true."
        )

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
        return await backend.request(method, path, token, params=params, body=body)
    except (BackendUnavailable, BackendAPIError) as exc:
        raise _backend_error(exc) from exc
