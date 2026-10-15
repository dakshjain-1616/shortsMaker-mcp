"""Autopilot series tools: recurring video generation and publishing.

The backend and its API call these "workflows"; the product and these tools call them series.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, Field

from ..backend_client import BackendClient
from ._common import (
    ASK_EVERY_TIME,
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    Confirm,
    WorkflowId,
    WorkflowSlotId,
    _account_refs,
    _api,
    _bounded_text,
    _clean_tags,
    _dedupe_ids,
    _dict_result,
    _items,
    _label,
    _optional_params,
    _parse_time,
    _require_confirmation,
    _series_ref,
    _when,
    _with_next,
)
from .gallery import GALLERY_META, shown_as_gallery


def _workflow_days(days: list[int]) -> list[int]:
    if not days or len(days) > 7 or any(
        isinstance(day, bool) or not isinstance(day, int) or day < 0 or day > 6
        for day in days
    ):
        raise ToolError("days_of_week must contain 1-7 values from 0 (Monday) through 6 (Sunday).")
    return sorted(set(days))

def _workflow_times(times: list[str]) -> list[str]:
    if not times or len(times) > 8:
        raise ToolError("times_of_day must contain 1-8 values in HH:MM 24-hour form.")
    if not all(isinstance(time, str) for time in times):
        raise ToolError("times_of_day must contain values in HH:MM 24-hour form.")
    cleaned = sorted({time.strip() for time in times})
    if any(len(time) != 5 or time[2] != ":" or not time[:2].isdigit() or not time[3:].isdigit()
           or int(time[:2]) > 23 or int(time[3:]) > 59 for time in cleaned):
        raise ToolError("times_of_day must contain values in HH:MM 24-hour form.")
    return cleaned

# A series video is priced from these when they are made; without mode and length the backend
# cannot price it and every slot is skipped, so a series that looks active never posts.
_JOB_DEFAULTS = {"mode": "lite", "format": "auto", "resolution": "720p"}


def _check_timezone(timezone: str | None) -> None:
    if timezone is None:
        return
    try:
        ZoneInfo(timezone.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(
            f"Unknown timezone {timezone!r}. Use an IANA name like 'America/New_York' or "
            "'Europe/London'; ask the user where they are if unsure."
        ) from None


def _check_ends_at(ends_at: datetime | None) -> None:
    if ends_at is not None and ends_at <= datetime.now(UTC):
        raise ToolError("ends_at is in the past. Ask the user for a future end date, or omit it to run until stopped.")


def _check_length(defaults: dict[str, Any]) -> None:
    if not defaults.get("total_length"):
        raise ToolError(
            "job_defaults.total_length is required: the length in seconds of each video. "
            "Offer the user lengths from get_video_options(format=...)."
        )


async def _priced_defaults(backend: BackendClient, defaults: dict[str, Any], theme: str | None) -> dict[str, Any]:
    """Price one video with these settings now, so bad settings fail here, not silently later."""
    return _dict_result(await _api(backend, "GET", "/api/resolve", params=_optional_params(
        mode=defaults.get("mode"),
        total_length=defaults.get("total_length"),
        format=defaults.get("format"),
        resolution=defaults.get("resolution"),
        audio_mode=defaults.get("audio_mode"),
        niche=theme,
    )))


def _short_name(theme: str) -> str:
    """A series name from its brief: the first few words, so lists stay readable."""
    words = theme.split()
    name = " ".join(words[:6])
    return (name[:60].rstrip() + ("…" if len(words) > 6 or len(name) > 60 else "")) or "My series"


def _weekly_cost(per_video: Any, body: dict[str, Any], balance: Any) -> dict[str, Any]:
    per_week = len(body["days_of_week"]) * len(body["times_of_day"])
    out: dict[str, Any] = {"videos_per_week": per_week}
    if isinstance(per_video, int | float):
        out.update(credits_per_video=per_video, credits_per_week=per_video * per_week)
        if isinstance(balance, int | float) and per_video:
            out["balance"] = balance
            out["weeks_covered"] = round(balance / (per_video * per_week), 1)
    return out


def _workflow_create_body(
    *,
    name: str,
    theme: str,
    connection_ids: list[str],
    timezone: str,
    days_of_week: list[int],
    times_of_day: list[str],
    guidance: str | None,
    avoid: str | None,
    variation: bool,
    visibility: str,
    lead_minutes: int,
    grace_minutes: int,
    ends_at: AwareDatetime | None,
    post_description: str | None,
    post_tags: list[str] | None,
    job_defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    return _optional_params(
        name=_bounded_text(name, "name", 120, required=True),
        theme=_bounded_text(theme, "theme", 2000, required=True),
        guidance=_bounded_text(guidance, "guidance", 2000),
        avoid=_bounded_text(avoid, "avoid", 2000),
        variation=variation,
        connection_ids=_dedupe_ids(connection_ids),
        visibility=visibility,
        timezone=_bounded_text(timezone, "timezone", 64, required=True),
        days_of_week=_workflow_days(days_of_week),
        times_of_day=_workflow_times(times_of_day),
        lead_minutes=lead_minutes,
        grace_minutes=grace_minutes,
        ends_at=ends_at,
        post_description=_bounded_text(post_description, "post_description", 10_000),
        post_tags=_clean_tags(post_tags),
        job_defaults=job_defaults,
    )


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_PLATFORM = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


def _clock(hhmm: str) -> str:
    hour, minute = int(hhmm[:2]), hhmm[3:5]
    return f"{(hour % 12) or 12}:{minute} {'AM' if hour < 12 else 'PM'}"


def _zone_abbr(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).tzname() or tz
    except (ZoneInfoNotFoundError, ValueError):
        return tz


def _schedule_line(series: dict[str, Any], accounts: dict[str, str]) -> str:
    """ "Every day at 8:00 PM IST, posts to My Channel (YouTube), public" """
    days = sorted({d for d in series.get("days_of_week") or [] if isinstance(d, int) and 0 <= d <= 6})
    if not days:
        when = "No posting days set"
    elif len(days) == 7:
        when = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "Weekdays"
    elif days == [5, 6]:
        when = "Weekends"
    else:
        when = "Every " + ", ".join(_DAYS[d] for d in days)
    times = [_clock(t) for t in series.get("times_of_day") or [] if isinstance(t, str) and len(t) >= 5]
    if times:
        when += " at " + " and ".join(times) + (f" {_zone_abbr(series.get('timezone'))}" if series.get("timezone") else "")
    names = [accounts.get(str(c), "an account that is no longer connected") for c in series.get("connection_ids") or []]
    line = when + (f", posts to {', '.join(names)}" if names else "")
    return line + (f", {series['visibility']}" if series.get("visibility") else "")


def _account_names(connections: Any) -> dict[str, str]:
    return {
        str(c.get("id")): f"{c.get('account_name')} ({_PLATFORM.get(c.get('platform'), c.get('platform'))})"
        for c in connections if isinstance(c, dict)
    } if isinstance(connections, list) else {}


def _series_view(series: dict[str, Any], accounts: dict[str, str]) -> dict[str, Any]:
    """Add the plain-language fields: status label, schedule line and next video time."""
    view = {**series, "status_label": _label(series.get("status")), "schedule": _schedule_line(series, accounts)}
    if series.get("next_slot_at") and series.get("status") == "active":
        view["next_video"] = _when(series["next_slot_at"], series.get("timezone"))
    return view


async def _live_view(backend: BackendClient, series: Any) -> tuple[dict[str, Any], bool]:
    """Only the series list computes next_slot_at, counts and held_posts; every other response
    returns them empty. Merge them in from the list so the user hears the real next time.
    Also says whether the series was in the list at all."""
    series = _dict_result(series)
    listed, connections = await asyncio.gather(
        _api(backend, "GET", "/api/workflows"), _api(backend, "GET", "/api/connections"),
    )
    match = next(
        (item for item in listed if isinstance(item, dict) and item.get("id") == series.get("id")), None,
    ) if isinstance(listed, list) else None
    if match:
        series = {**series, **{k: match[k] for k in ("next_slot_at", "counts", "held_posts") if k in match}}
    return _series_view(series, _account_names(connections)), match is not None


async def _live(backend: BackendClient, series: Any) -> dict[str, Any]:
    return (await _live_view(backend, series))[0]


async def _first_slot(backend: BackendClient, series: dict[str, Any], tries: int = 5) -> dict[str, Any]:
    """A new series has an empty calendar until the scheduler's next tick (seconds); wait up to
    ~16s for the first planned video so the user hears when it is, not "nothing planned"."""
    view, listed = await _live_view(backend, series)
    for _ in range(tries - 1):
        if view.get("next_video") or not listed:
            break
        await asyncio.sleep(4)
        view, listed = await _live_view(backend, series)
    return view


# Fields whose change makes the backend delete and re-plan every planned video (_replan_needed).
_REPLAN_FIELDS = {"timezone", "days_of_week", "times_of_day", "lead_minutes", "ends_at", "theme", "guidance", "avoid", "variation"}
_FIELD_WORDS = {
    "name": "name", "theme": "topic", "guidance": "guidance", "avoid": "what to avoid",
    "variation": "variations", "connection_ids": "where it posts", "visibility": "who can see it",
    "timezone": "timezone", "days_of_week": "days", "times_of_day": "time", "ends_at": "end date",
    "lead_minutes": "head start", "grace_minutes": "late-post window", "post_description": "post description",
    "post_tags": "post tags", "job_defaults": "video settings",
}


def _same(field: str, old: Any, new: Any) -> bool:
    if field == "ends_at":
        return _parse_time(old) == _parse_time(new)
    if isinstance(old, list) and isinstance(new, list):
        return sorted(map(str, old)) == sorted(map(str, new))
    return old == new


def _changes(current: dict[str, Any], body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each field that really changes, with its previous value for undo."""
    return {
        key: {"from": current.get(key), "to": value}
        for key, value in body.items() if not _same(key, current.get(key), value)
    }


