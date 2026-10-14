import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp import tools
from shortsmaker_mcp.oauth_auth import Principal, _principal
from shortsmaker_mcp.tools import _common, video, workflows

TEST_PRINCIPAL = Principal("user-1", frozenset({"mcp:read", "mcp:write"}), "client-1")


def test_job_summary_excludes_large_unneeded_fields():
    result = video._job_summary(
        {
            "id": "job-1",
            "status": "ready",
            "config": {"large": "payload"},
            "video_url": "https://media.example/job-1.mp4",
        }
    )

    assert result == {
        "id": "job-1",
        "status": "ready",
        "video_url": "https://media.example/job-1.mp4",
    }


def test_optional_params_serializes_datetime_and_uuid():
    when = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    result = _common._optional_params(when=when, job_id=UUID(int=0), absent=None)

    assert result == {"when": when.isoformat(), "job_id": str(UUID(int=0))}


def test_resolve_params_cleans_optional_values():
    result = video._resolve_params(
        mode=" lite ",
        total_length=15,
        format=" transformation ",
        resolution="720p",
        audio_mode=" ",
        workflow=" weekly ",
        niche=" deep sea facts ",
    )

    assert result == {
        "mode": "lite",
        "total_length": 15,
        "format": "transformation",
        "resolution": "720p",
        "workflow": "weekly",
        "niche": "deep sea facts",
    }


def test_resolve_params_rejects_oversized_text():
    with pytest.raises(ToolError, match="64 characters or fewer"):
        video._resolve_params(
            mode="lite",
            total_length=15,
            format="x" * 65,
            resolution="720p",
            audio_mode=None,
            workflow=None,
            niche=None,
        )


def test_api_uses_request_context_principal_and_bridge_token():
    backend = AsyncMock()
    backend.request.return_value = {"balance": 12}
    backend.bridge_token = Mock(return_value="bridge-token")

    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(_common._api(backend, "GET", "/api/payments/credit-status"))
    finally:
        _principal.reset(context)

    assert result == {"balance": 12}
    backend.request.assert_awaited_once_with(
        "GET", "/api/payments/credit-status", "bridge-token", params=None, body=None
    )


def _registered_tools():
    server = MCPServer("test")
    backend = AsyncMock()
    backend.bridge_token = Mock(return_value="bridge-token")
    tools.register_tools(server, backend)
    return server, backend


def test_registered_tools_expose_risk_annotations_and_structured_schemas():
    server, _ = _registered_tools()

    read_tool = server._tool_manager._tools["get_credits"]
    write_tool = server._tool_manager._tools["create_video"]

    assert read_tool.annotations.read_only_hint is True
    assert read_tool.annotations.destructive_hint is False
    assert read_tool.output_schema is not None
    assert write_tool.annotations.read_only_hint is False
    assert write_tool.annotations.destructive_hint is True
    assert write_tool.output_schema is not None
    assert write_tool.parameters["properties"]["confirm"]["description"]
    assert "idempotency_key" in write_tool.parameters["required"]


def test_create_video_requires_confirmation_without_calling_backend():
    server, backend = _registered_tools()
    tool = server._tool_manager._tools["create_video"]

    with pytest.raises(ToolError, match="explicit approval"):
        asyncio.run(
            tool.fn(
                "deep sea animals",
                15,
                "request-123",
                confirm=False,
            )
        )

    backend.request.assert_not_awaited()


def test_create_video_forwards_confirmed_idempotent_request():
    server, backend = _registered_tools()
    backend.request.return_value = {
        "id": "job-1",
        "status": "queued",
        "credits_cost": 4,
    }
    tool = server._tool_manager._tools["create_video"]
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(
            tool.fn(
                " deep sea animals ",
                15,
                "request-123",
                confirm=True,
            )
        )
    finally:
        _principal.reset(context)

    assert result.pop("next_step").startswith("Queued, not ready")
    assert result == {"id": "job-1", "status": "queued", "credits_cost": 4}
    backend.request.assert_awaited_once_with(
        "POST",
        "/api/jobs",
        "bridge-token",
        params=None,
        body={
            "niche": "deep sea animals",
            "total_length": 15,
            "format": "auto",
            "mode": "lite",
            "resolution": "720p",
            "idempotency_key": "request-123",
        },
    )


