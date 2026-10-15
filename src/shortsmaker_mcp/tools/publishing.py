"""Social-account connection and publishing-queue tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

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
    _account_refs,
    _api,
    _bounded_text,
    _clean_tags,
    _dict_result,
    _fail,
    _items,
    _label,
    _optional_params,
    _post_state,
    _require_confirmation,
    _video_ref,
    _when,
    _with_next,
)


async def _health(backend: BackendClient) -> dict[str, Any]:
    """Whether posting works right now; {} (unknown, assume fine) if it can't be read, since
    a diagnostic must never block listing accounts or posting."""
    try:
        return _dict_result(await _api(backend, "GET", "/api/publishing/health"))
    except ToolError:
        return {}


_DELAYED = "Posting is paused on our side; posts wait and go out when it's back"


def _posting(c: dict[str, Any], health: dict[str, Any]) -> tuple[str, str]:
    """Can this account be posted to right now, apart from being connected? (reason, words);
    reason is ok, reconnect, unsupported or delayed."""
    if c.get("status") not in (None, "active"):
        return "reconnect", "Reconnect first"
    platforms = health.get("worker_platforms")
    if isinstance(platforms, list) and health.get("worker_alive") and c.get("platform") not in platforms:
        return "unsupported", f"Posting to {_PLATFORM.get(c.get('platform'), c.get('platform'))} isn't supported yet"
    if health.get("worker_alive") is False:
        return "delayed", _DELAYED
    return "ok", "Working"


async def _check_destinations(
    backend: BackendClient, job_id: str, chosen: set[str], allow_repost: bool,
) -> tuple[dict[str, str], str]:
    """Catch the mistakes a chat makes that the API accepts: an account that needs reconnecting,
    a platform posting doesn't support, and a repeat post. Returns the chosen accounts' display
    names and a warning ("" if none) when posting is delayed."""
    connections, health = await asyncio.gather(_api(backend, "GET", "/api/connections"), _health(backend))
    rows = {c["id"]: c for c in connections if isinstance(c, dict) and c.get("id") in chosen} \
        if isinstance(connections, list) else {}
    broken = [_account(c) for c in rows.values() if c.get("status") not in (None, "active")]
    if broken:
        raise _fail(
            "needs_reconnect",
            f"{', '.join(broken)} needs reconnecting before posting. Nothing was posted. "
            "Offer connect_account for that platform."
        )
    posting = {cid: _posting(c, health) for cid, c in rows.items()}
    unsupported = [why for reason, why in posting.values() if reason == "unsupported"]
    if unsupported:
        # A post there would wait forever, so refuse instead of queueing it.
        raise _fail("platform_unavailable", f"{'; '.join(unsupported)}. Nothing was posted.")
    delayed = next((why for reason, why in posting.values() if reason == "delayed"), "")
    names = {cid: _account(c) for cid, c in rows.items()}
    if allow_repost:
        return names, delayed
    posts = await _api(backend, "GET", "/api/posts", params={"job_id": str(job_id), "summary": "true"})
    repeats = [
        f"{names.get(p['connection_id'], 'an account')}: {_post_state(p)['status_label'].lower()}"
        for p in posts if isinstance(p, dict)
        and p.get("connection_id") in chosen and p.get("status") in {"scheduled", "publishing", "published"}
    ] if isinstance(posts, list) else []
    if repeats:
        raise _fail(
            "already_posted",
            "This video is already posted or waiting to post there (" + "; ".join(repeats) + "). Tell the user; "
            "call again with allow_repost=true only if they want it posted again."
        )
    return names, delayed


def _post_time(when: datetime | None) -> str:
    """The time as the user gave it (their own offset), plus how far away it is."""
    if when is None:
        return "right away"
    clock = f"{when:%I:%M %p}".lstrip("0")
    return f"on {when:%a} {when.day} {when:%b} at {clock} ({_when(when)})"


_PLATFORM = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


def _account(c: dict[str, Any]) -> str:
    platform = _PLATFORM.get(c.get("platform"), str(c.get("platform") or "account"))
    return f"{c.get('account_name') or 'your account'} ({platform})"


def _connection_row(c: Any, health: dict[str, Any]) -> Any:
    """``status_label`` says whether it is signed in; ``can_post``/``posting`` whether posting
    there actually works right now. Connected is not the same as postable."""
    if not isinstance(c, dict):
        return c
    label = "Connected" if c.get("status") in (None, "active") else _label(c.get("status"))
    reason, posting = _posting(c, health)
    return {**c, "status_label": label, "can_post": reason == "ok", "posting": posting}


def _connections_summary(connections: list[Any]) -> str:
    rows = [c for c in connections if isinstance(c, dict)]
    if not rows:
        return "No social accounts are connected yet. Want a link to connect YouTube, TikTok or Instagram?"
    ok = [_account(c) for c in rows if c.get("status") in (None, "active")]
    broken = [_account(c) for c in rows if c.get("status") not in (None, "active")]
    text = f"Connected: {', '.join(ok)}." if ok else ""
    for why in sorted({c["posting"] for c in rows if c.get("status") in (None, "active") and not c["can_post"]}):
        text += f" {why}."
    if broken:
        text += f" {', '.join(broken)} needs reconnecting. Want a sign-in link?"
    return text.strip()


def _post_row(p: Any) -> Any:
    return {**p, **_post_state(p)} if isinstance(p, dict) else p


def _posts_summary(posts: list[Any]) -> str:
    rows = [p for p in posts if isinstance(p, dict)]
    if not rows:
        return "Nothing has been posted or scheduled yet."
    counts: dict[str, int] = {}
    for p in rows:
        counts[p["status_label"]] = counts.get(p["status_label"], 0) + 1
    return f"{len(rows)} post{'' if len(rows) == 1 else 's'}: " + ", ".join(
        f"{n} {label.lower()}" for label, n in counts.items()
    ) + "."


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
    if any(isinstance(p, dict) and p.get("state") == "failed" for p in posts):
        return "Some posts failed: quote each one's last_error and offer its fix."
    if any(isinstance(p, dict) and p.get("state") == "sent" for p in posts):
        return "A 'sent' post was accepted by the platform but not confirmed live: never call it live."
    return None


# list_posts filters by the user's states; the backend knows only its five statuses.
_BACKEND_STATUS = {"queued": "scheduled", "sent": "published", "cancelled": "canceled"}


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="See my connected accounts",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_connections() -> dict[str, Any]:
        """Use when you need the user's YouTube, TikTok or Instagram accounts: before posting,
        creating a series, or when they ask what is connected.

        Do: show each account as "name (platform)"; follow ``next_step`` when present. Being
        connected is not the same as being able to post: say ``posting`` when ``can_post`` is false.
        Don't: post to an account whose status is not active; reconnect it with connect_account.
        Returns: ``items`` (id, platform, account_name, status, status_label, last_error,
        can_post, posting), ``count`` and, when there is nothing usable, a ``next_step``. Never
        includes provider tokens.
        """
        connections, health = await asyncio.gather(_api(backend, "GET", "/api/connections"), _health(backend))
        rows = [_connection_row(c, health) for c in _items(connections)["items"]]
        return _items(rows, _connections_next(rows), _connections_summary(rows))

    @server.tool(
        title="List my posts",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def list_posts(
        status: Annotated[
            Literal["queued", "scheduled", "publishing", "sent", "published", "failed", "cancelled"] | None,
            Field(description="Optional filter by state (see Returns)."),
        ] = None,
        job_id: Annotated[str | None, Field(max_length=200, description="Only posts of this video: words from its title, or its id.")] = None,
        updated_since: Annotated[datetime | None, Field(description="Optional timezone-aware update cutoff.")] = None,
    ) -> dict[str, Any]:
        """Use when the user asks whether a video was posted, what is scheduled, or why a post failed.

        Each post has a ``state``: queued (due, waiting to go out, or trying again after a
        failed attempt: see ``next_try`` and ``tries``), scheduled (goes out later), publishing
        (uploading now), sent (the platform accepted it but gave no link back: NOT confirmed
        live), published (live, with platform_post_url), failed (see ``last_error`` and
        ``fix``: "Retry the post" = control_post retry, "Reconnect the account" =
        connect_account first), cancelled.
        Do: say ``status_label``; only "published" is live. Quote last_error and offer ``fix``
        for failed posts.
        Don't: call a sent post live, say a post is live from publish_video's result, or poll
        in a loop.
        Returns: ``items`` (id, job_id, platform, account_name, state, status_label, when,
        last_error, fix), ``count`` and a ``next_step`` when there are none or some need care.
        """
        job_id = await _video_ref(backend, job_id) if job_id else None
        params = _optional_params(status=_BACKEND_STATUS.get(status, status), job_id=job_id, updated_since=updated_since)
        params["summary"] = "true"
        out = _items(await _api(backend, "GET", "/api/posts", params=params))
        rows = [_post_row(p) for p in out["items"]]
        if status:
            rows = [p for p in rows if isinstance(p, dict) and p["state"] == status]
        return _items(rows, _posts_next(rows), _posts_summary(rows))

    @server.tool(
        title="Check posting is working",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def check_publishing_health() -> dict[str, Any]:
        """Use when posts seem stuck in "scheduled" or "publishing" past their time.

        Do: tell the user in plain words whether posting is working.
        Don't: call it before every post; it is a diagnostic, not a precondition.
        Returns: whether the publishing worker is alive and processing the queue, which
        platforms it posts to, and a ``summary``.
        """
        health = _dict_result(await _api(backend, "GET", "/api/publishing/health"))
        if health.get("worker_alive") is False:
            summary = "Posting is paused on our side right now. Waiting posts go out as soon as it's back."
        else:
            summary = "Posting is working."
        waiting = health.get("waiting")
        if isinstance(waiting, int) and waiting:
            summary += f" {waiting} post{'' if waiting == 1 else 's'} waiting."
        return {**health, "summary": summary}

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
            f"Open this link to connect your {_PLATFORM[platform]} account (it works for about 10 "
            "minutes), then tell me when you're done and I'll check it worked.",
        )

    @server.tool(
        title="Disconnect an account",
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
        connection_id = (await _account_refs(backend, [connection_id]))[0]
        _require_confirmation(confirm, "Social-account disconnect")
        result = _dict_result(await _api(backend, "DELETE", f"/api/connections/{connection_id}"))
        held = result.get("canceled_posts")
        result["summary"] = "Disconnected. Posts already published stay up" + (
            f"; {held} waiting post{'' if held == 1 else 's'} cancelled." if held else "."
        )
        return result

    @server.tool(
        title="Post a video",
        annotations=WRITE,
        meta=ASK_EVERY_TIME,
        structured_output=True,
    )
    async def publish_video(
        job_id: JobId,
        connection_ids: Annotated[
            list[str],
            Field(min_length=1, max_length=20, description="Where to post: account names or platforms ('my YouTube'), or ids from list_connections."),
        ],
        visibility: Annotated[
            Literal["public", "unlisted", "private"],
            Field(description=(
                "Who can see it; always ask the user. Offer public (recommended), unlisted or "
                "private. TikTok treats unlisted and private as only me. Instagram posts are always public."
            )),
        ],
        scheduled_at: Annotated[
            AwareDatetime | None,
            Field(description="Optional publish time in the user's own timezone offset; omit to post right away."),
        ] = None,
        title: Annotated[str | None, Field(max_length=100, description="Optional published title, up to 100 characters.")] = None,
        description: Annotated[str | None, Field(max_length=10_000, description="Optional published description.")] = None,
        tags: Annotated[list[str] | None, Field(max_length=100, description="Optional published tags.")] = None,
        allow_repost: Annotated[
            bool,
            Field(description="True only if the user explicitly wants this video posted again to an account it already went to."),
        ] = False,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Use when the user wants to post a finished video to their connected accounts. Call it
        first without confirm to get a preview, then with confirm=true after the user says yes.

        Do: ask who can see it (public recommended) and when (now, or a time in their timezone).
        Show the preview's ``summary`` and get a yes. If a call timed out, check list_posts
        before calling again.
        Don't: guess the timezone, or set allow_repost unless the user asked to post it again.
        Returns: without confirm, a preview (status "needs_approval"; it also refuses an
        unfinished video, an account that needs reconnecting or a platform posting doesn't
        support). With confirm, ``items`` (one post per account, state queued or scheduled:
        NOT posted yet) and ``summary``.
        Example: "post the cooking video to my YouTube tomorrow 9am" -> ask their timezone and
        visibility, publish_video("cooking", ["my YouTube"], "public",
        scheduled_at="2026-10-16T09:00:00-05:00") for the preview, then the same with confirm=true.
        """
        job_id = await _video_ref(backend, job_id)
        if scheduled_at is not None and scheduled_at < datetime.now(UTC) - timedelta(minutes=1):
            raise _fail("invalid_input", "That time is in the past. Ask for a future time, or post right away.")
        body = _optional_params(
            job_id=job_id,
            connection_ids=await _account_refs(backend, connection_ids),
            scheduled_at=scheduled_at,
            title=_bounded_text(title, "title", 100),
            description=description.strip() if description is not None else None,
            tags=_clean_tags(tags),
            visibility=visibility,
        )
        if not confirm:
            job = _dict_result(await _api(backend, "GET", f"/api/jobs/{job_id}"))
            if job.get("status") in ("failed", "cancelled"):
                raise _fail("wrong_state", "This video didn't finish, so there is nothing to post. Nothing was posted.")
            if job.get("status") not in ("ready", "complete"):
                raise _fail(
                    "wrong_state",
                    f"This video isn't ready to post yet ({_label(job.get('status')).lower()}). "
                    "Nothing was posted; offer to post it once get_video shows it ready."
                )
            names, delayed = await _check_destinations(backend, job_id, set(body["connection_ids"]), allow_repost)
            places = ", ".join(names.values()) or "the chosen accounts"
            name = body.get("title") or job.get("title") or (job.get("niche") or "this video")[:60]
            summary = f"Post \"{name}\" to {places} {_post_time(scheduled_at)}, {visibility}."
            if any(place.endswith("(Instagram)") for place in names.values()) and visibility != "public":
                summary += " Instagram posts are always public."
            if delayed:
                summary += f" Note: {delayed}."
            return {
                "status": "needs_approval",
                "summary": summary,
                "next_step": "Show the summary and ask to post. On yes, call publish_video again with the same values and confirm=true.",
            }
        names, _ = await _check_destinations(backend, job_id, set(body["connection_ids"]), allow_repost)
        result = await _api(backend, "POST", "/api/posts", body=body)
        rows = [_post_row(p) for p in (result if isinstance(result, list) else [_dict_result(result)])]
        return _items(
            rows,
            "Queued, not posted. Check list_posts(job_id=...) when the user asks.",
            f"Queued to post to {', '.join(names.values()) or 'your accounts'} {_post_time(scheduled_at)}. "
            "I'll confirm it's live when you ask.",
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
            "Cancelled. It won't be posted." if action == "cancel"
            else "Trying that post again. I'll confirm it's live when you ask.",
        )