def _show(field: str, value: Any, accounts: dict[str, str], tz: str | None) -> str:
    """A field's value as a person reads it."""
    if value in (None, "", []):
        return "none"
    if field == "times_of_day":
        return " and ".join(_clock(t) for t in value)
    if field == "days_of_week":
        line = _schedule_line({"days_of_week": value}, {})
        return line[0].lower() + line[1:]
    if field == "connection_ids":
        return ", ".join(accounts.get(str(c), "an old account") for c in value)
    if field == "ends_at":
        return _when(value if isinstance(value, str) else value.isoformat(), tz)
    if field == "job_defaults" and isinstance(value, dict):
        return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in value.items() if k not in _JOB_DEFAULTS or v != _JOB_DEFAULTS[k])
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value)
    return f'"{text[:80]}…"' if len(text) > 80 else f'"{text}"' if isinstance(value, str) else text


def _series_summary(view: dict[str, Any]) -> str:
    text = f"\"{view.get('name') or 'Your series'}\" is {view['status_label'].lower()}: {view['schedule']}."
    if view.get("next_video"):
        text += f" Next video {view['next_video']}."
    if view.get("status") == "paused" and view.get("last_error"):
        text += f" {view['last_error']}"
    return text


_LIFECYCLE_ORDER = {"active": 0, "paused": 1, "stopped": 2}


