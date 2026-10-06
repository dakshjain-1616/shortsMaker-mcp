"""User-controlled workflow prompts that reinforce the preview, approve, write, verify sequence."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from ..backend_client import BackendClient
from ._common import (
    JobId,
    Niche,
    WorkflowId,
)


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.prompt(
        name="shortsmaker_create_video_workflow",
        title="Plan a ShortsMaker video",
        description="Guide the assistant through a safe preview, confirmation, and video-creation workflow.",
    )
    def shortsmaker_create_video_workflow(
        niche: Niche,
        total_length: Annotated[int, Field(ge=1, le=3600, description="Requested duration in seconds.")] = 15,
    ) -> list[dict[str, Any]]:
        """Return a user-controlled workflow prompt for creating one generated short."""
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Plan a ShortsMaker video about {niche!r} with requested length "
                        f"{total_length} seconds. First use read-only tools to inspect valid "
                        "options and resolve the estimated price. Explain the resolved format, "
                        "duration, and estimated credits (estimated_credits) to me and ask for explicit "
                        "confirmation. "
                        "Do not call the create tool until I approve the exact settings. After "
                        "approval, call shortsmaker_create_video with confirm=true and a fresh "
                        "idempotency_key. Report only the returned job ID, queued status, and "
                        "cost; generation is asynchronous, so use shortsmaker_get_video for "
                        "progress."
                    ),
                },
            }
        ]

    @server.prompt(
        name="shortsmaker_publish_video_workflow",
        title="Plan video publishing",
        description="Guide the assistant through account selection, confirmation, queueing, and status checks.",
    )
    def shortsmaker_publish_video_workflow(job_id: JobId) -> list[dict[str, Any]]:
        """Return a user-controlled workflow prompt for publishing a completed video."""
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Help me publish ShortsMaker job {job_id}. First read the job and list "
                        "connected accounts. If the video is not complete, explain that it "
                        "cannot be published yet. If account authorization is needed, start "
                        "the browser flow and ask me to complete it; never request provider "
                        "tokens in chat. Before publishing, show the selected account names, "
                        "platforms, title, description, visibility, and schedule, then ask for "
                        "explicit confirmation. Only after approval call publish with "
                        "confirm=true. Report that the post is queued, not that it has already "
                        "been uploaded, and use list_posts to check final status."
                    ),
                },
            }
        ]


    @server.prompt(
        name="shortsmaker_workflow_management",
        title="Review a recurring workflow",
        description="Guide the assistant through safe workflow inspection and confirmation.",
    )
    def shortsmaker_workflow_management(workflow_id: WorkflowId) -> list[dict[str, Any]]:
        """Return a user-controlled workflow management prompt."""
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Help me manage recurring workflow {workflow_id}. First read the workflow "
                        "and its slots. Explain its status, cadence, destination accounts, "
                        "planned work, and any consequence of the requested change. For edits, "
                        "pause/resume/stop/delete, or slot actions, show the exact operation and "
                        "ask for explicit approval before calling the corresponding tool with "
                        "confirm=true. Do not claim a generated video or post exists until the "
                        "job or post readers confirm it."
                    ),
                },
            }
        ]
