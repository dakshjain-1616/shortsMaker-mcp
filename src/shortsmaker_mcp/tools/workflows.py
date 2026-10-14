"""Autopilot series tools: recurring video generation and publishing.

The backend and its API call these "workflows"; the product and these tools call them series.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID
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
    _api,
    _bounded_text,
    _clean_tags,
    _dedupe_ids,
    _dict_result,
    _items,
    _optional_params,
    _require_confirmation,
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


async def _priced_defaults(backend: BackendClient, defaults: dict[str, Any], theme: str | None) -> None:
    """Price one video with these settings now, so bad settings fail here, not silently later."""
    await _api(backend, "GET", "/api/resolve", params=_optional_params(
        mode=defaults.get("mode"),
        total_length=defaults.get("total_length"),
        format=defaults.get("format"),
        resolution=defaults.get("resolution"),
        audio_mode=defaults.get("audio_mode"),
        niche=theme,
    ))


def _workflow_create_body(
    *,
    name: str,
    theme: str,
    connection_ids: list[UUID],
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
        title="List Autopilot series",
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
        result = await _api(backend, "GET", "/api/workflows")
        listed = _running_first(result if isinstance(result, list) else [], status)
        return {**listed, "shown_as_gallery": shown_as_gallery(ctx)}

    @server.tool(
        title="Get Autopilot series",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_series(series_id: WorkflowId) -> dict[str, Any]:
        """Use when the user asks about one series: when the next video is, why it paused, or its settings.

        Do: quote ``last_error`` when it is paused; read its slots with list_series_slots for detail.
        Don't: guess why it paused from the status alone.
        Returns: schedule, status, timezone, ``next_slot_at``, job_defaults, slot counts, last_error.
        """
        return _dict_result(await _api(backend, "GET", f"/api/workflows/{series_id}"))

    @server.tool(
        title="List series slots",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_series_slots(
        series_id: WorkflowId,
        status: Annotated[
            Literal["planned", "generating", "promoted", "failed", "skipped"] | None,
            Field(description="Optional slot status filter."),
        ] = None,
        from_time: Annotated[
            AwareDatetime | None,
            Field(description="Optional inclusive lower bound; must include a timezone."),
        ] = None,
        to_time: Annotated[
            AwareDatetime | None,
            Field(description="Optional inclusive upper bound; must include a timezone."),
        ] = None,
    ) -> dict[str, Any]:
        """Use when the user asks what a series will post next, or why one of its videos was
        skipped or failed. Each slot is one planned, generating or posted video.

        Do: show times in the series timezone; quote a failed or skipped slot's last_error.
        Don't: retry or skip slots from here; that is control_series_slot, after approval.
        Returns: ``items`` (id, slot_at, status, title, last_error; up to the 200 most recent)
        and ``count``.
        """
        params = _optional_params(
            status=status,
            **{
                "from": from_time,
                "to": to_time,
            },
        )
        return _items(await _api(backend, "GET", f"/api/workflows/{series_id}/slots", params=params))

    @server.tool(
        title="Create Autopilot series",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def create_series(
        name: Annotated[str, Field(min_length=1, max_length=120, description="Series display name.")],
        theme: Annotated[str, Field(min_length=3, max_length=2000, description="What the series is about: the brief every video is generated from.")],
        connection_ids: Annotated[
            list[UUID],
            Field(min_length=1, max_length=20, description="Owned connected accounts to publish to."),
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
        guidance: Annotated[str | None, Field(max_length=2000, description="Optional generation guidance.")] = None,
        avoid: Annotated[str | None, Field(max_length=2000, description="Optional generation exclusions.")] = None,
        variation: Annotated[bool, Field(description="Make variations of one video instead of a fresh idea each time.")] = False,
        visibility: Annotated[
            Literal["public", "unlisted", "private"],
            Field(description="Default YouTube visibility."),
        ] = "public",
        lead_minutes: Annotated[int, Field(ge=30, le=1440, description="Head start: minutes before each post time to start making the video.")] = 360,
        grace_minutes: Annotated[int, Field(ge=0, le=10080, description="Late-post window: how many minutes late a video may still be posted.")] = 360,
        ends_at: Annotated[AwareDatetime | None, Field(description="When the series ends; omit to run until stopped.")] = None,
        post_description: Annotated[str | None, Field(max_length=10_000, description="Optional shared post description.")] = None,
        post_tags: Annotated[list[str] | None, Field(max_length=100, description="Optional shared post tags.")] = None,
        job_defaults: Annotated[dict[str, Any] | None, Field(description=(
                "Video settings for every video, using create_video's keys: format, style, "
                "total_length, audio_mode, mode, resolution, narrator_voice, narration_language, music_track."
            ))] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user has approved starting an Autopilot series that makes and posts a
        new video on a schedule. Each video spends credits when it is made.

        Do: gather the choices one at a time (theme, format, video length, days, times and
        timezone, accounts from list_connections, visibility, end date), quote one video with
        get_video_price, and show a summary with videos per week and credits per week.
        job_defaults must include total_length; mode, format and resolution default to lite,
        auto and 720p.
        Don't: assume a timezone (ask), use accounts whose status is not active, or create it
        when list_connections is empty (offer connect_account first).
        Returns: the series (id, status, next_slot_at) and ``next_step``.
        Example: "post a space fact every weekday at 9am and 6pm on my channel" ->
        days_of_week=[0,1,2,3,4], times_of_day=["09:00","18:00"], timezone asked
        ("Europe/London"), job_defaults={"total_length": 30}; summary "10 videos a week x 18
        credits = 180 credits a week", then create_series(..., confirm=true).
        Example: "every morning" -> ask for the time, offering 07:00, 09:00 (recommended) or 11:00.
        """
        _check_timezone(timezone)
        _check_ends_at(ends_at)
        job_defaults = {**_JOB_DEFAULTS, **(job_defaults or {})}
        _check_length(job_defaults)
        body = _workflow_create_body(
            name=name,
            theme=theme,
            connection_ids=connection_ids,
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
            post_tags=post_tags,
            job_defaults=job_defaults,
        )
        _require_confirmation(confirm, "Creating an Autopilot series")
        await _priced_defaults(backend, job_defaults, theme)
        return _with_next(
            await _api(backend, "POST", "/api/workflows", body=body),
            "Series created; nothing is posted yet. Tell the user when the first video is due "
            "(next_slot_at) and that each video spends credits when it is made.",
        )

    @server.tool(
        title="Update Autopilot series",
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
        connection_ids: Annotated[list[UUID] | None, Field(max_length=20, description="Replacement destinations.")] = None,
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
        """Use when the user wants to change an Autopilot series, after they review the change.

        Do: pass only what changes (omitted fields stay; job_defaults is merged into the
        current settings). Before changing days, times, timezone or theme, say that planned
        videos will be re-planned.
        Don't: use it to pause, resume or stop; those have their own tools.
        Returns: the updated series and ``next_step``.
        """
        _check_timezone(timezone)
        _check_ends_at(ends_at)
        body = _optional_params(
            name=_bounded_text(name, "name", 120),
            theme=_bounded_text(theme, "theme", 2000),
            guidance=_bounded_text(guidance, "guidance", 2000),
            avoid=_bounded_text(avoid, "avoid", 2000),
            variation=variation,
            connection_ids=_dedupe_ids(connection_ids) if connection_ids is not None else None,
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
        _require_confirmation(confirm, "Updating an Autopilot series")
        if job_defaults is not None:
            # The backend replaces job_defaults whole; merge so a partial change keeps the rest.
            current = _dict_result(await _api(backend, "GET", f"/api/workflows/{series_id}"))
            body["job_defaults"] = {**_JOB_DEFAULTS, **(current.get("job_defaults") or {}), **job_defaults}
            await _priced_defaults(backend, body["job_defaults"], theme or current.get("theme"))
        return _with_next(
            await _api(backend, "PATCH", f"/api/workflows/{series_id}", body=body),
            "Updated. Check next_slot_at, or list_series_slots if the schedule changed.",
        )

    @server.tool(
        title="Pause Autopilot series",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def pause_series(series_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Use when the user wants a series to take a break without losing it.

        No new videos are made and queued posts are held until resume_series.
        Do: confirm which series by name. Don't: use stop_series for a temporary break.
        Returns: the series with status "paused" and ``next_step``.
        """
        _require_confirmation(confirm, "Pausing an Autopilot series")
        return _with_next(
            await _api(backend, "POST", f"/api/workflows/{series_id}/pause"),
            "Paused: nothing is made or posted until resume_series.",
        )

    @server.tool(
        title="Resume Autopilot series",
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
        _require_confirmation(confirm, "Resuming an Autopilot series")
        body = _optional_params(ends_at=ends_at)
        return _with_next(
            await _api(backend, "POST", f"/api/workflows/{series_id}/resume", body=body or None),
            "Running again. Tell the user when the next video is due (next_slot_at).",
        )

    @server.tool(
        title="Stop Autopilot series",
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
        _require_confirmation(confirm, "Stopping an Autopilot series")
        return _with_next(
            await _api(backend, "POST", f"/api/workflows/{series_id}/stop"),
            "Stopped; nothing more will be made or posted. resume_series can restart it.",
        )

    @server.tool(
        title="Delete Autopilot series",
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
        _require_confirmation(confirm, "Deleting an Autopilot series")
        return _with_next(
            await _api(backend, "DELETE", f"/api/workflows/{series_id}"),
            "Deleted. It no longer appears in list_series.",
        )

    @server.tool(
        title="Update series slot",
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
        return _dict_result(await _api(backend, "PATCH", f"/api/workflow-slots/{slot_id}", body=body))

    @server.tool(
        title="Skip, retry or re-idea a series slot",
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
            return _with_next(
                await _api(backend, "POST", f"/api/workflow-slots/{slot_id}/regenerate-idea", body=body),
                "New idea written. Show it to the user; update_series_slot can still change it.",
            )
        if suggested_changes is not None:
            raise ToolError("suggested_changes is only valid for regenerate_idea.")
        return _with_next(
            await _api(backend, "POST", f"/api/workflow-slots/{slot_id}/{action}"),
            "Skipped; this video will not be made." if action == "skip"
            else "Queued again, not made yet. Check list_series_slots when the user asks.",
        )