def _lifecycle(series: dict[str, Any], now: datetime) -> str:
    """Same rule as the web app's lifecycleOf: an active series past its end date has stopped."""
    ends_at = series.get("ends_at")
    if series.get("status") == "active" and ends_at:
        try:
            if datetime.fromisoformat(ends_at) <= now:
                return "stopped"
        except (TypeError, ValueError):
            pass
    return series.get("status") or "stopped"


def _running_first(series: list[dict[str, Any]], status: str | None) -> dict[str, Any]:
    """Running series first, then paused; stopped ones only when asked for, as a count otherwise."""
    now = datetime.now(UTC)
    for item in series:
        item["status"] = _lifecycle(item, now)
    # Two stable sorts: newest first, then by lifecycle, so each group stays newest first.
    series = sorted(series, key=lambda item: item.get("created_at") or "", reverse=True)
    series.sort(key=lambda item: _LIFECYCLE_ORDER.get(item["status"], 3))
    if status:
        return {"series": [item for item in series if item["status"] == status]}
    shown = [item for item in series if item["status"] != "stopped"]
    return {"series": shown, "stopped_count": len(series) - len(shown)}


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="See my series",
        annotations=READ_ONLY,
        meta=GALLERY_META,
        structured_output=True,
    )
    async def list_series(
        status: Annotated[
            Literal["active", "paused", "stopped"] | None,
            Field(description="Only series in this state. Omit to get running and paused series."),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Use when the user asks about their series, Autopilot or workflows (all the same
        thing): recurring schedules that make and post videos.

        Do: mention ``stopped_count`` in a few words; for a paused series, quote its last_error.
        Don't: list stopped series unless asked (pass status="stopped" then).
        Returns: ``series`` (running first, then paused) with schedule, ``next_slot_at`` and
        slot counts by status, plus ``stopped_count``.
        """
        # Filter here, not in the backend: an "active" series past its end date counts as stopped.
        result, connections = await asyncio.gather(
            _api(backend, "GET", "/api/workflows"), _api(backend, "GET", "/api/connections"),
        )
        listed = _running_first(result if isinstance(result, list) else [], status)
        accounts = _account_names(connections)
        listed["series"] = [_series_view(item, accounts) for item in listed["series"]]
        if listed["series"]:
            summary = " ".join(_series_summary(view) for view in listed["series"][:5])
        else:
            summary = "No series yet. Want to start one that posts a new video on a schedule?"
        if listed.get("stopped_count"):
            summary += f" ({listed['stopped_count']} stopped not shown.)"
        return {"summary": summary, **listed, "shown_as_gallery": shown_as_gallery(ctx)}

    @server.tool(
        title="Check a series",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_series(series_id: WorkflowId) -> dict[str, Any]:
        """Use when the user asks about one series: when the next video is, why it paused, or its settings.

        Do: quote ``last_error`` when it is paused; read its slots with list_series_slots for detail.
        Don't: guess why it paused from the status alone.
        Returns: schedule, status, timezone, ``next_slot_at``, job_defaults, slot counts, last_error.
        """
        series_id = await _series_ref(backend, series_id)
        view = await _live(backend, await _api(backend, "GET", f"/api/workflows/{series_id}"))
        return {"summary": _series_summary(view), **view}

    @server.tool(
        title="See upcoming videos in a series",
        annotations=READ_ONLY,
        meta=GALLERY_META,
        structured_output=True,
    )
    async def list_series_slots(
        series_id: WorkflowId,
        status: Annotated[
            Literal["planned", "generating", "promoted", "failed", "skipped"] | None,
            Field(description="Only videos in this state (promoted = made and posted). Omit for the upcoming ones."),
        ] = None,
        from_time: Annotated[
            AwareDatetime | None,
            Field(description="Optional inclusive lower bound; must include a timezone."),
        ] = None,
        to_time: Annotated[
            AwareDatetime | None,
            Field(description="Optional inclusive upper bound; must include a timezone."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many to show.")] = 5,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Use when the user asks what a series will post next, wants to review or change its
        upcoming videos before they are made, or asks why one was skipped or failed.

        By default it shows the next few upcoming videos, soonest first, with when (in the
        series timezone), title and idea (``prompt``).
        Do: offer, for each, to keep it, edit it (update_series_slot), get a new idea
        (control_series_slot regenerate_idea) or skip it (control_series_slot skip). Quote a
        failed or skipped one's last_error.
        Don't: change anything from here without the user's approval.
        Returns: ``items`` (id, when, title, prompt, status_label, last_error), ``count`` and
        ``summary``.
        """
        series_id = await _series_ref(backend, series_id)
        upcoming = status is None and from_time is None and to_time is None
        now = datetime.now(UTC)
        params = _optional_params(
            status=status,
            # The backend returns the LATEST 200 slots in the window, so an open-ended "from now"
            # could drop the soonest ones; two weeks holds far fewer than 200 for any schedule.
            **{
                "from": now if upcoming else from_time,
                "to": now + timedelta(days=14) if upcoming else to_time,
            },
        )
        series, slots = await asyncio.gather(
            _api(backend, "GET", f"/api/workflows/{series_id}"),
            _api(backend, "GET", f"/api/workflows/{series_id}/slots", params=params),
        )
        series = _dict_result(series)
        slots = [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else []
        if upcoming:
            slots = sorted(
                (slot for slot in slots if slot.get("status") in ("planned", "generating")),
                key=lambda slot: slot.get("slot_at") or "",
            )
        rows = [
            {
                "id": slot.get("id"),
                "when": _when(slot.get("slot_at"), series.get("timezone")),
                "title": slot.get("title") or "Idea not written yet",
                "prompt": slot.get("prompt"),
                "status": slot.get("status"),
                "status_label": _label(slot.get("status")),
                **({"last_error": slot["last_error"]} if slot.get("last_error") else {}),
                **({"thumbnail": slot["thumbnail_url"]} if slot.get("thumbnail_url") else {}),
            }
            for slot in slots[:limit]
        ]
        name = series.get("name") or "your series"
        if not rows:
            summary = f"Nothing upcoming in \"{name}\" right now."
        else:
            summary = f"Next in \"{name}\": " + "; ".join(f"{r['when']}: \"{r['title']}\"" for r in rows) + "."
        out = _items(
            rows,
            "Offer to keep, edit, re-idea or skip any of them." if upcoming and rows else None,
            summary,
        )
        return {**out, "series_name": name, "shown_as_gallery": shown_as_gallery(ctx)}

    @server.tool(
        title="Start a series",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def create_series(
        theme: Annotated[str, Field(min_length=3, max_length=2000, description="What the series is about: the brief every video is generated from.")],
        connection_ids: Annotated[
            list[str],
            Field(min_length=1, max_length=20, description="Where to post: account names or platforms ('my YouTube'), or ids from list_connections."),
        ],
        timezone: Annotated[str, Field(max_length=64, description="IANA timezone, such as America/Los_Angeles.")],
        days_of_week: Annotated[
            list[int],
            Field(min_length=1, max_length=7, description="Posting days: 0 Monday through 6 Sunday."),
        ],
        times_of_day: Annotated[
            list[str],
            Field(min_length=1, max_length=8, description="Posting times in HH:MM 24-hour form."),
        ],
        visibility: Annotated[
            Literal["public", "unlisted", "private"],
            Field(description=(
                "Who can see the posts; always ask the user. Offer public (recommended), unlisted "
                "or private. Instagram posts are always public whatever this says."
            )),
        ],
        name: Annotated[str | None, Field(max_length=120, description="Short series name; omit to name it from the theme.")] = None,
        guidance: Annotated[str | None, Field(max_length=2000, description="Optional generation guidance.")] = None,
        avoid: Annotated[str | None, Field(max_length=2000, description="Optional generation exclusions.")] = None,
        variation: Annotated[bool, Field(description="Make variations of one video instead of a fresh idea each time.")] = False,
        lead_minutes: Annotated[int, Field(ge=30, le=1440, description="Head start: minutes before each post time to start making the video. Leave the default.")] = 360,
        grace_minutes: Annotated[int, Field(ge=0, le=10080, description="Late-post window: how many minutes late a video may still be posted. Leave the default.")] = 360,
        ends_at: Annotated[AwareDatetime | None, Field(description="When the series ends; omit to run until stopped.")] = None,
        post_description: Annotated[str | None, Field(max_length=10_000, description="Optional shared post description.")] = None,
        post_tags: Annotated[list[str] | None, Field(max_length=100, description="Optional shared post tags.")] = None,
        job_defaults: Annotated[dict[str, Any] | None, Field(description=(
                "Video settings for every video, using create_video's keys: format, style, "
                "total_length, audio_mode, mode, resolution, narrator_voice, narration_language, music_track."
            ))] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants a series that makes and posts a new video on a schedule. Call
        it first without confirm to get a preview, then with confirm=true after the user says yes.
        Each video spends credits when it is made.

        Do: ask four things, one at a time: what it is about, how often (Every day / Weekdays /
        Weekends), what time (and their timezone), and where to post (list_connections;
        connect_account if there are none). Then ask who can see the posts (public
        recommended). Length defaults to 30 seconds and the rest to auto unless the user wants
        to choose. Show the preview's ``summary`` (schedule, weekly cost, how long their
        credits last) and get a yes.
        Don't: assume a timezone (ask), or use accounts that need reconnecting.
        Returns: without confirm, a preview (status "needs_approval"). With confirm, the series
        with its ``schedule`` line, ``next_video`` and ``summary``.
        Example: "post a space fact every weekday at 9am on my YouTube" -> ask timezone and
        visibility, then create_series(theme="space facts", connection_ids=["my YouTube"],
        timezone="Europe/London", days_of_week=[0,1,2,3,4], times_of_day=["09:00"],
        visibility="public") for the preview; on "yes" call again with confirm=true.
        """
        _check_timezone(timezone)
        _check_ends_at(ends_at)
        job_defaults = {**_JOB_DEFAULTS, "total_length": 30, **(job_defaults or {})}
        _check_length(job_defaults)
        cleaned_theme = _bounded_text(theme, "theme", 2000, required=True)
        body = _workflow_create_body(
            name=_bounded_text(name, "name", 120) or _short_name(cleaned_theme),
            theme=theme,
            connection_ids=await _account_refs(backend, connection_ids),
            timezone=timezone,
            days_of_week=days_of_week,
            times_of_day=times_of_day,
            guidance=guidance,
            avoid=avoid,
            variation=variation,
            visibility=visibility,
            lead_minutes=lead_minutes,
            grace_minutes=grace_minutes,
            ends_at=ends_at,
            post_description=post_description,
            post_tags=_clean_tags(post_tags),
            job_defaults=job_defaults,
        )
        quote, credits, connections = await asyncio.gather(
            _priced_defaults(backend, job_defaults, theme),
            _api(backend, "GET", "/api/payments/credit-status"),
            _api(backend, "GET", "/api/connections"),
        )
        accounts = _account_names(connections)
        schedule = _schedule_line(body, accounts)
        if not confirm:
            cost = _weekly_cost(quote.get("estimated_credits"), body, _dict_result(credits).get("balance"))
            summary = f"\"{body['name']}\": {schedule}."
            if "credits_per_week" in cost:
                summary += (f" About {cost['credits_per_video']} credits a video, {cost['videos_per_week']} a week "
                            f"= {cost['credits_per_week']:,} credits a week.")
            if "weeks_covered" in cost:
                summary += f" Your {cost['balance']:,} credits cover about {cost['weeks_covered']:g} weeks."
            instagram = any(
                isinstance(c, dict) and c.get("platform") == "instagram" and str(c.get("id")) in body["connection_ids"]
                for c in (connections if isinstance(connections, list) else [])
            )
            if instagram and visibility != "public":
                summary += " Instagram posts are always public."
            return {
                "status": "needs_approval",
                "name": body["name"],
                "schedule": schedule,
                **cost,
                "summary": summary,
                "next_step": "Show the summary and ask to start it. On yes, call create_series again with the same values and confirm=true.",
            }
        created = _dict_result(await _api(backend, "POST", "/api/workflows", body=body))
        view = await _first_slot(backend, created)
        first = f" First video {view['next_video']}." if view.get("next_video") else (
            " The first video is being planned; ask me in a minute to see when it is.")
        return _with_next(
            view,
            "Series started; nothing is posted yet. Each video spends credits when it is made.",
            f"Started \"{view.get('name')}\": {view['schedule']}.{first}",
        )

    @server.tool(
        title="Change a series",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def update_series(
        series_id: WorkflowId,
        name: Annotated[str | None, Field(max_length=120, description="Replacement display name.")] = None,
        theme: Annotated[str | None, Field(max_length=2000, description="Replacement generation brief.")] = None,
        guidance: Annotated[str | None, Field(max_length=2000, description="Replacement guidance.")] = None,
        avoid: Annotated[str | None, Field(max_length=2000, description="Replacement exclusions.")] = None,
        variation: Annotated[bool | None, Field(description="Whether to generate variants.")] = None,
        connection_ids: Annotated[list[str] | None, Field(max_length=20, description="Replacement destinations: account names, platforms or ids.")] = None,
        visibility: Annotated[
            Literal["public", "unlisted", "private"] | None,
            Field(description="Replacement YouTube visibility."),
        ] = None,
        timezone: Annotated[str | None, Field(max_length=64, description="Replacement IANA timezone.")] = None,
        days_of_week: Annotated[list[int] | None, Field(max_length=7, description="Replacement posting days.")] = None,
        times_of_day: Annotated[list[str] | None, Field(max_length=8, description="Replacement posting times.")] = None,
        lead_minutes: Annotated[int | None, Field(ge=30, le=1440, description="Replacement generation lead time.")] = None,
        grace_minutes: Annotated[int | None, Field(ge=0, le=10080, description="Replacement lateness window.")] = None,
        ends_at: Annotated[AwareDatetime | None, Field(description="Replacement timezone-aware end date.")] = None,
        post_description: Annotated[str | None, Field(max_length=10_000, description="Replacement shared description.")] = None,
        post_tags: Annotated[list[str] | None, Field(max_length=100, description="Replacement shared tags.")] = None,
        job_defaults: Annotated[dict[str, Any] | None, Field(description="Replacement video settings, same keys as create_series.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to change a series. Call it first without confirm: it
        returns a before-and-after preview and warns when upcoming videos will be re-planned
        (changing the topic, days, time, timezone or end date drops planned videos and any
        edits to them). Then call again with confirm=true after the user says yes.

        Do: pass only what changes (job_defaults is merged into the current settings). Show the
        preview's ``summary``. After saving, offer the undo the summary mentions.
        Don't: use it to pause, restart or end a series; those have their own tools.
        Returns: without confirm, ``changes`` (from/to) and ``summary``. With confirm, the series
        with ``changed`` (from/to, for undo), ``next_video`` and ``summary``.
        Example: "move my space series to 9pm" -> update_series("space", times_of_day=["21:00"])
        shows "time from 8:00 PM to 9:00 PM. This re-plans your 5 upcoming videos..."; on yes,
        the same with confirm=true. "undo" -> update_series("space", times_of_day=["20:00"]).
        """
        series_id = await _series_ref(backend, series_id)
        _check_timezone(timezone)
        _check_ends_at(ends_at)
        body = _optional_params(
            name=_bounded_text(name, "name", 120),
            theme=_bounded_text(theme, "theme", 2000),
            guidance=_bounded_text(guidance, "guidance", 2000),
            avoid=_bounded_text(avoid, "avoid", 2000),
            variation=variation,
            connection_ids=await _account_refs(backend, connection_ids) if connection_ids is not None else None,
            visibility=visibility,
            timezone=_bounded_text(timezone, "timezone", 64),
            days_of_week=_workflow_days(days_of_week) if days_of_week is not None else None,
            times_of_day=_workflow_times(times_of_day) if times_of_day is not None else None,
            lead_minutes=lead_minutes,
            grace_minutes=grace_minutes,
            ends_at=ends_at,
            post_description=_bounded_text(post_description, "post_description", 10_000),
            post_tags=_clean_tags(post_tags),
            job_defaults=job_defaults,
        )
        if not body:
            raise ToolError("Provide at least one series field to update.")
        current, accounts = await asyncio.gather(
            _live(backend, await _api(backend, "GET", f"/api/workflows/{series_id}")),
            _api(backend, "GET", "/api/connections"),
        )
        accounts = _account_names(accounts)
        if job_defaults is not None:
            # The backend replaces job_defaults whole; merge so a partial change keeps the rest.
            body["job_defaults"] = {**_JOB_DEFAULTS, **(current.get("job_defaults") or {}), **job_defaults}
            await _priced_defaults(backend, body["job_defaults"], theme or current.get("theme"))
        changes = _changes(current, body)
        if not changes:
            return {"status": "unchanged", "summary": "Nothing to change: the series is already set up that way."}
        tz = body.get("timezone") or current.get("timezone")
        lines = "; ".join(
            f"{_FIELD_WORDS.get(k, k)} from {_show(k, c['from'], accounts, tz)} to {_show(k, c['to'], accounts, tz)}"
            for k, c in changes.items()
        )
        planned = (current.get("counts") or {}).get("planned") or 0
        warning = (
            f"This re-plans your {planned} upcoming video{'' if planned == 1 else 's'}; "
            "any edits you made to them are lost."
        ) if planned and _REPLAN_FIELDS & set(changes) else None
        name = current.get("name") or "your series"
        if not confirm:
            return {
                "status": "needs_approval",
                "changes": changes,
                **({"warning": warning} if warning else {}),
                "summary": f"Change \"{name}\": {lines}." + (f" {warning}" if warning else ""),
                "next_step": "Show the summary and ask. On yes, call update_series again with the same values and confirm=true.",
            }
        # Send only what really changes: resending an unchanged schedule field is harmless, but
        # sending just the diff keeps the backend's re-plan decision exactly what we warned about.
        view = await _live(backend, await _api(
            backend, "PATCH", f"/api/workflows/{series_id}", body={k: body[k] for k in changes},
        ))
        nxt = f" Next video {view['next_video']}." if view.get("next_video") else ""
        return _with_next(
            {**view, "changed": changes},
            "To undo, call update_series with each changed field's 'from' value, after the user asks.",
            f"Changed {lines}.{nxt} Say \"undo\" to switch back.",
        )

    @server.tool(
        title="Pause a series",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def pause_series(series_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Use when the user wants a series to take a break without losing it.

        No new videos are made and queued posts are held until resume_series.
        Do: confirm which series by name. Don't: use stop_series for a temporary break.
        Returns: the series with status "paused" and ``next_step``.
        """
        series_id = await _series_ref(backend, series_id)
        _require_confirmation(confirm, "Pausing an Autopilot series")
        view = await _live(backend, await _api(backend, "POST", f"/api/workflows/{series_id}/pause"))
        return _with_next(
            view,
            "Paused: nothing is made or posted until resume_series.",
            f"Paused \"{view.get('name') or 'your series'}\". Nothing is made or posted until you restart it.",
        )

    @server.tool(
        title="Restart a series",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def resume_series(
        series_id: WorkflowId,
        ends_at: Annotated[AwareDatetime | None, Field(description="Optional new timezone-aware end date.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants a paused or stopped series to run again.

        It plans new videos from now on; missed times are not made up.
        Do: if it paused itself, read last_error (often credits) and fix the cause first; ask
        for a new end date if the old one has passed.
        Don't: resume when the user cannot afford the next video; show credits first.
        Returns: the series with status "active", ``next_slot_at`` and ``next_step``.
        """
        series_id = await _series_ref(backend, series_id)
        _require_confirmation(confirm, "Resuming an Autopilot series")
        body = _optional_params(ends_at=ends_at)
        view = await _first_slot(
            backend, await _api(backend, "POST", f"/api/workflows/{series_id}/resume", body=body or None),
        )
        nxt = f" Next video {view['next_video']}." if view.get("next_video") else " The next video is being planned."
        return _with_next(
            view,
            "Running again; missed times are not made up.",
            f"\"{view.get('name') or 'Your series'}\" is running again: {view['schedule']}.{nxt}",
        )

    @server.tool(
        title="End a series",
        annotations=WRITE,
        structured_output=True,
    )
    async def stop_series(series_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Use when the user wants a series to end for good but keep its history.

        Cancels its queued posts; videos already made and posts already published are kept.
        Do: say queued posts will be cancelled and get approval.
        Don't: use it for a temporary break (pause_series) or to remove it (delete_series).
        Returns: the series with status "stopped" and ``next_step``.
        """
        series_id = await _series_ref(backend, series_id)
        _require_confirmation(confirm, "Stopping an Autopilot series")
        result = _dict_result(await _api(backend, "POST", f"/api/workflows/{series_id}/stop"))
        held = result.get("canceled_posts")
        return _with_next(
            result,
            "Stopped; nothing more will be made or posted. resume_series can restart it.",
            f"Ended \"{result.get('name') or 'your series'}\". Videos already made and posted are kept"
            + (f"; {held} waiting post{'' if held == 1 else 's'} cancelled." if held else "."),
        )

    @server.tool(
        title="Delete a series",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def delete_series(series_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Use when the user wants a series removed entirely. Cannot be undone.

        Its schedule is deleted and posts still waiting to go out are cancelled; published
        posts stay in list_posts.
        Do: name the series, say it cannot be undone, and get approval.
        Don't: delete when the user only wants it to stop; stop_series keeps the history.
        Returns: the backend's confirmation and ``next_step``.
        """
        series_id = await _series_ref(backend, series_id)
        _require_confirmation(confirm, "Deleting an Autopilot series")
        return _with_next(
            await _api(backend, "DELETE", f"/api/workflows/{series_id}"),
            "Deleted. It no longer appears in list_series.",
            "Deleted the series. Posts already published stay up.",
        )

    @server.tool(
        title="Edit an upcoming video",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def update_series_slot(
        slot_id: WorkflowSlotId,
        title: Annotated[str | None, Field(max_length=500, description="Replacement slot title.")] = None,
        prompt: Annotated[str | None, Field(min_length=3, max_length=10_000, description="Replacement video prompt.")] = None,
        overrides: Annotated[dict[str, Any] | None, Field(description="Optional generation overrides.")] = None,
        slot_at: Annotated[AwareDatetime | None, Field(description="Replacement timezone-aware posting time.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to change one upcoming video of a series: its idea, prompt,
        settings or time.

        Do: find the slot with list_series_slots and show the change first.
        Don't: edit a slot that is already generating or posted; only planned slots change.
        Returns: the updated slot.
        """
        _require_confirmation(confirm, "Updating a series slot")
        body = _optional_params(
            title=_bounded_text(title, "title", 500),
            prompt=_bounded_text(prompt, "prompt", 10_000),
            overrides=overrides,
            slot_at=slot_at,
        )
        if not body:
            raise ToolError("Provide at least one slot field to update.")
        slot = _dict_result(await _api(backend, "PATCH", f"/api/workflow-slots/{slot_id}", body=body))
        return _with_next(
            slot,
            "Saved. Only this upcoming video changed.",
            f"Saved the upcoming video \"{slot.get('title') or 'untitled'}\". The rest of the series is unchanged.",
        )

    @server.tool(
        title="Skip, redo or rethink an upcoming video",
        annotations=WRITE,
        structured_output=True,
    )
    async def control_series_slot(
        slot_id: WorkflowSlotId,
        action: Annotated[Literal["skip", "retry", "regenerate_idea"], Field(description="Slot action to perform.")],
        suggested_changes: Annotated[str | None, Field(max_length=1000, description="Optional guidance for a regenerated idea.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to skip one upcoming series video, retry a failed one, or get
        a new idea for it.

        skip: that video is not made or posted. retry: makes a failed slot again and spends
        its credits again. regenerate_idea: writes a new idea for a planned slot (guide it with
        suggested_changes).
        Do: read it with list_series_slots first and say the cost before a retry.
        Don't: pass suggested_changes for skip or retry; don't retry a slot whose account is broken.
        Returns: the updated slot and ``next_step``.
        """
        _require_confirmation(confirm, f"Series slot {action}")
        if action == "regenerate_idea":
            body = _optional_params(suggested_changes=_bounded_text(suggested_changes, "suggested_changes", 1000))
            slot = _dict_result(await _api(backend, "POST", f"/api/workflow-slots/{slot_id}/regenerate-idea", body=body))
            return _with_next(
                slot,
                "New idea written. Show it to the user; update_series_slot can still change it.",
                f"New idea: \"{slot.get('title') or 'untitled'}\". Keep it, or ask for another.",
            )
        if suggested_changes is not None:
            raise ToolError("suggested_changes is only valid for regenerate_idea.")
        return _with_next(
            await _api(backend, "POST", f"/api/workflow-slots/{slot_id}/{action}"),
            "Skipped; this video will not be made." if action == "skip"
            else "Queued again, not made yet. Check list_series_slots when the user asks.",
            "Skipped. That video won't be made or posted." if action == "skip"
            else "Making it again. It takes a few minutes.",
        )
