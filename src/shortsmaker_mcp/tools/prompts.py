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
    "Ask one short question at a time. If you have an interactive question or multiple-choice "
    "tool, use it, with 2-4 options that each have a short description and one marked "
    "recommended. Skip any question I have already answered."
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
        about = f"about {topic!r}" if topic else "and ask me what it should be about first"
        return _user_message(
            f"Help me make a Faceless Video Maker video {about}. {_ASKING} "
            "First call get_video_options. Then ask, in order: the format (the 3-4 that fit the "
            "topic best, plus Auto), the length (only lengths valid for that format), the sound "
            "and, for narration, the voice and language, and the visual style (Auto by default). "
            "Then resolve the estimated price with get_video_price and show me a short summary "
            "with estimated_credits. Do not create anything until I approve. After I approve, "
            "call create_video with confirm=true and a fresh idempotency_key, tell me it is "
            "being made and takes a few minutes, and check it with get_video when I ask.\n"
            "Handle these cases: if my topic is vague (\"make me a viral video\"), offer 3 concrete "
            "topics first. If get_video_price says can_afford is false, show the price and my "
            "balance and send me to https://facelessvideomaker.ai instead of asking to go ahead. "
            "If create_video times out, retry with the SAME idempotency_key. If I ask for several "
            "videos, quote the total first and use a new key for each.\n"
            "Example summary before approval: \"30s Narrated Story about the Roman empire, "
            "voice Atlas, 18 credits (you have 120). Make it?\""
        )

    @server.prompt(
        name="new_series",
        title="Start an Autopilot series",
        description="Set up a series that makes and posts a new video on a schedule, step by step.",
    )
    def new_series(
        theme: Annotated[str | None, Field(max_length=2000, description="What the series is about, if you know already.")] = None,
    ) -> list[dict[str, Any]]:
        """Guide the assistant through creating an Autopilot series."""
        about = f"about {theme!r}" if theme else "and ask me what the series should be about first"
        return _user_message(
            f"Help me start a Faceless Video Maker Autopilot series {about}. {_ASKING} "
            "First call list_connections and get_video_options. If no social account is "
            "connected, offer to connect one with connect_account and wait for me to finish in "
            "the browser. Then ask, in order: the format, the posting days (Every day, Weekdays, "
            "Weekends, or specific days), the posting times and my timezone, which connected "
            "accounts to post to, the YouTube visibility, and when it should end (Never, 7 days, "
            "15 days, 1 month). Quote the price per video with get_video_price and show a short "
            "summary including how many videos a week it will make. Do not create anything until "
            "I approve. After I approve, call create_series with confirm=true and tell me when "
            "the first video is planned.\n"
            "Handle these cases: never assume my timezone, ask (offer my likely city's zone). For "
            "\"every morning\", offer 07:00, 09:00 (recommended) or 11:00. Pass the video length "
            "in job_defaults.total_length. If a TikTok account is chosen, ask whether posts are "
            "public or only me.\n"
            "Example summary before approval: \"Space facts, Mon-Fri at 09:00 and 18:00 "
            "Europe/London, to MyChannel (YouTube, public), runs until stopped, 30s videos at about "
            "18 credits each, 10 a week = 180 credits a week. Videos are made 6 hours before each "
            "post. Start it?\""
        )

    @server.prompt(
        name="share_video",
        title="Post a video",
        description="Pick accounts, review the post, and queue it to YouTube, TikTok or Instagram.",
    )
    def share_video(job_id: JobId) -> list[dict[str, Any]]:
        """Guide the assistant through publishing a finished video."""
        return _user_message(
            f"Help me post Faceless Video Maker video {job_id}. {_ASKING} First read the video and "
            "list connected accounts. If the video is not complete, explain that it "
            "cannot be published yet. If account authorization is needed, start "
            "the browser flow and ask me to complete it; never request provider "
            "tokens in chat. Before publishing, show the selected account names, "
            "platforms, title, description, visibility, and schedule, then ask for "
            "explicit confirmation. Only after approval call publish_video with "
            "confirm=true. Report that the post is queued, not that it has already "
            "been uploaded, and use list_posts to check final status."
        )

    @server.prompt(
        name="review_series",
        title="Review an Autopilot series",
        description="See how a series is doing and change, pause, resume or stop it safely.",
    )
    def review_series(series_id: WorkflowId) -> list[dict[str, Any]]:
        """Guide the assistant through inspecting and changing a series."""
        return _user_message(
            f"Help me manage Autopilot series {series_id}. First read the series "
            "and its slots. Explain its status, schedule, destination accounts, "
            "planned work, and any consequence of the requested change. For edits, "
            "pause/resume/stop/delete, or slot actions, show the exact operation and "
            "ask for explicit approval before calling the corresponding tool with "
            "confirm=true. Do not claim a generated video or post exists until the "
            "video or post readers confirm it."
        )
