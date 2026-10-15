"""Types, risk annotations, and request helpers shared by every tool module.

The tools are a deliberately narrow adapter over the existing user-facing API. Every write
requires an explicit ``confirm=True`` argument so a client cannot accidentally turn a read/plan
conversation into a credit-spending or publishing action.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

JobId = Annotated[str, Field(
    min_length=1, max_length=200,
    description="The video: words from its title or topic, as the user said them ('the marble one'), or its id.",
)]

PostId = Annotated[UUID, Field(description="A post id, from list_posts or publish_video.")]

ConnectionId = Annotated[str, Field(
    min_length=1, max_length=200,
    description="The account: its name or platform as the user said it ('my YouTube'), or its id.",
)]

WorkflowId = Annotated[str, Field(
    min_length=1, max_length=200,
    description="The series: words from its name or theme ('my space facts series'), or its id.",
)]

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

def _json_value(value: Any) -> Any:
    """Convert values to the JSON-safe representation expected by httpx."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
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

def _dedupe_ids(connection_ids: list[Any]) -> list[str]:
    values = list(dict.fromkeys(str(value) for value in connection_ids))
    if not values:
        raise ToolError("connection_ids must contain at least one connected account.")
    if len(values) > 20:
        raise ToolError("connection_ids must contain 20 accounts or fewer.")
    return values

