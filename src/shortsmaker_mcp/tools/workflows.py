"""Recurring generation-and-publishing workflow tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, Field

from ..backend_client import BackendClient
from ._common import (
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
    _optional_params,
    _require_confirmation,
)


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
    cleaned = sorted(set(time.strip() for time in times if isinstance(time, str)))
    if len(cleaned) != len(times) or any(len(time) != 5 or time[2] != ":" or not time[:2].isdigit() or not time[3:].isdigit()
           or int(time[:2]) > 23 or int(time[3:]) > 59 for time in cleaned):
        raise ToolError("times_of_day must contain values in HH:MM 24-hour form.")
    return cleaned

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


def register(server: MCPServer, backend: BackendClient) -> None:
    @server.tool(
        title="List recurring workflows",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_list_workflows(
        status: Annotated[
            Literal["active", "paused", "stopped"] | None,
            Field(description="Optional workflow status filter."),
        ] = None,
    ) -> list[dict[str, Any]]:
        """List the user's recurring generation-and-publishing workflows and slot counts."""
        result = await _api(backend, "GET", "/api/workflows", params=_optional_params(status=status))
        return result if isinstance(result, list) else []

    @server.tool(
        title="Get recurring workflow",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_get_workflow(workflow_id: WorkflowId) -> dict[str, Any]:
        """Read one owned recurring workflow, including cadence, status, and slot counts."""
        return _dict_result(await _api(backend, "GET", f"/api/workflows/{workflow_id}"))

    @server.tool(
        title="List workflow slots",
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def shortsmaker_list_workflow_slots(
        workflow_id: WorkflowId,
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
    ) -> list[dict[str, Any]]:
        """Read up to the backend's recent 200 scheduled slots for one owned workflow."""
        params = _optional_params(
            status=status,
            **{
                "from": from_time,
                "to": to_time,
            },
        )
        result = await _api(backend, "GET", f"/api/workflows/{workflow_id}/slots", params=params)
        return result if isinstance(result, list) else []

    @server.tool(
        title="Create recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_create_workflow(
        name: Annotated[str, Field(min_length=1, max_length=120, description="Workflow display name.")],
        theme: Annotated[str, Field(min_length=3, max_length=2000, description="Brief for recurring videos.")],
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
        variation: Annotated[bool, Field(description="Use variants of one theme instead of fresh ideas.")] = False,
        visibility: Annotated[
            Literal["public", "unlisted", "private"],
            Field(description="Default YouTube visibility."),
        ] = "public",
        lead_minutes: Annotated[int, Field(ge=30, le=1440, description="Minutes before posting to start generation.")] = 360,
        grace_minutes: Annotated[int, Field(ge=0, le=10080, description="How late a slot may be handled.")] = 360,
        ends_at: Annotated[AwareDatetime | None, Field(description="Optional timezone-aware final posting date.")] = None,
        post_description: Annotated[str | None, Field(max_length=10_000, description="Optional shared post description.")] = None,
        post_tags: Annotated[list[str] | None, Field(max_length=100, description="Optional shared post tags.")] = None,
        job_defaults: Annotated[dict[str, Any] | None, Field(description="Optional backend generation defaults.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Create an active recurring generation-and-publishing workflow.

        This creates a schedule that can generate videos and publish them to the selected
        accounts over time. Before calling, show the cadence, theme, destination accounts,
        visibility, and likely credit usage; call only after explicit approval.
        """
        _require_confirmation(confirm, "Creating a recurring workflow")
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
        return _dict_result(await _api(backend, "POST", "/api/workflows", body=body))

    @server.tool(
        title="Update recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_update_workflow(
        workflow_id: WorkflowId,
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
        job_defaults: Annotated[dict[str, Any] | None, Field(description="Replacement generation defaults.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Update an existing recurring workflow after explicit review.

        Changing the cadence or generation brief causes the backend to drop planned slots and
        re-plan them. Show that consequence before calling. Omitted fields stay unchanged.
        """
        _require_confirmation(confirm, "Updating a recurring workflow")
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
            raise ToolError("Provide at least one workflow field to update.")
        return _dict_result(await _api(backend, "PATCH", f"/api/workflows/{workflow_id}", body=body))

    @server.tool(
        title="Pause recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_pause_workflow(workflow_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Pause a recurring workflow so new videos and queued posts are held."""
        _require_confirmation(confirm, "Pausing a recurring workflow")
        return _dict_result(await _api(backend, "POST", f"/api/workflows/{workflow_id}/pause"))

    @server.tool(
        title="Resume recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_resume_workflow(
        workflow_id: WorkflowId,
        ends_at: Annotated[AwareDatetime | None, Field(description="Optional new timezone-aware end date.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Resume a paused or stopped workflow and continue planning from the current time."""
        _require_confirmation(confirm, "Resuming a recurring workflow")
        body = _optional_params(ends_at=ends_at)
        return _dict_result(await _api(backend, "POST", f"/api/workflows/{workflow_id}/resume", body=body or None))

    @server.tool(
        title="Stop recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_stop_workflow(workflow_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Stop a workflow, canceling queued posts while retaining generated video history."""
        _require_confirmation(confirm, "Stopping a recurring workflow")
        return _dict_result(await _api(backend, "POST", f"/api/workflows/{workflow_id}/stop"))

    @server.tool(
        title="Delete recurring workflow",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_delete_workflow(workflow_id: WorkflowId, confirm: Confirm = False) -> dict[str, Any]:
        """Delete a workflow and its calendar; published history remains in the posts list."""
        _require_confirmation(confirm, "Deleting a recurring workflow")
        return _dict_result(await _api(backend, "DELETE", f"/api/workflows/{workflow_id}"))

    @server.tool(
        title="Update workflow slot",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_update_workflow_slot(
        slot_id: WorkflowSlotId,
        title: Annotated[str | None, Field(max_length=500, description="Replacement slot title.")] = None,
        prompt: Annotated[str | None, Field(min_length=3, max_length=10_000, description="Replacement video prompt.")] = None,
        overrides: Annotated[dict[str, Any] | None, Field(description="Optional generation overrides.")] = None,
        slot_at: Annotated[AwareDatetime | None, Field(description="Replacement timezone-aware posting time.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Edit one planned workflow slot before generation has started."""
        _require_confirmation(confirm, "Updating a workflow slot")
        body = _optional_params(
            title=_bounded_text(title, "title", 500),
            prompt=_bounded_text(prompt, "prompt", 10_000),
            overrides=overrides,
            slot_at=slot_at,
        )
        if not body:
            raise ToolError("Provide at least one workflow-slot field to update.")
        return _dict_result(await _api(backend, "PATCH", f"/api/workflow-slots/{slot_id}", body=body))

    @server.tool(
        title="Control workflow slot",
        annotations=WRITE,
        structured_output=True,
    )
    async def shortsmaker_control_workflow_slot(
        slot_id: WorkflowSlotId,
        action: Annotated[Literal["skip", "retry", "regenerate_idea"], Field(description="Slot action to perform.")],
        suggested_changes: Annotated[str | None, Field(max_length=1000, description="Optional guidance for a regenerated idea.")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Skip, retry, or regenerate one workflow slot after explicit approval.

        Retry can cause a new generation attempt and regenerate_idea calls the backend idea
        engine. A slot must still be eligible for the selected operation.
        """
        _require_confirmation(confirm, f"Workflow slot {action}")
        if action == "regenerate_idea":
            body = _optional_params(suggested_changes=_bounded_text(suggested_changes, "suggested_changes", 1000))
            return _dict_result(await _api(
                backend,
                "POST",
                f"/api/workflow-slots/{slot_id}/regenerate-idea",
                body=body,
            ))
        if suggested_changes is not None:
            raise ToolError("suggested_changes is only valid for regenerate_idea.")
        return _dict_result(await _api(backend, "POST", f"/api/workflow-slots/{slot_id}/{action}"))