def test_workflow_reader_serializes_filters_and_slot_time_bounds():
    server, backend = _registered_tools()
    backend.request.return_value = []
    workflow_id = UUID(int=1)
    when = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    context = _principal.set(TEST_PRINCIPAL)
    try:
        asyncio.run(server._tool_manager._tools["list_series"].fn("paused"))
        asyncio.run(
            server._tool_manager._tools["list_series_slots"].fn(
                workflow_id, "planned", when, when
            )
        )
    finally:
        _principal.reset(context)

    assert backend.request.await_args_list[0].args == (
        "GET", "/api/workflows", "bridge-token"
    )
    # The status filter is applied after the lifecycle rule, so the backend gets no filter.
    assert backend.request.await_args_list[0].kwargs["params"] is None
    assert backend.request.await_args_list[1].kwargs["params"] == {
        "status": "planned",
        "from": when.isoformat(),
        "to": when.isoformat(),
    }


def test_create_workflow_validates_and_forwards_confirmed_schedule():
    server, backend = _registered_tools()
    backend.request.return_value = {"id": "workflow-1", "status": "active"}
    tool = server._tool_manager._tools["create_series"]
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(
            tool.fn(
                "Daily ocean facts",
                "Ocean facts for short-form videos",
                [UUID(int=1)],
                "UTC",
                [0, 2],
                ["09:00", "17:30", "09:00"],
                job_defaults={"total_length": 30},
                confirm=True,
            )
        )
    finally:
        _principal.reset(context)

    assert result.pop("next_step").startswith("Series created")
    assert result == {"id": "workflow-1", "status": "active"}
    # The video settings are priced first, so settings the backend cannot price fail now.
    resolve = backend.request.await_args_list[0]
    assert resolve.args[:2] == ("GET", "/api/resolve")
    assert resolve.kwargs["params"] == {
        "mode": "lite", "total_length": 30, "format": "auto", "resolution": "720p",
        "niche": "Ocean facts for short-form videos",
    }
    backend.request.assert_awaited_with(
        "POST",
        "/api/workflows",
        "bridge-token",
        params=None,
        body={
            "name": "Daily ocean facts",
            "theme": "Ocean facts for short-form videos",
            "variation": False,
            "connection_ids": [str(UUID(int=1))],
            "visibility": "public",
            "timezone": "UTC",
            "days_of_week": [0, 2],
            "times_of_day": ["09:00", "17:30"],
            "lead_minutes": 360,
            "grace_minutes": 360,
            "job_defaults": {"mode": "lite", "format": "auto", "resolution": "720p", "total_length": 30},
        },
    )


def test_workflow_update_requires_at_least_one_field():
    server, backend = _registered_tools()
    tool = server._tool_manager._tools["update_series"]

    with pytest.raises(ToolError, match="at least one series field"):
        asyncio.run(tool.fn(UUID(int=1), confirm=True))

    backend.request.assert_not_awaited()


def test_workflow_prompts_require_preview_and_confirmation():
    server, _ = _registered_tools()
    create_prompt = server._prompt_manager._prompts["new_video"]
    publish_prompt = server._prompt_manager._prompts["share_video"]
    workflow_prompt = server._prompt_manager._prompts["review_series"]

    create_text = create_prompt.fn("deep sea animals")[0]["content"]["text"]
    series_prompt = server._prompt_manager._prompts["new_series"]
    series_text = series_prompt.fn()[0]["content"]["text"]
    publish_text = publish_prompt.fn(UUID(int=0))[0]["content"]["text"]

    assert "resolve the estimated price" in create_text
    assert "confirm=true" in create_text
    assert "never request provider tokens" in publish_text
    assert "queued, not that it has already been uploaded" in publish_text
    assert "planned work" in workflow_prompt.fn(UUID(int=0))[0]["content"]["text"]
    # Without arguments the guided prompts start by asking, one question at a time.
    assert "offer 3-4" in create_prompt.fn()[0]["content"]["text"]
    assert "one short question at a time" in series_text
    assert "list_connections" in series_text and "confirm=true" in series_text


