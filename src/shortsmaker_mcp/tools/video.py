"""Video generation, job inspection, and job control tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backend_client import BackendClient
from ..config import BILLING_URL
from ._common import (
    ASK_EVERY_TIME,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    Confirm,
    JobId,
    Niche,
    PreviewToken,
    _api,
    _bounded_text,
    _check_token,
    _clean_tags,
    _dict_result,
    _fail,
    _label,
    _optional_params,
    _parse_time,
    _preview_token,
    _require_confirmation,
    _video_ref,
    _when,
    _with_next,
    _words,
)
from .gallery import GALLERY_META, shown_as_gallery


def _resolve_params(
    *,
    mode: str,
    total_length: int,
    format: str,
    resolution: str,
    audio_mode: str | None,
    workflow: str | None,
    niche: str | None,
) -> dict[str, Any]:
    return _optional_params(
        mode=_bounded_text(mode, "mode", 32, required=True),
        total_length=total_length,
        format=_bounded_text(format, "format", 64, required=True),
        resolution=_bounded_text(resolution, "resolution", 32, required=True),
        audio_mode=_bounded_text(audio_mode, "audio_mode", 32),
        workflow=_bounded_text(workflow, "workflow", 64),
        niche=_bounded_text(niche, "niche", 10_000),
    )

def _create_params(
    *,
    niche: str,
    total_length: int,
    format: str,
    mode: str,
    resolution: str,
    style: str | None,
    audio_mode: str | None,
    narrator_voice: str | None,
    narration_language: str | None,
    music_track: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    return _optional_params(
        niche=_bounded_text(niche, "niche", 10_000, required=True),
        total_length=total_length,
        format=_bounded_text(format, "format", 64, required=True),
        mode=_bounded_text(mode, "mode", 32, required=True),
        resolution=_bounded_text(resolution, "resolution", 32, required=True),
        style=_bounded_text(style, "style", 200),
        audio_mode=_bounded_text(audio_mode, "audio_mode", 32),
        narrator_voice=_bounded_text(narrator_voice, "narrator_voice", 64),
        narration_language=_bounded_text(narration_language, "narration_language", 64),
        music_track=_bounded_text(music_track, "music_track", 128),
        idempotency_key=_bounded_text(idempotency_key, "idempotency_key", 200, required=True),
    )

_IN_PROGRESS = {"queued", "pending", "processing", "paused"}
_DONE = {"ready", "complete"}


def _thumbnail(job: dict[str, Any]) -> str | None:
    thumbs = job.get("thumbnail_urls") if isinstance(job.get("thumbnail_urls"), dict) else {}
    return thumbs.get("after") or thumbs.get("mid") or thumbs.get("before")


def _auto_key(body: dict[str, Any]) -> str:
    """The same settings always give the same key, so a retried call after a timeout returns the
    first video instead of charging again. The backend keeps keys forever, so asking for the very
    same video later replays it too; the result says so and a new key makes a deliberate copy."""
    settings = {key: value for key, value in body.items() if key != "idempotency_key"}
    return "auto-" + hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:40]


# What the engine resolves and the job really runs with; confirm sends these exactly, so a
# re-resolve on the backend (Auto is a model's choice) can't pick something other than the preview.
_PINNED = ("format", "workflow", "total_length", "audio_mode", "resolution")


def _effective(body: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """The settings that will really run: the request overlaid with what the engine resolved."""
    settings = {key: value for key, value in body.items() if key != "idempotency_key"}
    for key in _PINNED:
        value = quote.get(key)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value not in (None, ""):
            settings[key] = value
    return settings


def _pinned(body: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """The create body carrying the approved settings; the key stays the request's own."""
    pinned = {**settings, "idempotency_key": body["idempotency_key"]}
    if not isinstance(pinned.get("total_length"), int):
        # The API takes whole seconds; a fractional resolved length is re-resolved from the ask.
        pinned["total_length"] = body["total_length"]
    return pinned