def _dict_result(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {"result": result}

def _with_next(result: Any, next_step: str, summary: str | None = None) -> dict[str, Any]:
    """Tell the agent what this result means and what to do next, so it never reports a queued
    write as done. ``summary`` is a plain sentence the agent can pass to the user as-is."""
    return {**_dict_result(result), **({"summary": summary} if summary else {}), "next_step": next_step}

def _items(result: Any, next_step: str | None = None, summary: str | None = None) -> dict[str, Any]:
    """Lists go out as an object so an empty or problem state can carry a next step."""
    items = result if isinstance(result, list) else []
    return {
        "items": items,
        "count": len(items),
        **({"summary": summary} if summary else {}),
        **({"next_step": next_step} if next_step else {}),
    }


# One plain label per status, whatever the backend spells it ("queued"/"pending",
# "cancelled"/"canceled"). Slot and post statuses share the table.
STATUS_LABEL = {
    "queued": "In line",
    "pending": "In line",
    "processing": "Being made",
    "generating": "Being made",
    "paused": "Paused",
    "ready": "Ready",
    "complete": "Ready",
    "failed": "Didn't work, credits returned",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "planned": "Planned",
    "promoted": "Made",
    "skipped": "Skipped",
    "scheduled": "Waiting to post",
    "publishing": "Posting now",
    "published": "Posted",
    "active": "Running",
    "stopped": "Stopped",
    "error": "Needs reconnecting",
    "revoked": "Disconnected",
}


def _label(status: Any) -> str:
    return STATUS_LABEL.get(status, str(status or "Unknown").capitalize())


def _parse_time(value: Any) -> datetime | None:
    """ISO text to an aware datetime; tolerates the backend's malformed ``+00:00Z``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("+00:00Z", "+00:00")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ago(seconds: float) -> str:
    future, seconds = seconds < 0, abs(seconds)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            text = f"{n} {unit}{'' if n == 1 else 's'}"
            return f"in {text}" if future else f"{text} ago"
    return "in a moment" if future else "just now"


def _when(value: Any, tz: str | None = None, *, now: datetime | None = None) -> str:
    """A time a person reads: "today 8:00 PM IST" in ``tz``, or "2 hours ago" without one.

    The backend stores no user timezone, so only series (which have one) get clock times.
    """
    moment = _parse_time(value)
    if moment is None:
        return ""
    now = now or datetime.now(UTC)
    zone = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
    if zone is None:
        return _ago((now - moment).total_seconds())
    local, today = moment.astimezone(zone), now.astimezone(zone).date()
    days = (local.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or f"{local:%a} {local.day} {local:%b}"
    clock = f"{local:%I:%M %p}".lstrip("0")
    return f"{day} {clock} {local.tzname()}"

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
    409: " Read the item (get_video, get_series or list_posts) and tell the user its status and last_error; don't retry blindly.",
}


# Known raw engine/backend messages, rewritten for a non-technical user. Matched by substring.
_ENGINE_TROUBLE = "Video making is having trouble right now. Try again in a few minutes."
_FRIENDLY = (
    ("engine unavailable", _ENGINE_TROUBLE),
    ("can't price this video", _ENGINE_TROUBLE),
    ("_key not set", _ENGINE_TROUBLE),
    ("something went wrong on our side", "Something went wrong on our side. Try again in a minute."),
)
# FastAPI validation text arrives as "times_of_day.0: Value error, Time must be HH:MM"; keep the sentence.
_FIELD_PREFIX = re.compile(r"(^|; )[\w.]+: (Value error, )?")


def _public_text(text: str) -> str:
    """Backend sentences are written for users, but strip links (signed URLs carry credentials)."""
    lowered = text.lower()
    for needle, friendly in _FRIENDLY:
        if needle in lowered:
            return friendly
    return _FIELD_PREFIX.sub(r"\1", _URL.sub("[link]", text)).strip()[:300]


def _backend_error(exc: Exception, write: bool = False, spends: bool = False) -> ToolError:
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
            if _public_text(exc.detail) == _ENGINE_TROUBLE:
                # Pricing failed, so nothing was held.
                return ToolError(_ENGINE_TROUBLE + (" No credits were used." if spends else ""))
            return ToolError("Faceless Video Maker could not complete that request. Try again shortly.")
        if status in (401, 403, 429) or exc.detail.startswith("Request failed ("):
            message = _FALLBACK.get(status, "Faceless Video Maker rejected this request. Check the values and try again.")
        else:
            message = _public_text(exc.detail)
        if message and message[-1] not in ".!?":
            message += "."
        if spends:
            # A rejected request never reaches the credit hold.
            message += " No credits were used."
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
                cleaned[key] = "This video didn't finish. Its credits were returned."
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
        # Making a video, or remaking one (a video or series retry); posts never spend credits.
        spends = method.upper() == "POST" and (
            path == "/api/jobs" or (path.endswith("/retry") and not path.startswith("/api/posts"))
        )
        raise _backend_error(exc, write=method.upper() != "GET", spends=spends) from exc


# ---- Finding things by name ----
# Words people wrap around a name ("my YouTube", "the marble one") that never appear in it.
_FILLER = {"my", "the", "a", "an", "one", "video", "videos", "series", "channel", "account", "about", "on", "that"}


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _words(ref: str) -> list[str]:
    """The words of a name that can appear in it: "the marble one" -> ["marble"]."""
    return [word for word in re.findall(r"\w+", ref.lower()) if word not in _FILLER]


def _pick(
    items: list[dict[str, Any]], ref: str, text_of: Any, describe: Any, kind: str,
) -> dict[str, Any]:
    """The one item whose text contains every meaningful word of ``ref``.

    None or several matches raise an error that names the options so the agent can ask the user;
    ids ride along in brackets for the agent only.
    """
    words = _words(ref)
    matches = [item for item in items if words and all(word in text_of(item).lower() for word in words)]
    if len(matches) == 1:
        return matches[0]
    options = matches or items
    listed = "; ".join(f"{describe(item)} [id {item.get('id')}]" for item in options[:6])
    if matches:
        raise ToolError(
            f"Several {kind}s match {ref!r}: {listed}. Ask the user which one (don't show the ids)."
        )
    if not items:
        raise ToolError(f"There is no {kind} yet.")
    raise ToolError(f"No {kind} matches {ref!r}. Recent ones: {listed}. Ask the user which one (don't show the ids).")


async def _video_ref(backend: BackendClient, ref: str) -> str:
    """A video id from a name or an id. Ids skip the lookup."""
    ref = str(ref).strip()
    if _is_uuid(ref):
        return ref
    jobs = await _api(backend, "GET", "/api/jobs")
    jobs = [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    return str(_pick(
        jobs, ref,
        lambda job: f"{job.get('title') or ''} {job.get('niche') or ''}",
        lambda job: f"'{job.get('title') or (job.get('niche') or '')[:60]}' ({_when(job.get('created_at'))})",
        "video",
    )["id"])


async def _series_ref(backend: BackendClient, ref: str) -> str:
    ref = str(ref).strip()
    if _is_uuid(ref):
        return ref
    series = await _api(backend, "GET", "/api/workflows")
    series = [item for item in series if isinstance(item, dict)] if isinstance(series, list) else []
    return str(_pick(
        series, ref,
        lambda item: f"{item.get('name') or ''} {item.get('theme') or ''}",
        lambda item: f"'{item.get('name')}' ({item.get('status')})",
        "series",
    )["id"])


async def _account_refs(backend: BackendClient, refs: list[str]) -> list[str]:
    """Account ids from names, platforms ('my YouTube') or ids, deduplicated."""
    refs = [str(ref).strip() for ref in refs]
    if all(_is_uuid(ref) for ref in refs):
        return _dedupe_ids(refs)
    connections = await _api(backend, "GET", "/api/connections")
    connections = [c for c in connections if isinstance(c, dict)] if isinstance(connections, list) else []
    return _dedupe_ids([
        ref if _is_uuid(ref) else str(_pick(
            connections, ref,
            lambda c: f"{c.get('account_name') or ''} {c.get('platform') or ''}",
            lambda c: f"{c.get('account_name')} ({c.get('platform')})",
            "connected account",
        )["id"])
        for ref in refs
    ])
