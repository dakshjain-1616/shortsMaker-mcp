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
    _items,
    _optional_params,
    _require_confirmation,
    _with_next,
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


def _connections_next(connections: list[Any]) -> str | None:
    if not connections:
        return "No accounts are connected. Offer connect_account for YouTube, TikTok or Instagram."
    broken = sorted({
        str(c.get("platform")) for c in connections
        if isinstance(c, dict) and c.get("status") not in (None, "active")
    })
    if broken:
        return (
            f"Not usable now: {', '.join(broken)} (see status and last_error). Offer "
            "connect_account(platform) to reconnect; don't post or retry posts to them until fixed."
        )
    return None


def _posts_next(posts: list[Any]) -> str | None:
    if not posts:
        return "No posts match. Videos are posted with publish_video."
    if any(isinstance(p, dict) and p.get("status") == "failed" for p in posts):
        return (
            "Some posts failed: quote each one's last_error. If it is an account or auth problem, "
            "reconnect with connect_account before control_post(action='retry')."
        )
    return None


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="List connected social accounts",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_connections() -> dict[str, Any]:
        """Use when you need the user's YouTube, TikTok or Instagram accounts: before posting,
        creating a series, or when they ask what is connected.

        Do: show each account as "name (platform)"; follow ``next_step`` when present.
        Don't: post to an account whose status is not active; reconnect it with connect_account.
        Returns: ``items`` (id, platform, account_name, status, last_error), ``count`` and,
        when there is nothing usable, a ``next_step``. Never includes provider tokens.
        """
        out = _items(await _api(backend, "GET", "/api/connections"))
        return _items(out["items"], _connections_next(out["items"]))

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
    ) -> dict[str, Any]:
        """Use when the user asks whether a video was posted, what is scheduled, or why a post failed.

        Do: report each post's status as-is: only "published" means it is live; "scheduled"
        and "publishing" are still waiting. Quote ``last_error`` for failed posts.
        Don't: say a post is live from publish_video's result; check here when the user asks,
        without polling in a loop.
        Returns: ``items`` (id, job_id, connection_id, platform, status, scheduled_at,
        last_error), ``count`` and a ``next_step`` when there are none or some failed.
        """
        params = _optional_params(status=status, job_id=job_id, updated_since=updated_since)
        params["summary"] = "true"
        out = _items(await _api(backend, "GET", "/api/posts", params=params))
        return _items(out["items"], _posts_next(out["items"]))

    @server.tool(
        title="Check posting service",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def check_publishing_health() -> dict[str, Any]:
        """Use when posts seem stuck in "scheduled" or "publishing" past their time.

        Do: tell the user in plain words whether posting is working.
        Don't: call it before every post; it is a diagnostic, not a precondition.
        Returns: whether the publishing worker is alive and processing the queue.
        """
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

        Do: give the user the link, say it expires in about 10 minutes, and call
        list_connections after they say they are done. Calling it for a platform whose account
        shows status "error" repairs that connection.
        Don't: ask for passwords or tokens in chat, or say the account is connected before
        list_connections shows it.
        Returns: the sign-in link and a ``next_step``.
        Example: "connect my TikTok" -> connect_account("tiktok", confirm=true); reply "Open this
        link and approve, then tell me when you're done", then call list_connections to check.
        """
        _require_confirmation(confirm, f"Connecting a {platform} account")
        return _with_next(
            await _api(backend, "POST", f"/api/connections/{platform}/authorize"),
            "Not connected yet. Give the user the link (expires in ~10 minutes); when they say "
            "they are done, check with list_connections.",
        )

    @server.tool(
        title="Disconnect social account",
        annotations=WRITE,
        structured_output=True,
    )
    async def disconnect_account(
        connection_id: ConnectionId,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to remove a connected social account.

        It cancels that account's still-queued posts and deletes the stored sign-in; reconnecting
        needs connect_account again. Published posts stay published.
        Do: name the account and platform and say queued posts will be cancelled, then get approval.
        Don't: use it to fix a broken account; connect_account repairs it in place.
        Returns: the backend's confirmation.
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
    ) -> dict[str, Any]:
        """Use when the user has approved posting a finished video to their connected accounts.

        Do: check the video is ready (get_video); show the account names, platforms,
        visibility, time (with timezone), title and description; get explicit approval. For
        TikTok, ask who can see it. If a call timed out, check list_posts before calling again.
        Don't: post an unfinished video, post to an account whose status is not active, guess
        the timezone, or set allow_repost unless the user asked to post it again.
        Returns: ``items`` (one post per account: id, status "scheduled"/"publishing" = queued,
        NOT posted), ``count`` and ``next_step``.
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
            title=_bounded_text(title, "title", 100),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        _require_confirmation(confirm, "Publishing")
        await _check_destinations(backend, job_id, set(body["connection_ids"]), visibility, allow_repost)
        result = await _api(backend, "POST", "/api/posts", body=body)
        return _items(
            result if isinstance(result, list) else [_dict_result(result)],
            "Queued, not posted. Tell the user when it will go out; check list_posts(job_id=...) "
            "when they ask.",
        )

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

        Do: read it with list_posts first; before a retry, show the destination and the
        earlier last_error.
        Don't: retry a post whose account shows an error (reconnect with connect_account
        first), or try to cancel one already "publishing": it cannot be recalled.
        Returns: the post's new status plus ``next_step``.
        """
        _require_confirmation(confirm, f"Publishing post {action}")
        return _with_next(
            await _api(backend, "POST", f"/api/posts/{post_id}/{action}"),
            "Cancelled; it will not be posted." if action == "cancel"
            else "Queued again, not posted. Check list_posts when the user asks.",
        )