def _job_summary(job: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    """A video as a person sees it. Lists get a slim row (one thumbnail, link only when ready);
    ``full`` adds the topic, description, tags and dates for a single-video read."""
    status = job.get("status")
    topic = job.get("niche") or ""
    row: dict[str, Any] = {
        "id": job.get("id"),
        "title": job.get("title") or (topic[:80] + ("…" if len(topic) > 80 else "")) or "Untitled video",
        "status": status,
        "status_label": _label(status),
    }
    if status in _IN_PROGRESS:
        row["progress_pct"] = round((job.get("progress") or 0) * 100)
        if job.get("active_stage"):
            row["active_stage"] = job["active_stage"]
    for key in ("format", "credits_cost"):
        if job.get(key) is not None:
            row[key] = job[key]
    if job.get("created_at"):
        row["created"] = _when(job["created_at"])
    if _thumbnail(job):
        row["thumbnail"] = _thumbnail(job)
    if status in _DONE and job.get("video_url"):
        row["video_url"] = job["video_url"]
    if full:
        for key in ("niche", "description", "error", "updated_at"):
            if job.get(key):
                row[key] = job[key]
        if isinstance(job.get("tags"), list):
            row["tags"] = job["tags"][:15]
    return row


def _videos_summary(videos: list[dict[str, Any]], total: int, hidden: dict[str, int]) -> str:
    if not videos:
        text = "No more videos past this point." if total else "No videos here yet."
    else:
        counts: dict[str, int] = {}
        for row in videos:
            counts[row["status_label"]] = counts.get(row["status_label"], 0) + 1
        parts = ", ".join(f"{n} {label.lower()}" for label, n in counts.items())
        text = f"Showing {len(videos)} of {total} video{'' if total == 1 else 's'}: {parts}."
    hidden_total = sum(hidden.values())
    if hidden_total:
        text += f" {hidden_total} that didn't finish or were cancelled are hidden."
    return text


def _video_line(row: dict[str, Any]) -> str:
    extra = f" ({row['progress_pct']}%)" if "progress_pct" in row and row["status"] != "paused" else ""
    return f"\"{row['title']}\": {row['status_label']}{extra}"


_SOUND = {"dialogue": "dialogue", "narration": "narration", "ambient": "natural sound", "silent": "music only"}


def _adjustments(requested: dict[str, Any], quote: dict[str, Any]) -> list[str]:
    """What the engine quietly changed from what was asked, in words the user understands."""
    changes = []
    asked, got = quote.get("requested_length") or requested.get("total_length"), quote.get("total_length")
    if isinstance(asked, int | float) and isinstance(got, int | float) and asked != got:
        changes.append(f"Length changed from {asked:g}s to {got:g}s, the closest this format allows.")
    asked, got = requested.get("audio_mode"), quote.get("audio_mode")
    if asked and got and asked != got:
        changes.append(
            f"Sound changed from {_SOUND.get(asked, asked)} to {_SOUND.get(got, got)}, "
            "because this format can't do the first."
        )
    asked, got = requested.get("resolution"), quote.get("resolution")
    if asked and got and asked != got:
        changes.append(f"Resolution changed from {asked} to {got}.")
    return changes


def _recent_failures(jobs: Any, now: datetime) -> int:
    """Videos that failed in the last hour: a sign the next one will fail too."""
    count = 0
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or job.get("status") != "failed":
            continue
        when = _parse_time(job.get("updated_at") or job.get("created_at"))
        if when and (now - when).total_seconds() < 3600:
            count += 1
    return count


async def _quote(
    backend: BackendClient, params: dict[str, Any], body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The price and settings the engine will really use, checked against what the user asked.

    Also checks style/voice/music/resolution/sound against the catalogue (``body``, the create
    settings, defaults to ``params``) so a bad choice fails here, before any credits are held.
    """
    now = datetime.now(UTC)
    config, quote, credits, jobs = await asyncio.gather(
        _api(backend, "GET", "/api/config"),
        _api(backend, "GET", "/api/resolve", params=params),
        _api(backend, "GET", "/api/payments/credit-status"),
        # Only the last hour matters for the failure warning; skip the other 200 rows' signed URLs.
        _api(backend, "GET", "/api/jobs", params={"updated_since": (now - timedelta(hours=1)).isoformat()}),
    )
    _check_choices(config, body or params)
    quote = {key: value for key, value in _dict_result(quote).items() if key != "estimated_cost"}
    adjustments = _adjustments(params, quote)
    if adjustments:
        quote["adjustments"] = adjustments
    balance = credits.get("balance") if isinstance(credits, dict) else None
    needed = quote.get("estimated_credits")
    if isinstance(balance, int | float) and isinstance(needed, int | float):
        quote.update(balance=balance, can_afford=balance >= needed)
        if balance < needed:
            summary = (f"This video costs {needed:,} credits but you have {balance:,}. "
                       f"You can add credits at {BILLING_URL}.")
        else:
            count = int(balance // needed) if needed else 0
            summary = f"This video costs {needed:,} credits. You have {balance:,}, enough for about {count} like this."
        quote["summary"] = " ".join([summary, *adjustments])
    failures = _recent_failures(jobs, now)
    if failures >= 2:
        quote["warning"] = (
            f"{failures} of your videos failed in the last hour. Credits come back when a video fails, "
            "but this one will likely fail too. It may be better to wait a little."
        )
    return quote


def _plain_choices(config: Any) -> dict[str, list[dict[str, str]]]:
    """Formats and quality levels as the user should hear them: a label and one plain line."""
    config = config if isinstance(config, dict) else {}

    def lines(items: Any, text_key: str) -> list[dict[str, str]]:
        return [
            {"id": str(item["id"]), "say": f"{item.get('label') or item['id']}: {item.get(text_key) or ''}".rstrip(": ")}
            for item in items if isinstance(item, dict) and item.get("id")
        ] if isinstance(items, list) else []

    return {"formats": lines(config.get("formats"), "desc"), "quality": lines(config.get("modes"), "description")}


def _strip_previews(value: Any) -> Any:
    """Drop presigned audio preview links: long, expiring, and nothing a chat can play."""
    if isinstance(value, list):
        return [_strip_previews(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_previews(item) for key, item in value.items() if key != "preview_url"}
    return value


_CONTROL_NEXT = {
    "cancel": "Cancel requested; the held credits are refunded once it stops. Confirm with get_video when the user asks.",
    "pause": "Pause requested; the credits stay held. retry_video resumes it for free.",
}


# Cancel or pause on a running video answers "cancelling"/"pausing", then the video reads as
# "processing" until the worker stops (seconds, normally). Wait that out so the user hears the end
# state. ponytail: ~30s cap keeps well inside client timeouts; past it we say it is still stopping.
_SETTLE_TRIES, _SETTLE_POLL = 10, 3.0


async def _settled(backend: BackendClient, job_id: str) -> dict[str, Any]:
    """The video once it has stopped, or {} if it can't be read: the action itself already
    succeeded, so a failed follow-up read must not turn it into an error."""
    job: dict[str, Any] = {}
    try:
        for attempt in range(_SETTLE_TRIES):
            if attempt:
                await asyncio.sleep(_SETTLE_POLL)
            job = _dict_result(await _api(backend, "GET", f"/api/jobs/{job_id}"))
            if job.get("status") != "processing":
                break
    except ToolError:
        pass
    return job


def _control_summary(action: str, answered: Any, job: dict[str, Any]) -> str:
    cost = job.get("credits_cost")
    # The settled read wins; if it failed, the action's own answer ("cancelled", "pausing") stands.
    status = job.get("status") or answered
    if action == "cancel":
        if status == "cancelled":
            return f"Cancelled. {cost} credits returned." if cost else "Cancelled. Your credits were returned."
        return "Stopping it now. Your credits come back as soon as it stops; ask me in a minute."
    if status == "paused":
        return "Paused. Your credits stay held; say \"resume\" to carry on for free."
    return "Pausing it now; it stops in a moment. Say \"resume\" later to carry on for free."


def _details_summary(changed: dict[str, dict[str, Any]], live: Any) -> str:
    if not changed:
        return "Nothing changed: the video already had those details."
    parts = []
    for key, change in changed.items():
        if key == "title":
            parts.append(f'title from "{change["from"] or "none"}" to "{change["to"]}"')
        elif key == "visibility":
            parts.append(f"who can see it to {change['to']}")
        else:
            parts.append(f"the {key}")
    text = "Changed " + ", ".join(parts) + "."
    live = live if isinstance(live, dict) else {}
    if live.get("updated"):
        text += " Also updated on YouTube."
    for failure in live.get("failed") or []:
        if isinstance(failure, dict):
            text += f" Saved here, but on {failure.get('account_name') or 'YouTube'}: {failure.get('error')}"
    return text + ' Say "undo" to switch back.'


def _ids(options: Any) -> set[str]:
    """Ids from a config list that mixes plain strings and {"id": ...} objects."""
    if not isinstance(options, list):
        return set()
    return {str(o.get("id")) if isinstance(o, dict) else str(o) for o in options}


def _check_choices(config: Any, body: dict[str, Any]) -> None:
    """Refuse a voice, music track or style the engine does not offer, before credits are held.
    A config without a list for one of them (older engine) skips that check."""
    config = config if isinstance(config, dict) else {}
    audio = config.get("audio") if isinstance(config.get("audio"), dict) else {}
    by_format = (config.get("styles_by_format") or {}).get(body.get("format"))
    styles = by_format.get("available") if isinstance(by_format, dict) else config.get("styles")
    for key, options in (
        ("narrator_voice", audio.get("voices")),
        ("music_track", audio.get("music_tracks")),
        ("style", styles),
    ):
        valid = _ids(options)
        if body.get(key) and valid and body[key] not in valid:
            raise _fail(
                "invalid_input",
                f"{key} {body[key]!r} is not offered. Choose one of: {', '.join(sorted(valid)[:20])}. "
                "Nothing was charged."
            )
    # Resolution and sound depend on the format; "auto" picks a format later, so only check a known one.
    for key, by_format, word in (
        ("resolution", config.get("resolutions_by_format"), "resolution"),
        ("audio_mode", audio.get("by_format"), "sound"),
    ):
        entry = (by_format or {}).get(body.get("format")) if isinstance(by_format, dict) else None
        valid = _ids(entry.get("available")) if isinstance(entry, dict) else set()
        if body.get(key) and valid and body[key] not in valid:
            raise _fail(
                "invalid_input",
                f"This format doesn't offer {word} {body[key]!r}; it offers {', '.join(sorted(valid))}. "
                "Nothing was charged."
            )


# The web app's dashboard order (app/lib/settled.ts): videos still being made first.
_STATUS_ORDER = {"processing": 0, "pending": 1, "paused": 2, "ready": 3, "complete": 4, "failed": 5, "cancelled": 6}
_HIDDEN_BY_DEFAULT = {"failed", "cancelled"}
_RETRYABLE = {"failed", "cancelled", "paused"}
_SHOW = {
    "in_progress": {"processing", "pending", "paused"},
    "ready": {"ready", "complete"},
    "failed": {"failed"},
    "cancelled": {"cancelled"},
}


def _working_first(jobs: list[dict[str, Any]], show: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter to ``show`` and sort in-progress first, newest first; count what default hides."""
    jobs = sorted(jobs, key=lambda job: job.get("created_at") or "", reverse=True)
    jobs.sort(key=lambda job: _STATUS_ORDER.get(job.get("status"), 7))
    if show == "all":
        return jobs, {}
    if show != "default":
        return [job for job in jobs if job.get("status") in _SHOW[show]], {}
    # Exclude rather than include, so a status the backend adds later still shows up.
    shown = [job for job in jobs if job.get("status") not in _HIDDEN_BY_DEFAULT]
    hidden: dict[str, int] = {}
    for job in jobs:
        if job.get("status") in _HIDDEN_BY_DEFAULT:
            hidden[job["status"]] = hidden.get(job["status"], 0) + 1
    return shown, hidden


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="See video choices",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_video_options(
        format: Annotated[str, Field(max_length=64, description="Format to list valid lengths for.")] = "auto",
        mode: Annotated[str, Field(max_length=32, description="Mode to list valid lengths for.")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Resolution to list valid lengths for.")] = "720p",
        niche: Annotated[str | None, Field(max_length=10_000, description="The topic, so auto lists the lengths of the format it would pick.")] = None,
    ) -> dict[str, Any]:
        """Use when the user wants to make a video or series and you need the real choices:
        formats, styles per format, voices, languages, music, modes, resolutions, and the valid
        lengths for one format. Read-only; spends nothing.

        Do: keep it to two questions for most people: the topic, then quality (``choices.quality``,
        the first one recommended). Offer format, length, sound or style only when the user wants
        to choose; describe formats with ``choices.formats`` lines ("Action: continuous action,
        beat by beat"), never their ids. Call again with format=... after the user picks one:
        lengths depend on the format.
        Don't: paste the whole catalogue, or invent ids (voices, styles, music) that are not here.
        Returns: ``choices`` (plain lines for formats and quality), ``config`` (formats,
        styles_by_format, audio.voices, audio.music_tracks, ...) and ``lengths`` for the given
        format. Pass ids exactly as returned.
        Example: "what kinds of videos can you make?" -> get_video_options(), then offer the 3-4
        formats that fit their topic plus Auto, one short line each.
        Example: user picks the pov format -> get_video_options(format="pov", niche=topic) and
        offer 2-4 of the returned lengths.
        """
        config, lengths = await asyncio.gather(
            _api(backend, "GET", "/api/config"),
            _api(
                backend,
                "GET",
                "/api/lengths",
                params={
                    "format": _bounded_text(format, "format", 64, required=True),
                    "mode": _bounded_text(mode, "mode", 32, required=True),
                    "resolution": _bounded_text(resolution, "resolution", 32, required=True),
                    **_optional_params(niche=_bounded_text(niche, "niche", 10_000)),
                },
            ),
        )
        config = _strip_previews(config)
        return {
            "choices": _plain_choices(config),
            "config": config,
            "lengths": lengths,
        }

    @server.tool(
        title="Check video price",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_video_price(
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")],
        mode: Annotated[str, Field(max_length=32, description="Generation mode, usually lite.")] = "lite",
        format: Annotated[str, Field(max_length=64, description="Format id from get_video_options; auto picks one.")] = "auto",
        resolution: Annotated[str, Field(max_length=32, description="Output resolution.")] = "720p",
        audio_mode: Annotated[str | None, Field(max_length=32, description="Optional sound choice, such as narration.")] = None,
        workflow: Annotated[str | None, Field(max_length=64, description="Optional workflow name.")] = None,
        niche: Annotated[str | None, Field(max_length=10_000, description="The topic; lets auto pick the real format.")] = None,
    ) -> dict[str, Any]:
        """Use when the user wants to know what a video costs, before making it. Never holds credits.

        Do: pass the topic as ``niche`` so Auto resolves to the real format and price, and use
        the same settings you will pass to create_video.
        Don't: ask for approval when ``can_afford`` is false; show the price, the balance and
        the billing link from the server instructions instead.
        Returns: the settings the video will really use, ``estimated_credits``, ``balance``,
        ``can_afford``, ``adjustments`` (what the engine changed from the request: tell the user),
        an optional ``warning`` (recent failures) and a ``summary`` to pass on.
        """
        params = _resolve_params(
            mode=mode,
            total_length=total_length,
            format=format,
            resolution=resolution,
            audio_mode=audio_mode,
            workflow=workflow,
            niche=niche,
        )
        return await _quote(backend, params)

    @server.tool(
        title="List my videos",
        annotations=READ_ONLY,
        meta=GALLERY_META,
        structured_output=True,
    )
    async def list_videos(
        show: Annotated[
            Literal["default", "in_progress", "ready", "failed", "cancelled", "all"],
            Field(description=(
                "Which videos: default is everything except failed and cancelled. Use failed or "
                "cancelled only when the user asks about those."
            )),
        ] = "default",
        query: Annotated[str | None, Field(max_length=200, description="Words from the title or topic, e.g. 'cooking'.")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Videos per page.")] = 6,
        offset: Annotated[int, Field(ge=0, description="Videos to skip; pass next_offset from the previous page.")] = 0,
        updated_since: Annotated[datetime | None, Field(description="Only videos updated after this timezone-aware timestamp.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Use when the user wants to see their videos or find one ("the cooking one").
        Videos still being made come first, then the newest finished ones.

        Do: mention the ``hidden`` count (failed and cancelled) in one short line; when several
        match a query, ask which one. Fetch ``next_offset`` only if the user wants more.
        Don't: call it in a loop to watch progress, or list hidden videos unless asked.
        Returns: ``videos`` (id, status, progress_pct, title, links when ready), ``total``,
        ``hidden``, ``next_offset``. Covers the 200 most recent videos. With ``updated_since``
        it is a change feed, oldest change first; advance by the last video's ``updated_at``.
        Example: "show my last video" -> list_videos(show="all", limit=1); if it failed, say so
        and offer a retry.
        Example: "post the cooking one" -> list_videos(query="cooking"); if several match, ask
        which one, offering each title and date as an option.
        """
        result = await _api(
            backend,
            "GET",
            "/api/jobs",
            params=_optional_params(updated_since=updated_since),
        )
        jobs = result if isinstance(result, list) else []
        words = _words(query) if query else []
        if words:
            jobs = [job for job in jobs if all(
                word in f"{job.get('title') or ''} {job.get('niche') or ''}".lower() for word in words
            )]
        hidden: dict[str, int] = {}
        if updated_since is None:
            jobs, hidden = _working_first(jobs, show)
        elif show == "default":
            jobs = [job for job in jobs if job.get("status") not in _HIDDEN_BY_DEFAULT]
        elif show != "all":
            jobs = [job for job in jobs if job.get("status") in _SHOW[show]]
        end = offset + limit
        # The change feed needs updated_at to advance; plain browsing does not.
        videos = [_job_summary(job, full=updated_since is not None) for job in jobs[offset:end]]
        return {
            "summary": _videos_summary(videos, len(jobs), hidden),
            "videos": videos,
            "total": len(jobs),
            "hidden": hidden,
            "offset": offset,
            "next_offset": end if end < len(jobs) else None,
            "shown_as_gallery": shown_as_gallery(ctx),
        }

    @server.tool(
        title="Check a video",
        annotations=READ_ONLY,
        meta=GALLERY_META,
        structured_output=True,
    )
    async def get_video(job_id: JobId, ctx: Context | None = None) -> dict[str, Any]:
        """Use when the user asks about one video: is it ready, how far along, why it failed, or
        its link.

        Do: report ``status`` as-is. Only "ready" or "complete" with ``video_url`` means done;
        pending, queued or processing means still being made (give ``progress_pct``).
        Don't: poll in a loop, or reuse an old video_url: links are temporary, read it again.
        Returns: id, status, progress_pct, active_stage, credits_cost, title, video_url,
        thumbnail_urls; a failed video has an ``error`` sentence to pass on.
        """
        job_id = await _video_ref(backend, job_id)
        result = await _api(backend, "GET", f"/api/jobs/{job_id}")
        row = _job_summary(_dict_result(result), full=True)
        summary = _video_line(row) + "."
        if row["status"] in _DONE and row.get("video_url"):
            summary += " It's ready to watch or post."
        elif row["status"] in _DONE:
            summary += " Ready, but its link isn't available right now."
        elif row["status"] in _IN_PROGRESS - {"paused"}:
            summary += " Videos take a few minutes; I'll check again when you ask."
        return {**row, "summary": summary, "shown_as_gallery": shown_as_gallery(ctx)}

    @server.tool(
        title="Make a video",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def create_video(
        niche: Niche,
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")],
        format: Annotated[str, Field(max_length=64, description="Format id from get_video_options; auto picks one.")] = "auto",
        mode: Annotated[str, Field(max_length=32, description="Quality: a mode id from get_video_options (lite is Standard).")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Output resolution.")] = "720p",
        style: Annotated[str | None, Field(max_length=200, description="Optional visual style for this format; omit for auto.")] = None,
        audio_mode: Annotated[str | None, Field(max_length=32, description="Optional sound choice, such as narration.")] = None,
        narrator_voice: Annotated[str | None, Field(max_length=64, description="Optional narrator voice.")] = None,
        narration_language: Annotated[str | None, Field(max_length=64, description="Optional narration language.")] = None,
        music_track: Annotated[str | None, Field(max_length=128, description="Optional music track identifier.")] = None,
        preview_token: PreviewToken = None,
        idempotency_key: Annotated[str | None, Field(min_length=1, max_length=200, description=(
            "Leave out: retries of the same video are recognized automatically and never charge "
            "twice. Pass a new made-up key only when the user deliberately wants a second copy "
            "of a video they already made with exactly the same settings."
        ))] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to make one video. Call it first without confirm to get a
        preview, then again with confirm=true after the user says yes. Holds credits.

        Do: ask the topic, then offer quality (mode labels; Standard recommended). Ask about
        format, length, sound, voice or style only if the user wants to choose; otherwise
        leave the defaults (auto). Show the preview's ``summary``, ``adjustments`` and any
        ``warning``, get a yes, then call again with the same settings, confirm=true and the
        preview's ``preview_token``. For several videos, preview the total first. To try a
        failed video again use retry_video, not this.
        Don't: say the video is ready (it takes a few minutes), poll in a loop, or invent
        voice/style/music ids (they are refused before any charge).
        Returns: without confirm, a preview (status "needs_approval") with ``settings``: what
        will really run, which confirm checks and sends exactly. With confirm, the video
        (status_label "In line" = accepted, NOT finished), ``credits_cost`` (the exact charge),
        ``replayed`` when this exact video already existed (no new charge), and ``summary``.
        Example: "make a 30s video about the Roman empire" -> create_video(niche, total_length=30)
        shows "18 credits. You have 120, enough for about 6 like this."; on "yes" ->
        create_video(niche, total_length=30, preview_token="p-...", confirm=true).
        Example: the call timed out -> call again with the same settings; it never charges twice.
        """
        body = _create_params(
            niche=niche,
            total_length=total_length,
            format=format,
            mode=mode,
            resolution=resolution,
            style=style,
            audio_mode=audio_mode,
            narrator_voice=narrator_voice,
            narration_language=narration_language,
            music_track=music_track,
            idempotency_key=idempotency_key or "pending",
        )
        if idempotency_key is None:
            body["idempotency_key"] = _auto_key(body)
        params = _resolve_params(
            mode=mode, total_length=total_length, format=format, resolution=resolution,
            audio_mode=audio_mode, workflow=None, niche=niche,
        )
        quote = await _quote(backend, params, body)
        settings = _effective(body, quote)
        token = _preview_token({**settings, "credits": quote.get("estimated_credits")})
        if not confirm:
            return {
                "status": "needs_approval",
                **quote,
                "settings": settings,
                "preview_token": token,
                "next_step": (
                    "Show the user the summary (and any adjustments or warning) and ask to go ahead. "
                    "On yes, call create_video again with the same settings, confirm=true and "
                    f"preview_token={token!r}."
                ),
            }
        _check_token(preview_token, token, "create_video")
        result = _dict_result(await _api(backend, "POST", "/api/jobs", body=_pinned(body, settings)))
        # A new video comes back as {id, status, credits_cost}; an existing one for the same key
        # comes back whole (with its topic): that is a replay, nothing new was made or charged.
        replayed = "niche" in result
        if replayed and result.get("niche") != body["niche"]:
            raise _fail(
                "invalid_input",
                f"This idempotency_key was already used for another video ({result.get('niche')!r}, "
                f"id {result.get('id')}); nothing new was made. Use a new key for a new video."
            )
        row = _job_summary(result)
        if replayed and row["status"] in _HIDDEN_BY_DEFAULT:
            return _with_next(
                {**row, "replayed": True},
                "Nothing new was made. To try this video again, call retry_video with its id (it "
                "previews the price first).",
                f"You tried this exact video before ({row.get('created') or 'earlier'}) and it didn't "
                "finish, so nothing new was charged. Want me to try it again?",
            )
        if replayed:
            return _with_next(
                {**row, "replayed": True},
                "Nothing new was made. If the user wants another copy anyway, call again with a "
                "new idempotency_key (after a new approval).",
                f"You already made this exact video ({row.get('created') or 'earlier'}), so nothing "
                f"new was charged. It's {row['status_label'].lower()}.",
            )
        return _with_next(
            row,
            "Queued, not ready: a video takes a few minutes. Check with get_video when the user "
            "asks; do not poll in a loop.",
            f"Your video is in line to be made ({row.get('credits_cost', quote.get('estimated_credits'))} credits). "
            "It takes a few minutes; ask me to check on it any time.",
        )

    @server.tool(
        title="Stop or pause a video",
        annotations=WRITE,
        structured_output=True,
    )
    async def control_video(
        job_id: JobId,
        action: Annotated[Literal["cancel", "pause"], Field(description="Job action to perform.")],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to stop or pause one video that is being made, after approval.

        cancel: stops it and refunds the held credits. pause: stops it but keeps the hold so
        retry_video resumes it without paying again.
        Do: read it with get_video first. To resume, or to try a failed one again, use retry_video.
        Don't: cancel or pause a ready video (it is refused).
        Returns: the new status, the video, ``summary`` and ``next_step``. Both wait up to ~30s
        for the video to actually stop, so the summary says whether the credits are already back.
        """
        job_id = await _video_ref(backend, job_id)
        _require_confirmation(confirm, f"Video {action}")
        try:
            result = _dict_result(await _api(backend, "POST", f"/api/jobs/{job_id}/{action}"))
        except ToolError:
            # The backend answers "not found or already finished"; say which, from the video itself.
            job = await _api(backend, "GET", f"/api/jobs/{job_id}")
            raise _fail(
                "wrong_state",
                f"This video is {_label(job.get('status')).lower()}, so there is nothing to {action}."
            ) from None
        job = await _settled(backend, job_id)
        return _with_next({**result, **_job_summary(job)} if job else result, _CONTROL_NEXT[action],
                          _control_summary(action, result.get("status"), job))

    @server.tool(
        title="Try a video again",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def retry_video(
        job_id: JobId,
        preview_token: PreviewToken = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to try a failed or cancelled video again, or resume a paused
        one. Call it first without confirm to get a preview, then with confirm=true after a yes.

        A paused video resumes where it stopped, for free. A failed or cancelled one is made
        again from the start and costs its price again (it was refunded when it stopped).
        Do: show the preview's ``summary`` (price, balance, any ``warning``) and get a yes.
        Don't: retry in a loop, or use create_video for this: the same settings bring back the
        old video instead of making it again.
        Returns: without confirm, a preview (status "needs_approval", ``credits``,
        ``preview_token``). With confirm, the video, now in line to be made, and ``summary``.
        Example: "try the marble one again" -> retry_video("marble") shows "Trying it again
        costs 18 credits. You have 120." -> on yes retry_video("marble", preview_token=..., confirm=true).
        """
        job_id = await _video_ref(backend, job_id)
        now = datetime.now(UTC)
        job, credits, recent = await asyncio.gather(
            _api(backend, "GET", f"/api/jobs/{job_id}"),
            _api(backend, "GET", "/api/payments/credit-status"),
            _api(backend, "GET", "/api/jobs", params={"updated_since": (now - timedelta(hours=1)).isoformat()}),
        )
        row = _job_summary(_dict_result(job))
        status = row["status"]
        if status not in _RETRYABLE:
            raise _fail(
                "wrong_state",
                f"This video is {row['status_label'].lower()}, so there is nothing to try again. "
                "Only a video that failed, was cancelled or is paused can be.",
            )
        cost = 0 if status == "paused" else int(row.get("credits_cost") or 0)
        token = _preview_token({"video": job_id, "status": status, "credits": cost})
        if confirm:
            _check_token(preview_token, token, "retry_video")
            await _api(backend, "POST", f"/api/jobs/{job_id}/retry")
            return _with_next(
                _job_summary({**_dict_result(job), "status": "pending"}),
                "Queued, not ready. Check with get_video when the user asks; do not poll in a loop.",
                ("Resumed for free. " if status == "paused" else f"Trying it again ({cost:,} credits). ")
                + "It takes a few minutes; ask me to check on it any time.",
            )
        balance = _dict_result(credits).get("balance")
        name = f"\"{row.get('title')}\"" if row.get("title") else "This video"
        if status == "paused":
            summary = f"{name} is paused. Resuming is free; it carries on where it stopped."
        elif isinstance(balance, int | float) and balance < cost:
            summary = (f"Trying {name} again costs {cost:,} credits but you have {balance:,}. "
                       f"You can add credits at {BILLING_URL}.")
        else:
            summary = f"Trying {name} again costs {cost:,} credits" + (
                f". You have {balance:,}." if isinstance(balance, int | float) else ".")
        preview = {
            **row,
            "status": "needs_approval",
            "credits": cost,
            "preview_token": token,
            "summary": summary,
            "next_step": "Show the summary and ask to go ahead. On yes, call retry_video again with "
                         f"confirm=true and preview_token={token!r}.",
        }
        if isinstance(balance, int | float):
            preview.update(balance=balance, can_afford=balance >= cost)
        failures = _recent_failures(recent, now)
        if status != "paused" and failures >= 2:
            preview["warning"] = (
                f"{failures} of your videos failed in the last hour, so this one may fail again. "
                "Credits come back if it does, but it may be better to wait a little."
            )
        return preview

    @server.tool(
        title="Edit video title and details",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def update_video_details(
        job_id: JobId,
        title: Annotated[str | None, Field(max_length=500, description="New title.")] = None,
        description: Annotated[str | None, Field(max_length=10_000, description="New description.")] = None,
        tags: Annotated[list[str] | None, Field(max_length=100, description="Replacement tag list.")] = None,
        visibility: Annotated[
            Literal["public", "unlisted", "private"] | None,
            Field(description="Optional YouTube visibility update."),
        ] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to change a video's title, description, tags or YouTube
        visibility. Free, but a write: it may also change an already published YouTube video.

        Do: show the exact new values and get approval before confirm=true. Afterwards, pass on
        the summary: it says whether the live YouTube video changed too, and offers undo.
        Don't: send fields the user did not ask to change (tags replace the whole list).
        Returns: the updated video, ``changed`` (from/to, for undo), ``live_update`` and ``summary``.
        """
        job_id = await _video_ref(backend, job_id)
        _require_confirmation(confirm, "Video details update")
        body = _optional_params(
            title=_bounded_text(title, "title", 500),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        if not body:
            raise _fail("invalid_input", "Provide at least one detail to change.")
        before = _dict_result(await _api(backend, "GET", f"/api/jobs/{job_id}"))
        result = _dict_result(await _api(backend, "PATCH", f"/api/jobs/{job_id}", body=body))
        changed = {k: {"from": before.get(k), "to": v} for k, v in body.items() if before.get(k) != v}
        if isinstance(result.get("job"), dict):
            result["job"] = _job_summary(result["job"])
        return _with_next(
            {**result, "changed": changed},
            "To undo, call update_video_details with each changed field's 'from' value, after the user asks.",
            _details_summary(changed, result.get("live_update")),
        )