def test_video_options_passes_format_mode_and_resolution_to_lengths():
    server, backend = _registered_tools()
    backend.request.side_effect = lambda method, path, token, **kw: {"path": path, **kw}
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(
            server._tool_manager._tools["get_video_options"].fn(
                format="pov", mode="pro", resolution="1080p"
            )
        )
    finally:
        _principal.reset(context)

    assert result["lengths"]["params"] == {"format": "pov", "mode": "pro", "resolution": "1080p"}
    assert result["config"]["path"] == "/api/config"


def test_backend_bridge_rejection_does_not_claim_client_token_expired():
    from shortsmaker_mcp.backend_client import BackendAPIError
    error = _common._backend_error(BackendAPIError(401, 'Signature verification failed'))
    assert 'administrator' in str(error)
    assert 'expired' not in str(error)
    assert 'Signature verification' not in str(error)


def test_persisted_diagnostics_are_hidden_but_intended_urls_survive():
    value = {
        'video_url': 'https://media.example/video.mp4?signature=user-media-link',
        'authorization_url': 'https://provider.example/authorize?state=required-state',
        'posts': [{'last_error': 'HTTP error https://private.example/api?access_token=SECRET'}],
        'error': {'request': 'https://internal.example/engine', 'password': 'SECRET'},
        'empty': {'error': None, 'last_error': ''},
    }
    result = _common._public_result(value)
    assert 'SECRET' not in str(result) and 'internal.example' not in str(result)
    assert result['video_url'] == value['video_url']
    assert result['authorization_url'] == value['authorization_url']
    assert result['empty'] == value['empty']
    assert value['posts'][0]['last_error'].endswith('SECRET')
    assert result['posts'][0]['last_error'] == 'HTTP error [link]'


@pytest.mark.parametrize('status', [400, 401, 402, 403, 404, 409, 422])
def test_backend_error_diagnostics_cannot_disclose_internal_requests(status):
    from shortsmaker_mcp.backend_client import BackendAPIError
    diagnostic = 'HTTP error https://10.0.0.5/engine/resolve?access_token=SECRET'
    message = str(_common._backend_error(BackendAPIError(status, diagnostic)))
    assert 'SECRET' not in message and '10.0.0.5' not in message and '/engine/' not in message


def test_backend_sentences_reach_the_model_with_a_next_step():
    from shortsmaker_mcp.backend_client import BackendAPIError, BackendUnavailable
    credits = str(_common._backend_error(BackendAPIError(402, "Not enough credits: this video needs 40, you have 12.")))
    assert "needs 40, you have 12" in credits and "get_credits" in credits
    timezone = str(_common._backend_error(BackendAPIError(422, "Unknown timezone: 'PST'.")))
    assert timezone == "Unknown timezone: 'PST'."
    # 403 here means a route the connector may not use, not a user permission problem.
    assert "website" in str(_common._backend_error(BackendAPIError(403, "MCP access is not allowed")))
    # A write that timed out may have happened, so the model must check before retrying.
    assert "SAME" in str(_common._backend_error(BackendUnavailable(), write=True))
    # Product-written reasons stay readable; a video's engine error does not.
    shown = _common._public_result({"last_error": "Paused. Add credits to resume.", "error": "engine trace"})
    assert shown["last_error"] == "Paused. Add credits to resume." and "engine" not in shown["error"]


