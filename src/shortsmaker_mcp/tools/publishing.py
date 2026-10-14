"""Social-account connection and publishing-queue tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, Field

from ..backend_client import BackendClient
from ._common import (
    ASK_EVERY_TIME,
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


async def _check_destinations(
    backend: BackendClient, job_id: UUID, chosen: set[str], visibility: str | None, allow_repost: bool,
) -> None:
    """Catch the two mistakes a chat makes that the API accepts: a repeat post and a TikTok
    post whose privacy nobody chose (TikTok requires the user to pick it)."""
    connections = await _api(backend, "GET", "/api/connections")
    names = {
        c["id"]: f"{c.get('account_name')} ({c.get('platform')})"
        for c in connections if isinstance(c, dict) and c.get("id") in chosen
    } if isinstance(connections, list) else {}
    if visibility is None and any(name.endswith("(tiktok)") for name in names.values()):
        raise ToolError(
            "Ask the user who can see the TikTok post: everyone (visibility=public) or only them "
            "(visibility=private). TikTok requires their explicit choice."
        )
    if allow_repost:
        return
    posts = await _api(backend, "GET", "/api/posts", params={"job_id": str(job_id), "summary": "true"})
    repeats = [
        f"{names.get(p['connection_id'], p['connection_id'])}: {p['status']}"
        for p in posts if isinstance(p, dict)
        and p.get("connection_id") in chosen and p.get("status") in {"scheduled", "publishing", "published"}
    ] if isinstance(posts, list) else []
    if repeats:
        raise ToolError(
            "This video is already posted or queued there (" + "; ".join(repeats) + "). Tell the user; "
            "call again with allow_repost=true only if they want it posted again."
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
        """Use when the user wants to connect (or reconnect) a YouTube, TikTok or Instagram
        account. Returns a sign-in link for the user to open in their browser.

        The link expires in about 10 minutes. Never ask for passwords or tokens in chat.
        Calling it for a platform whose account shows status "error" repairs that connection.
        Example: "connect my TikTok" -> connect_account("tiktok", confirm=true); reply "Open this
        link and approve, then tell me when you're done", then call list_connections to check.
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
        meta=ASK_EVERY_TIME,
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
        title: Annotated[str | None, Field(max_length=100, description="Optional published title, up to 100 characters.")] = None,
        description: Annotated[str | None, Field(max_length=10_000, description="Optional published description.")] = None,
        tags: Annotated[list[str] | None, Field(max_length=100, description="Optional published tags.")] = None,
        visibility: Annotated[
            Literal["public", "unlisted", "private"] | None,
            Field(description=(
                "Who can see it. YouTube: public, unlisted or private. TikTok: public, or "
                "unlisted/private for only me; TikTok needs the user's explicit choice. "
                "Instagram ignores it. Omitted means public."
            )),
        ] = None,
        allow_repost: Annotated[
            bool,
            Field(description="True only if the user explicitly wants this video posted again to an account it already went to."),
        ] = False,
        confirm: Confirm = False,
    ) -> list[dict[str, Any]]:
        """Use when the user has approved posting a finished video to their connected accounts.

        Before calling, check the video is ready (get_video), show the account names,
        platforms, visibility, time (with timezone), title and description, and get explicit
        approval. The result means queued, not posted: check list_posts for the outcome.
        Refuses to post a video again to an account it already went to unless allow_repost.
        If a timed-out call is retried, check list_posts first.
        Example: "post my cooking video to YouTube tomorrow 9am" -> list_videos(query="cooking"),
        list_connections, ask their timezone if unknown, show "MyChannel (YouTube), public,
        Tue 09:00 America/Chicago, title ...", then publish_video(job_id, [youtube_id],
        scheduled_at="2026-10-15T09:00:00-05:00", confirm=true).
        Example: the TikTok account shows status "error" -> don't post; offer connect_account("tiktok").
        """
        if scheduled_at is not None and scheduled_at < datetime.now(UTC) - timedelta(minutes=1):
            raise ToolError("scheduled_at is in the past. Ask for a future time, or omit it to post now.")
        body = _optional_params(
            job_id=job_id,
            connection_ids=_dedupe_ids(connection_ids),
            scheduled_at=scheduled_at,
            title=_bounded_text(title, "title", 500),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        _require_confirmation(confirm, "Publishing")
        await _check_destinations(backend, job_id, set(body["connection_ids"]), visibility, allow_repost)
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
        """Use when the user wants to cancel a waiting post or retry a failed or cancelled one.

        A post already being uploaded cannot be recalled. Retrying creates a new publishing
        attempt and should happen only after the user understands the destination and prior error.
        """
        _require_confirmation(confirm, f"Publishing post {action}")
        return _dict_result(await _api(backend, "POST", f"/api/posts/{post_id}/{action}"))
