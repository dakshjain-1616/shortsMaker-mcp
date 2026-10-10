import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp import tools
from shortsmaker_mcp.oauth_auth import Principal, _principal
from shortsmaker_mcp.tools import _common, video

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

    read_tool = server._tool_manager._tools["shortsmaker_get_credit_status"]
    write_tool = server._tool_manager._tools["shortsmaker_create_video"]

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
    tool = server._tool_manager._tools["shortsmaker_create_video"]

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
    tool = server._tool_manager._tools["shortsmaker_create_video"]
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

    assert result == {"id": "job-1", "status": "queued", "credits_cost": 4}
    backend.request.assert_awaited_once_with(
        "POST",
        "/api/jobs",
        "bridge-token",
        params=None,
        body={
            "niche": "deep sea animals",
            "total_length": 15,
            "format": "transformation",
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
        asyncio.run(server._tool_manager._tools["shortsmaker_list_workflows"].fn("paused"))
        asyncio.run(
            server._tool_manager._tools["shortsmaker_list_workflow_slots"].fn(
                workflow_id, "planned", when, when
            )
        )
    finally:
        _principal.reset(context)

    assert backend.request.await_args_list[0].args == (
        "GET", "/api/workflows", "bridge-token"
    )
    assert backend.request.await_args_list[0].kwargs["params"] == {"status": "paused"}
    assert backend.request.await_args_list[1].kwargs["params"] == {
        "status": "planned",
        "from": when.isoformat(),
        "to": when.isoformat(),
    }


def test_create_workflow_validates_and_forwards_confirmed_schedule():
    server, backend = _registered_tools()
    backend.request.return_value = {"id": "workflow-1", "status": "active"}
    tool = server._tool_manager._tools["shortsmaker_create_workflow"]
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(
            tool.fn(
                "Daily ocean facts",
                "Ocean facts for short-form videos",
                [UUID(int=1)],
                "UTC",
                [0, 2],
                ["09:00", "17:30"],
                confirm=True,
            )
        )
    finally:
        _principal.reset(context)

    assert result == {"id": "workflow-1", "status": "active"}
    backend.request.assert_awaited_once_with(
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
        },
    )


def test_workflow_update_requires_at_least_one_field():
    server, backend = _registered_tools()
    tool = server._tool_manager._tools["shortsmaker_update_workflow"]

    with pytest.raises(ToolError, match="at least one workflow field"):
        asyncio.run(tool.fn(UUID(int=1), confirm=True))

    backend.request.assert_not_awaited()


def test_workflow_prompts_require_preview_and_confirmation():
    server, _ = _registered_tools()
    create_prompt = server._prompt_manager._prompts["shortsmaker_create_video_workflow"]
    publish_prompt = server._prompt_manager._prompts["shortsmaker_publish_video_workflow"]
    workflow_prompt = server._prompt_manager._prompts["shortsmaker_workflow_management"]

    create_text = create_prompt.fn("deep sea animals", 15)[0]["content"]["text"]
    publish_text = publish_prompt.fn(UUID(int=0))[0]["content"]["text"]

    assert "resolve the estimated price" in create_text
    assert "confirm=true" in create_text
    assert "never request provider tokens" in publish_text
    assert "queued, not that it has already been uploaded" in publish_text
    assert "planned work" in workflow_prompt.fn(UUID(int=0))[0]["content"]["text"]


def test_video_options_passes_mode_and_resolution_to_lengths():
    server, backend = _registered_tools()
    backend.request.side_effect = lambda method, path, token, **kw: {"path": path, **kw}
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(
            server._tool_manager._tools["shortsmaker_get_video_options"].fn(
                mode="pro", resolution="1080p"
            )
        )
    finally:
        _principal.reset(context)

    assert result["lengths"]["params"] == {"mode": "pro", "resolution": "1080p"}
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


@pytest.mark.parametrize('status', [400, 401, 402, 403, 404, 409, 422])
def test_backend_error_diagnostics_cannot_disclose_internal_requests(status):
    from shortsmaker_mcp.backend_client import BackendAPIError
    diagnostic = 'HTTP error https://10.0.0.5/engine/resolve?access_token=SECRET'
    message = str(_common._backend_error(BackendAPIError(status, diagnostic)))
    assert 'SECRET' not in message and '10.0.0.5' not in message and '/engine/' not in message
