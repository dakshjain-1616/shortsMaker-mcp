"""Guided prompts (slash commands in clients that show them) for the main user journeys.

Each one walks the assistant through asking one choice at a time, previewing the cost, getting
explicit approval, and only then writing.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from ..backend_client import BackendClient
from ._common import JobId, WorkflowId

_ASKING = (
    "Ask one short question at a time, always as choices: use your interactive question or "
    "multiple-choice tool if you have one, otherwise a short numbered list. Give 2-4 options, "
    "each a short label plus one line, one marked (Recommended), and let me answer something "
    "else. Never ask a bare open question. Skip any question I have already answered."
)


def _user_message(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": {"type": "text", "text": text}}]


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.prompt(
        name="new_video",
        title="Make a new video",
        description="Choose a topic, format, length, voice and style step by step, see the price, then make it.",
    )
    def new_video(
        topic: Annotated[str | None, Field(max_length=10_000, description="What the video is about, if you know already.")] = None,
    ) -> list[dict[str, Any]]:
        """Guide the assistant through making one video."""
        about = f"about {topic!r}" if topic else (
            "and start by asking what it should be about: call list_videos and offer 3-4 "
            "concrete topic ideas in the style of my past videos (or popular faceless niches if I "
            "have none)"
        )
        return _user_message(
            f"Help me make a Faceless Video Maker video {about}. {_ASKING} "
            "Keep it to two questions: the topic, then the quality (call get_video_options and "
            "offer choices.quality, the first one recommended). Only ask about format, length, "
            "sound, voice or style if I say I want to choose; otherwise leave them on auto. "
            "Then call create_video without confirm to get the preview and show me its summary, "
            "any adjustments and any warning, in plain words. Do not make anything until I say "
            "yes. After I approve, call create_video again with the same settings, confirm=true "
            "and expected_credits from the preview, tell me it is being made and takes a few "
            "minutes, and check it with get_video when I ask.\n"
            "Handle these cases: if my topic is vague (\"make me a viral video\"), offer 3 concrete "
            "topics first. If the preview says I can't afford it, show the price, my balance and "
            "the billing link instead of asking to go ahead. If the price changed, show the new "
            "one and ask again. If I ask for several videos, preview each and say the total first.\n"
            "Example summary before approval: \"A video about the Roman empire. This video costs "
            "18 credits. You have 120, enough for about 6 like this. Make it?\""
        )

    @server.prompt(
        name="new_series",
        title="Start a series",
        description="Set up a series that makes and posts a new video on a schedule, step by step.",
    )
    def new_series(
        theme: Annotated[str | None, Field(max_length=2000, description="What the series is about, if you know already.")] = None,
    ) -> list[dict[str, Any]]:
        """Guide the assistant through creating an Autopilot series."""
        about = f"about {theme!r}" if theme else "and ask me what the series should be about first"
        return _user_message(
            f"Help me start a Faceless Video Maker series {about}. {_ASKING} "
            "First call list_connections. If no social account is connected, offer to connect "
            "one with connect_account and wait for me to finish in the browser. Then ask four "
            "things, in order: what it is about, how often (Every day, Weekdays, Weekends, or "
            "specific days), what time (and my timezone), and where to post. Then ask who can "
            "see the posts: public (recommended), unlisted or private. Leave the video length "
            "at 30 seconds and the rest on auto unless I want to choose. Call create_series "
            "without confirm and show me its summary: the schedule, the weekly cost and how long "
            "my credits last. Do not start anything until I approve. After I approve, call "
            "create_series again with confirm=true and tell me when the first video is planned.\n"
            "Handle these cases: never assume my timezone, ask (offer my likely city's zone). For "
            "\"every morning\", offer 07:00, 09:00 (recommended) or 11:00.\n"
            "Example summary before approval: \"Space facts: Weekdays at 9:00 AM BST, posts to "
            "MyChannel (YouTube), public. About 18 credits a video, 5 a week = 90 credits a week. "
            "Your 720 credits cover about 8 weeks. Start it?\""
        )

    @server.prompt(
        name="share_video",
        title="Post a video",
        description="Pick accounts, review the post, and queue it to YouTube, TikTok or Instagram.",
    )
    def share_video(job_id: JobId) -> list[dict[str, Any]]:
        """Guide the assistant through publishing a finished video."""
        return _user_message(
            f"Help me post my Faceless Video Maker video {job_id!r}. {_ASKING} Ask where to post "
            "(my connected accounts from list_connections), who can see it (public recommended, "
            "unlisted or private) and when (now, or a time in my timezone). If an account needs "
            "reconnecting, start the browser flow with connect_account and ask me to finish it; "
            "never request passwords or tokens in chat. Call publish_video without confirm and "
            "show me its summary, then only after I approve call it again with confirm=true. Say "
            "the post is queued, not that it has already been uploaded, and use list_posts to "
            "check when I ask."
        )

    @server.prompt(
        name="review_series",
        title="Check on a series",
        description="See how a series is doing and change, pause, resume or stop it safely.",
    )
    def review_series(series_id: WorkflowId) -> list[dict[str, Any]]:
        """Guide the assistant through inspecting and changing a series."""
        return _user_message(
            f"Help me manage my series {series_id!r}. First read it with get_series and its "
            "upcoming videos with list_series_slots. Explain in plain words how it is doing, "
            "its schedule, where it posts and what is coming up, plus any consequence of the "
            "change I ask for. For changes, pause, restart, end or delete, or changes to one "
            "upcoming video, show me what will change and ask for my approval before calling "
            "the tool with confirm=true. Do not say a video or post exists until a read confirms it."
        )
