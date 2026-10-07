"""Video generation, job inspection, and job control tools."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
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


def _with_credits(quote: dict[str, Any], credits_per_usd: float) -> dict[str, Any]:
    """Add ``estimated_credits`` using the backend's own rule: ceil(usd * rate), at least 1."""
    cost = quote.get("estimated_cost")
    public_quote = {key: value for key, value in quote.items() if key != "estimated_cost"}
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
        return public_quote
    return {**public_quote, "estimated_credits": max(1, math.ceil(cost * credits_per_usd))}


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


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="Get video generation options",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_video_options(
        mode: Annotated[str, Field(max_length=32, description="Mode to list valid lengths for.")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Resolution to list valid lengths for.")] = "720p",
    ) -> dict[str, Any]:
        """Read current video formats, modes, resolutions, and valid length options.

        Valid lengths depend on mode and resolution, so pass the combination the user is
        considering. Use this before asking the user to choose generation settings. This tool is
        read-only; it does not create a job or spend credits.
        """
        config, lengths = await asyncio.gather(
            _api(backend, "GET", "/api/config"),
            _api(
                backend,
                "GET",
                "/api/lengths",
                params={
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
    async def shortsmaker_resolve_video_price(
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")],
        mode: Annotated[str, Field(max_length=32, description="Generation mode, usually lite.")] = "lite",
        format: Annotated[str, Field(max_length=64, description="Generation format.")] = "transformation",
        resolution: Annotated[str, Field(max_length=32, description="Output resolution.")] = "720p",
        audio_mode: Annotated[str | None, Field(max_length=32, description="Optional audio mode.")] = None,
        workflow: Annotated[str | None, Field(max_length=64, description="Optional workflow name.")] = None,
        niche: Annotated[str | None, Field(max_length=10_000, description="Optional topic used by auto resolution.")] = None,
    ) -> dict[str, Any]:
        """Preview the backend-resolved settings and price without creating a job.

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
        return _with_credits(quote, backend.credits_per_usd)

    @server.tool(
        title="List video jobs",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_list_videos(
        limit: Annotated[int, Field(ge=1, le=50, description="Jobs to return per page, newest first.")] = 5,
        offset: Annotated[int, Field(ge=0, description="Jobs to skip; pass next_offset from the previous page.")] = 0,
        updated_since: Annotated[datetime | None, Field(description="Only jobs updated after this timezone-aware timestamp.")] = None,
    ) -> dict[str, Any]:
        """List one page of the signed-in user's video jobs, newest first.

        Returns at most ``limit`` jobs (default 5) to keep responses small. When ``next_offset``
        is not null, more jobs exist: call again with ``offset=next_offset`` only if the user
        wants them. The backend exposes its 200 most recent jobs. With ``updated_since`` the
        order is oldest change first, so advance by the last job's ``updated_at``.
        """
        result = await _api(
            backend,
            "GET",
            "/api/jobs",
            params=_optional_params(updated_since=updated_since),
        )
        end = offset + limit
        return {
            "videos": [_job_summary(job) for job in result[offset:end]],
            "total": len(result),
            "offset": offset,
            "next_offset": end if end < len(result) else None,
        }

    @server.tool(
        title="Get video job",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_video(job_id: JobId) -> dict[str, Any]:
        """Read one owned video job, including status, progress, and temporary media links."""
        result = await _api(backend, "GET", f"/api/jobs/{job_id}")
        return _job_summary(result)

    @server.tool(
        title="Create and queue a video",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_create_video(
        niche: Niche,
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")],
        idempotency_key: IdempotencyKey,
        format: Annotated[str, Field(max_length=64, description="Generation format.")] = "transformation",
        mode: Annotated[str, Field(max_length=32, description="Generation mode.")] = "lite",
        resolution: Annotated[str, Field(max_length=32, description="Output resolution.")] = "720p",
        style: Annotated[str | None, Field(max_length=200, description="Optional visual style.")] = None,
        audio_mode: Annotated[str | None, Field(max_length=32, description="Optional audio mode.")] = None,
        narrator_voice: Annotated[str | None, Field(max_length=64, description="Optional narrator voice.")] = None,
        narration_language: Annotated[str | None, Field(max_length=64, description="Optional narration language.")] = None,
        music_track: Annotated[str | None, Field(max_length=128, description="Optional music track identifier.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Create a generation job and hold credits; this is a staging write operation.

        Before calling, resolve the price and show the user ``estimated_credits``, summarize the
        settings, and obtain explicit user approval. The exact charge comes back as
        ``credits_cost``. Set confirm=true only after approval. A unique idempotency_key is
        required so a network retry cannot create a duplicate job. The result means queued or
        accepted, not that video generation has finished.
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
        title="Control a video job",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_control_video(
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
        title="Update video metadata",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def shortsmaker_update_video_metadata(
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
