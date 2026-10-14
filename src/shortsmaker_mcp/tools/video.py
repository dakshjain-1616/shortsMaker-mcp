"""Video generation, job inspection, and job control tools."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backend_client import BackendClient
from ._common import (
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    Confirm,
    IdempotencyKey,
    JobId,
    Niche,
    _api,
    _bounded_text,
    _clean_tags,
    _dict_result,
    _optional_params,
    _require_confirmation,
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

def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "niche",
        "format",
        "progress",
        "active_stage",
        "error",
        "credits_cost",
        "title",
        "description",
        "tags",
        "has_video",
        "video_url",
        "thumbnail_urls",
        "created_at",
        "updated_at",
    )
    return {key: job.get(key) for key in keys if key in job}


# The web app's dashboard order (app/lib/settled.ts): videos still being made first.
_STATUS_ORDER = {"processing": 0, "pending": 1, "paused": 2, "ready": 3, "complete": 4, "failed": 5, "cancelled": 6}
_HIDDEN_BY_DEFAULT = {"failed", "cancelled"}
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
        title="Get video options",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_video_options(
        format: Annotated[str, Field(max_length=64, description="Format to list valid lengths for.")] = "auto",
        mode: Annotated[str, Field(max_length=32, description="Mode to list valid lengths for.")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Resolution to list valid lengths for.")] = "720p",
    ) -> dict[str, Any]:
        """Read the choices for a new video: formats, styles per format, voices, languages,
        music tracks, modes, resolutions, and the valid lengths for one format.

        Call this before asking the user anything about settings, then ask one choice at a time
        with 2-4 options (label plus a short description) and a recommended default; never paste
        the whole catalogue. Valid lengths depend on format, mode and resolution, so call again
        after the user picks a format. Read-only; spends nothing.
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
                },
            ),
        )
        return {"config": config, "lengths": lengths}

    @server.tool(
        title="Preview video price",
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
        """Quote a video's price and final settings without creating it.

        Always use this before create_video. Show the user ``estimated_credits``, the number of
        credits the job will cost. Provider cost is kept internal. The ``credits_cost`` that
        create_video returns is the exact charge. This tool never holds credits.
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
        quote = _dict_result(await _api(backend, "GET", "/api/resolve", params=params))
        return {key: value for key, value in quote.items() if key != "estimated_cost"}

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
        limit: Annotated[int, Field(ge=1, le=50, description="Videos per page.")] = 6,
        offset: Annotated[int, Field(ge=0, description="Videos to skip; pass next_offset from the previous page.")] = 0,
        updated_since: Annotated[datetime | None, Field(description="Only videos updated after this timezone-aware timestamp.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List the user's videos: ones still being made first, then the newest finished ones.

        Failed and cancelled videos are hidden by default and only counted in ``hidden``;
        mention that count in one short line, and list them only if the user asks. When
        ``next_offset`` is not null, more videos exist: fetch them only if the user wants more.
        Covers the 200 most recent videos. With ``updated_since`` the list is a change feed in
        oldest-change-first order; advance by the last video's ``updated_at``.
        """
        result = await _api(
            backend,
            "GET",
            "/api/jobs",
            params=_optional_params(updated_since=updated_since),
        )
        jobs = result if isinstance(result, list) else []
        hidden: dict[str, int] = {}
        if updated_since is None:
            jobs, hidden = _working_first(jobs, show)
        elif show == "default":
            jobs = [job for job in jobs if job.get("status") not in _HIDDEN_BY_DEFAULT]
        elif show != "all":
            jobs = [job for job in jobs if job.get("status") in _SHOW[show]]
        end = offset + limit
        return {
            "videos": [_job_summary(job) for job in jobs[offset:end]],
            "total": len(jobs),
            "hidden": hidden,
            "offset": offset,
            "next_offset": end if end < len(jobs) else None,
            "shown_as_gallery": shown_as_gallery(ctx),
        }

    @server.tool(
        title="Get a video",
        annotations=READ_ONLY,
        meta=GALLERY_META,
        structured_output=True,
    )
    async def get_video(job_id: JobId, ctx: Context | None = None) -> dict[str, Any]:
        """Read one video: status, progress while it is being made, and its video and thumbnail
        links once ready. Links are temporary; read the video again rather than reusing old ones."""
        result = await _api(backend, "GET", f"/api/jobs/{job_id}")
        return {**_job_summary(result), "shown_as_gallery": shown_as_gallery(ctx)}

    @server.tool(
        title="Make a video",
        annotations=WRITE,
        structured_output=True,
    )
    async def create_video(
        niche: Niche,
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")],
        idempotency_key: IdempotencyKey,
        format: Annotated[str, Field(max_length=64, description="Format id from get_video_options; auto picks one.")] = "auto",
        mode: Annotated[str, Field(max_length=32, description="Quality mode from get_video_options.")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Output resolution.")] = "720p",
        style: Annotated[str | None, Field(max_length=200, description="Optional visual style for this format; omit for auto.")] = None,
        audio_mode: Annotated[str | None, Field(max_length=32, description="Optional sound choice, such as narration.")] = None,
        narrator_voice: Annotated[str | None, Field(max_length=64, description="Optional narrator voice.")] = None,
        narration_language: Annotated[str | None, Field(max_length=64, description="Optional narration language.")] = None,
        music_track: Annotated[str | None, Field(max_length=128, description="Optional music track identifier.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Start making a video. This holds credits.

        Before calling: gather the choices one at a time (topic, format, length, sound and
        voice, style), quote it with get_video_price, show the summary with
        ``estimated_credits`` and get explicit approval. The exact charge comes back as
        ``credits_cost``. Use a fresh idempotency_key so a retry cannot make a duplicate.
        The result means queued, not finished: making a video takes a few minutes. Tell the
        user, and check with get_video when they ask; do not poll in a loop.
        """
        _require_confirmation(confirm, "Video creation")
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
            idempotency_key=idempotency_key,
        )
        result = await _api(backend, "POST", "/api/jobs", body=body)
        return _job_summary(result) if isinstance(result, dict) else {"result": result}

    @server.tool(
        title="Cancel, pause or retry a video",
        annotations=WRITE,
        structured_output=True,
    )
    async def control_video(
        job_id: JobId,
        action: Annotated[Literal["cancel", "pause", "retry"], Field(description="Job action to perform.")],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Cancel, pause, or retry a video job after explicit user approval.

        Cancel may refund a queued credit hold, pause keeps the hold, and retrying a failed or
        canceled job may charge credits again. A retry of a paused job resumes its hold.
        """
        _require_confirmation(confirm, f"Video job {action}")
        return _dict_result(await _api(backend, "POST", f"/api/jobs/{job_id}/{action}"))

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
        """Update owned video metadata; published YouTube metadata may also be changed.

        This is a write even though it does not spend credits. Ask the user to review the exact
        fields first and set confirm=true only after approval.
        """
        _require_confirmation(confirm, "Video metadata update")
        body = _optional_params(
            title=_bounded_text(title, "title", 500),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        if not body:
            raise ToolError("Provide at least one metadata field to update.")
        result = await _api(backend, "PATCH", f"/api/jobs/{job_id}", body=body)
        if isinstance(result, dict) and isinstance(result.get("job"), dict):
            return {**result, "job": _job_summary(result["job"])}
        return _dict_result(result)