def test_videos_list_in_progress_first_and_hide_failures_by_default():
    def job(id, status, minute):
        return {"id": id, "status": status, "created_at": f"2026-10-01T10:{minute:02d}:00+00:00"}

    jobs = [
        job("failed-new", "failed", 50), job("ready-new", "ready", 40), job("cancelled", "cancelled", 30),
        job("making", "processing", 5), job("ready-old", "complete", 1), job("queued", "pending", 20),
        job("future-status", "rendering", 45),
    ]

    shown, hidden = video._working_first(jobs, "default")
    assert [j["id"] for j in shown] == ["making", "queued", "ready-new", "ready-old", "future-status"]
    assert hidden == {"failed": 1, "cancelled": 1}

    assert [j["id"] for j in video._working_first(jobs, "failed")[0]] == ["failed-new"]
    assert [j["id"] for j in video._working_first(jobs, "in_progress")[0]] == ["making", "queued"]
    assert len(video._working_first(jobs, "all")[0]) == len(jobs)


def test_series_list_running_first_and_ended_series_count_as_stopped():
    series = [
        {"id": "paused-new", "status": "paused", "created_at": "2026-10-05"},
        {"id": "stopped", "status": "stopped", "created_at": "2026-10-04"},
        {"id": "active-old", "status": "active", "created_at": "2026-09-01"},
        {"id": "ended", "status": "active", "created_at": "2026-10-03", "ends_at": "2026-10-01T00:00:00Z"},
        {"id": "active-new", "status": "active", "created_at": "2026-10-02", "ends_at": "2099-01-01T00:00:00Z"},
    ]

    listed = workflows._running_first([dict(s) for s in series], None)
    assert [s["id"] for s in listed["series"]] == ["active-new", "active-old", "paused-new"]
    assert listed["stopped_count"] == 2

    stopped = workflows._running_first([dict(s) for s in series], "stopped")
    assert [s["id"] for s in stopped["series"]] == ["stopped", "ended"]  # newest first
    assert all(s["status"] == "stopped" for s in stopped["series"])


def test_publish_refuses_repeat_posts_and_unchosen_tiktok_privacy():
    from shortsmaker_mcp.tools import publishing
    yt, tt = str(UUID(int=1)), str(UUID(int=2))
    backend = Mock()
    backend.bridge_token.return_value = "bridge-token"
    backend.request = AsyncMock(side_effect=lambda method, path, token, **kw: {
        "/api/connections": [
            {"id": yt, "platform": "youtube", "account_name": "MyChannel"},
            {"id": tt, "platform": "tiktok", "account_name": "me"},
        ],
        "/api/posts": [{"connection_id": yt, "status": "published"}],
    }[path])
    check = publishing._check_destinations
    context = _principal.set(TEST_PRINCIPAL)
    try:
        with pytest.raises(ToolError, match="TikTok requires"):
            asyncio.run(check(backend, UUID(int=9), {tt}, None, False))
        with pytest.raises(ToolError, match=r"MyChannel \(youtube\): published"):
            asyncio.run(check(backend, UUID(int=9), {yt}, None, False))
        asyncio.run(check(backend, UUID(int=9), {yt}, None, True))  # the user asked to repost
        asyncio.run(check(backend, UUID(int=9), {tt}, "private", False))
    finally:
        _principal.reset(context)


def test_series_update_merges_video_settings_instead_of_replacing_them():
    server, backend = _registered_tools()
    backend.request.side_effect = lambda method, path, token, **kw: (
        {"theme": "space", "job_defaults": {"total_length": 30, "audio_mode": "narration"}}
        if method == "GET" and path.startswith("/api/workflows/") else {"ok": True}
    )
    context = _principal.set(TEST_PRINCIPAL)
    try:
        asyncio.run(server._tool_manager._tools["update_series"].fn(
            UUID(int=1), job_defaults={"format": "pov"}, confirm=True,
        ))
    finally:
        _principal.reset(context)

    patch = backend.request.await_args_list[-1]
    assert patch.args[:2] == ("PATCH", f"/api/workflows/{UUID(int=1)}")
    assert patch.kwargs["body"]["job_defaults"] == {
        "mode": "lite", "format": "pov", "resolution": "720p", "total_length": 30, "audio_mode": "narration",
    }
