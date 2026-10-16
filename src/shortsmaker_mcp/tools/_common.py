"""Types, risk annotations, and request helpers shared by every tool module.

The tools are a deliberately narrow adapter over the existing user-facing API. Every write
requires an explicit ``confirm=True`` argument so a client cannot accidentally turn a read/plan
conversation into a credit-spending or publishing action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..backend_client import BackendAPIError, BackendClient, BackendUnavailable
from ..config import APP_URL, BILLING_URL
from ..oauth_auth import current_principal

_APPROVAL_KEY = (os.getenv("MCP_BRIDGE_SECRET") or "").encode() or secrets.token_bytes(32)
_APPROVAL_TTL = timedelta(minutes=15)

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
            raise _fail("invalid_input", f"{name} is required.")
        return None
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise _fail("invalid_input", f"{name} is required.")
        return None
    if len(cleaned) > max_length:
        raise _fail("invalid_input", f"{name} must be {max_length} characters or fewer.")
    return cleaned

def _clean_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    if len(tags) > 100:
        raise _fail("invalid_input", "tags must contain 100 items or fewer.")
    return [
        _bounded_text(tag, "tag", 200, required=True)  # type: ignore[arg-type]
        for tag in tags
    ]

def _dedupe_ids(connection_ids: list[Any]) -> list[str]:
    values = list(dict.fromkeys(str(value) for value in connection_ids))
    if not values:
        raise _fail("invalid_input", "connection_ids must contain at least one connected account.")
    if len(values) > 20:
        raise _fail("invalid_input", "connection_ids must contain 20 accounts or fewer.")
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
# "cancelled"/"canceled"). Videos, series, slots and accounts; posts use POST_STATES.
STATUS_LABEL = {
    "queued": "In line",
    "pending": "In line",
    "processing": "Being made",
    "generating": "Being made",
    "paused": "Paused",
    "ready": "Ready",
    "succeeded": "Ready",
    "complete": "Ready",
    "failed": "Didn't work, credits returned",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "planned": "Planned",
    "promoted": "Made",
    "skipped": "Skipped",
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

# The seven publishing states the user hears. The backend has five statuses; due-now vs later
# (queued/scheduled) and confirmed vs unconfirmed (published/sent) are told apart here.
POST_STATES = {
    "queued": "Waiting to go out",
    "scheduled": "Scheduled",
    "publishing": "Posting now",
    "sent": "Sent, not confirmed live",
    "published": "Live",
    "failed": "Didn't post",
    "cancelled": "Cancelled",
}
# ponytail: keyword match on the platform's error text; a backend error code would be exact.
_ACCOUNT_TROUBLE = ("reconnect", "auth", "token", "revoked", "expired", "permission", "consent")


def _post_state(post: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """One post as the user hears it: ``state`` (a POST_STATES key), ``status_label``, and when
    it applies ``when``, ``next_try``, ``tries`` and ``fix``. A post the platform accepted but
    gave no link for is "sent", never "live"."""
    now = now or datetime.now(UTC)
    status, attempts = post.get("status"), post.get("attempts") or 0
    out: dict[str, Any] = {}
    if status == "scheduled":
        at, retry_at = _parse_time(post.get("scheduled_at")), _parse_time(post.get("next_attempt_at"))
        if attempts:
            state, label = "queued", "Trying again" + (f" {_when(retry_at, now=now)}" if retry_at and retry_at > now else "")
            out["tries"] = f"{attempts} of {post.get('max_attempts') or '?'}"
            if retry_at and retry_at > now:
                out["next_try"] = _when(retry_at, now=now)
        elif at and at > now + timedelta(minutes=1):
            state, label = "scheduled", f"Goes out {_when(at, now=now)}"
            out["when"] = "goes out " + _when(at, now=now)
        else:
            state, label = "queued", POST_STATES["queued"]
    elif status == "published":
        state = "published" if post.get("platform_post_url") else "sent"
        label = POST_STATES[state]
        if post.get("published_at"):
            out["when"] = "posted " + _when(post["published_at"], now=now)
    elif status == "failed":
        state, label = "failed", POST_STATES["failed"]
        error = str(post.get("last_error") or "").lower()
        out["fix"] = (
            "Reconnect the account, then retry the post"
            if any(word in error for word in _ACCOUNT_TROUBLE) else "Retry the post"
        )
    else:
        state = {"canceled": "cancelled"}.get(status, str(status or "unknown"))
        label = POST_STATES.get(state) or _label(status)
    return {"state": state, "status_label": label, **out}


_URL = re.compile(r"https?://\S+")

def _fail(code: str, message: str) -> ToolError:
    """A sentence the user can hear, then a stable code the agent acts on (see INSTRUCTIONS)."""
    return ToolError(f"{message} [code: {code}]")


# The backend sends only {"detail": "..."}; the HTTP status decides the code (other 4xx: invalid_input).
_CODES = {
    401: "connector_auth",
    402: "insufficient_credits",
    403: "not_available_here",
    404: "not_found",
    409: "wrong_state",
    429: "rate_limited",
}
# Fallbacks for when the backend gives no sentence of its own. 401/403/429 always use these:
# 401 and 403 here mean connector configuration, not something the user can fix in chat.
_FALLBACK = {
    401: "Faceless Video Maker could not verify this connector's backend access. Ask the connector administrator to check its configuration.",
    402: "Not enough credits for this.",
    403: f"This action isn't available through the connector yet. The user can do it on the website ({APP_URL}).",
    404: "Not found.",
    409: "Not allowed in the item's current state.",
    429: "Too many requests; do not retry in a loop.",
}
# A next step for the model, added after the backend's own sentence.
_NEXT_STEP = {
    402: f" Show the user the price and their balance (get_credits). They can add credits at {BILLING_URL}; you cannot buy credits.",
    404: " Use the matching list tool (list_videos, list_series, list_posts, list_connections) to find a valid id.",
    409: " Read the item (get_video, get_series or list_posts) and tell the user its status and last_error; don't retry blindly.",
}


# Known raw engine/backend messages, rewritten for a non-technical user. Matched by substring.
_ENGINE_TROUBLE = "Video making is having trouble right now."
_FRIENDLY = (
    ("engine unavailable", _ENGINE_TROUBLE),
    ("can't price this video", _ENGINE_TROUBLE),
    ("_key not set", _ENGINE_TROUBLE),
)
# FastAPI validation text arrives as "times_of_day.0: Value error, Time must be HH:MM"; keep the sentence.
_FIELD_PREFIX = re.compile(r"(^|; )[\w.]+: (Value error, )?")
# A 500 says "Something went wrong on our side. Reference <request id>"; support needs the id.
_REFERENCE = re.compile(r"Reference ([\w-]+)")


def _public_text(text: str) -> str:
    """Backend sentences are written for users, but strip links (signed URLs carry credentials)."""
    lowered = text.lower()
    for needle, friendly in _FRIENDLY:
        if needle in lowered:
            return friendly
    return _FIELD_PREFIX.sub(r"\1", _URL.sub("[link]", text)).strip()[:300]


def _wait(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f" Try again in about {seconds} seconds." if seconds < 120 else f" Try again in about {round(seconds / 60)} minutes."


def _backend_error(exc: Exception, write: bool = False, spends: bool = False) -> ToolError:
    unused = " No credits were used." if spends else ""
    if isinstance(exc, BackendUnavailable):
        if write:
            # A timed-out write may still have happened; blind retries could double-charge or double-post.
            return _fail(
                "outcome_unknown",
                "Faceless Video Maker did not answer in time, so this may or may not have gone through. "
                "Check list_videos or list_posts before trying again; asking create_video for the same "
                "video again is safe, it never charges twice.",
            )
        return _fail("service_busy", "Faceless Video Maker is temporarily unavailable. Tell the user and try again in a minute.")
    if not isinstance(exc, BackendAPIError):
        return _fail("unexpected", "Faceless Video Maker could not complete that request. Try again shortly.")
    status = exc.status_code
    if status >= 500:
        engine = _public_text(exc.detail) == _ENGINE_TROUBLE
        if engine or status == 503:
            # Pricing or the engine failed before any hold, so nothing was charged.
            reason = _ENGINE_TROUBLE if engine else "Faceless Video Maker is busy right now."
            return _fail("service_busy", reason + unused + (_wait(exc.retry_after) or " Try again in a few minutes."))
        reference = _REFERENCE.search(exc.detail)
        return _fail(
            "server_error",
            "Something went wrong on our side. Try again in a minute."
            + (f" If it keeps happening, give support this reference: {reference.group(1)}." if reference else ""),
        )
    if status in (401, 403, 429) or exc.detail.startswith("Request failed ("):
        message = _FALLBACK.get(status, "Faceless Video Maker rejected this request. Check the values and try again.")
    else:
        message = _public_text(exc.detail)
    if message and message[-1] not in ".!?":
        message += "."
    # A rejected request never reaches the credit hold.
    message += unused + (_wait(exc.retry_after) if status == 429 else "")
    return _fail(_CODES.get(status, "invalid_input"), message + _NEXT_STEP.get(status, ""))

def _preview_token(approved: dict[str, Any], *, tool: str = "") -> str:
    """Sign an operation for this user and client; the token expires after fifteen minutes."""
    principal = current_principal()
    expires = int((datetime.now(UTC) + _APPROVAL_TTL).timestamp())
    stamp = format(expires, "x")
    payload = json.dumps(
        [principal.user_id, principal.client_id, tool, approved],
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    operation = hmac.new(_APPROVAL_KEY, payload, hashlib.sha256).hexdigest()[:24]
    signature = hmac.new(_APPROVAL_KEY, f"{stamp}.{operation}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"p-{stamp}.{operation}.{signature}"


def _check_token(given: str | None, expected: str, tool: str) -> None:
    if given is None:
        raise _fail(
            "needs_approval",
            f"Nothing was done. Call {tool} without confirm first, show the user the preview, then "
            "confirm with its preview_token.",
        )
    try:
        prefix, operation, signature = given.split(".")
        stamp = prefix.removeprefix("p-")
        expiry = int(stamp, 16)
        expected_operation = expected.split(".")[1]
        valid = (
            prefix.startswith("p-")
            and datetime.now(UTC).timestamp() <= expiry
            and hmac.compare_digest(operation, expected_operation)
            and hmac.compare_digest(
                signature,
                hmac.new(_APPROVAL_KEY, f"{stamp}.{operation}".encode(), hashlib.sha256).hexdigest()[:24],
            )
        )
    except (ValueError, IndexError):
        valid = False
    if not valid:
        raise _fail(
            "preview_changed",
            f"The price or settings changed since the user approved them. Nothing was done. Call {tool} "
            "without confirm for the new preview and ask the user again.",
        )


PreviewToken = Annotated[str | None, Field(max_length=80, description=(
    "Required with confirm=true: the preview_token from the preview the user approved. If the "
    "price or settings changed since, nothing happens and you are told to preview again."
))]


def _approval(
    preview: dict[str, Any], confirm: bool, preview_token: str | None, tool: str,
) -> dict[str, Any] | None:
    """Return a preview or validate approval of the freshly rebuilt operation."""
    token = _preview_token(preview, tool=tool)
    if confirm:
        _check_token(preview_token, token, tool)
        return None
    return {
        "status": "needs_approval", "preview": preview, "preview_token": token,
        **({"estimated_cost": preview["estimated_cost"]} if preview.get("estimated_cost") is not None else {}),
        "next_step": f"Show this preview and ask for approval. Then call {tool} with confirm=true and preview_token={token!r}.",
    }


def _require_confirmation(confirm: bool, action: str) -> None:
    if not confirm:
        raise _fail(
            "needs_approval",
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
        raise _fail("connector_auth", "Authentication is missing for this MCP request.") from exc

    required_scope = "mcp:read" if method.upper() == "GET" else "mcp:write"
    if required_scope not in principal.scopes:
        raise _fail("connector_auth", f"This tool requires {required_scope} authorization.")

    try:
        token = backend.bridge_token(principal)
        return _public_result(await backend.request(method, path, token, params=params, body=body))
    except (BackendUnavailable, BackendAPIError) as exc:
        # Making a video, or remaking one (a video or series retry); posts never spend credits.
        spends = method.upper() == "POST" and (
            path == "/api/jobs" or (path.endswith("/retry") and not path.startswith("/api/posts"))
        )
        raise _backend_error(exc, write=method.upper() != "GET", spends=spends) from exc


def _choice_ids(values: Any) -> list[str]:
    return [str(item.get("id")) if isinstance(item, dict) else str(item)
            for item in values] if isinstance(values, list) else []


async def _resolve_video(
    backend: BackendClient, params: dict[str, Any], config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pin a supported resolution before calling the legacy API, whose default is always 720p."""
    config = config if isinstance(config, dict) else _dict_result(await _api(backend, "GET", "/api/config"))
    fmt, asked = params.get("format") or "transformation", params.get("resolution")
    by_format = config.get("resolutions_by_format") or {}
    entry = by_format.get(fmt) if isinstance(by_format, dict) else None
    offered = _choice_ids(entry.get("available")) if isinstance(entry, dict) else _choice_ids(entry)
    all_options = _choice_ids(config.get("resolutions"))
    if not all_options and isinstance(by_format, dict):
        all_options = list(dict.fromkeys(
            choice for value in by_format.values()
            for choice in _choice_ids(value.get("available") if isinstance(value, dict) else value)
        ))
    if asked and fmt != "auto" and offered and asked not in offered:
        raise _fail("invalid_input", f"This format doesn't offer resolution {asked!r}; it offers {', '.join(offered)}.")
    if asked:
        candidates = [asked]
    else:
        default = entry.get("default") if isinstance(entry, dict) else None
        pool = offered + [choice for choice in all_options if choice not in offered]
        if not pool:
            # Older engine catalogues may omit resolution metadata. Preserve its documented
            # API default explicitly; /resolve remains the authority and validates the pair.
            pool = ["720p"]
        preferred = default if default in pool else "720p" if "720p" in pool else pool[0]
        candidates = [preferred, *(choice for choice in pool if choice != preferred)]

    last_error: ToolError | None = None
    for index, choice in enumerate(candidates):
        try:
            quote = _dict_result(await _api(
                backend, "GET", "/api/resolve", params={**params, "resolution": choice},
            ))
            quote.setdefault("format", fmt)
            quote.setdefault("resolution", choice)
            return quote
        except ToolError as exc:
            message = str(exc).lower()
            if asked or index == len(candidates) - 1 or not (
                "resolution" in message and ("offer" in message or "support" in message)
            ):
                raise
            last_error = exc
    if last_error:
        raise last_error
    raise _fail("invalid_input", "No supported video resolution is available.")


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
        raise _fail(
            "ambiguous_name",
            f"Several {kind}s match {ref!r}: {listed}. Ask the user which one (don't show the ids)."
        )
    if not items:
        raise _fail("no_match", f"There is no {kind} yet.")
    raise _fail("no_match", f"No {kind} matches {ref!r}. Recent ones: {listed}. Ask the user which one (don't show the ids).")


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
