"""Social-account connection and publishing-queue tools."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from pydantic import AwareDatetime, Field

from ..backend_client import BackendClient
from ._common import (
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    Confirm,
    ConnectionId,
    JobId,
    PostId,
    _api,
    _bounded_text,
    _clean_tags,
    _dedupe_ids,
    _dict_result,
    _optional_params,
    _require_confirmation,
)


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="List connected social accounts",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_connections() -> list[dict[str, Any]]:
        """List the user's connected social accounts without exposing provider access tokens."""
        return await _api(backend, "GET", "/api/connections")

    @server.tool(
        title="List my posts",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_posts(
        status: Annotated[
            Literal["scheduled", "publishing", "published", "failed", "canceled"] | None,
            Field(description="Optional publishing status filter."),
        ] = None,
        job_id: Annotated[UUID | None, Field(description="Optional video job filter.")] = None,
        updated_since: Annotated[datetime | None, Field(description="Optional timezone-aware update cutoff.")] = None,
    ) -> list[dict[str, Any]]:
        """List the user's posts to YouTube, TikTok and Instagram and whether each is scheduled, posting, posted or failed."""
        params = _optional_params(status=status, job_id=job_id, updated_since=updated_since)
        params["summary"] = "true"
        return await _api(backend, "GET", "/api/posts", params=params)

    @server.tool(
        title="Check posting service",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def check_publishing_health() -> dict[str, Any]:
        """Check whether the asynchronous publishing worker is alive and processing the user's queue."""
        return _dict_result(await _api(backend, "GET", "/api/publishing/health"))

    @server.tool(
        title="Connect a social account",
        annotations=IDEMPOTENT_WRITE,
        structured_output=True,
    )
    async def connect_account(
        platform: Annotated[Literal["youtube", "instagram", "tiktok"], Field(description="Provider to connect.")],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Start browser-based social-account authorization and return its authorization URL.

        The user must open the returned URL and approve the provider consent screen. Never ask
        the user to paste provider access tokens into the MCP conversation.
        """
        _require_confirmation(confirm, f"Connecting a {platform} account")
        return _dict_result(await _api(backend, "POST", f"/api/connections/{platform}/authorize"))

    @server.tool(
        title="Disconnect social account",
        annotations=WRITE,
        structured_output=True,
    )
    async def disconnect_account(
        connection_id: ConnectionId,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Disconnect one owned social account and cancel its still-queued posts.

        This destroys the stored provider tokens and is not reversible through this tool. Require
        the user to confirm the account name and platform before calling with confirm=true.
        """
        _require_confirmation(confirm, "Social-account disconnect")
        return _dict_result(await _api(backend, "DELETE", f"/api/connections/{connection_id}"))

    @server.tool(
        title="Post a video",
        annotations=WRITE,
        structured_output=True,
    )
    async def publish_video(
        job_id: JobId,
        connection_ids: Annotated[
            list[UUID],
            Field(min_length=1, max_length=20, description="One or more owned connected-account UUIDs."),
        ],
        scheduled_at: Annotated[
            AwareDatetime | None,
            Field(description="Optional timezone-aware publish time; omit to publish as soon as possible."),
        ] = None,
        title: Annotated[str | None, Field(max_length=500, description="Optional published title.")] = None,
        description: Annotated[str | None, Field(max_length=10_000, description="Optional published description.")] = None,
        tags: Annotated[list[str] | None, Field(max_length=100, description="Optional published tags.")] = None,
        visibility: Annotated[
            Literal["public", "unlisted", "private"],
            Field(description="YouTube visibility; other providers may ignore it."),
        ] = "public",
        confirm: Confirm = False,
    ) -> list[dict[str, Any]]:
        """Queue a completed owned video for upload to selected social accounts.

        Before calling, verify the job is finished, show the account names, destination platforms,
        visibility, schedule, title, and description, then obtain explicit approval. A successful
        result means queued, not uploaded or published. The provider worker performs the upload
        afterward and the post-status tool must be used to check the final result.
        """
        _require_confirmation(confirm, "Publishing")
        body = _optional_params(
            job_id=job_id,
            connection_ids=_dedupe_ids(connection_ids),
            scheduled_at=scheduled_at,
            title=_bounded_text(title, "title", 500),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        result = await _api(backend, "POST", "/api/posts", body=body)
        return result if isinstance(result, list) else [_dict_result(result)]

    @server.tool(
        title="Cancel or retry a post",
        annotations=WRITE,
        structured_output=True,
    )
    async def control_post(
        post_id: PostId,
        action: Annotated[Literal["cancel", "retry"], Field(description="Post action to perform.")],
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Cancel a waiting post or retry a failed/canceled post after user approval.

        A post already being uploaded cannot be recalled. Retrying creates a new publishing
        attempt and should happen only after the user understands the destination and prior error.
        """
        _require_confirmation(confirm, f"Publishing post {action}")
        return _dict_result(await _api(backend, "POST", f"/api/posts/{post_id}/{action}"))
