import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp import tools
from shortsmaker_mcp.backend_client import BackendAPIError
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
        "title": "Untitled video",
        "status": "ready",
        "status_label": "Ready",
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
    assert "preview_token" in write_tool.parameters["properties"]
    assert "idempotency_key" not in write_tool.parameters.get("required", [])


def _routes(responses):
    """A backend that answers by path; POSTs return responses["POST " + path]."""
    config = {
        "resolutions": ["720p", "1080p"],
        "resolutions_by_format": {
            "transformation": {"available": ["720p", "1080p"], "default": "720p"},
            "asmr": {"available": ["1080p"], "default": "1080p"},
            "pov": {"available": ["720p", "1080p"], "default": "720p"},
        },
    }
    async def respond(method, path, token, **kwargs):
        key = path if method == "GET" else f"{method} {path}"
        if method == "GET" and path == "/api/config" and path not in responses:
            return config
        return responses.get(key, {})
    return respond


PRICED = {
    "/api/config": {
        "resolutions": ["720p", "1080p"],
        "resolutions_by_format": {
            "transformation": {"available": ["720p", "1080p"], "default": "720p"},
            "asmr": {"available": ["1080p"], "default": "1080p"},
        },
    },
    "/api/resolve": {"estimated_credits": 18, "total_length": 15, "requested_length": 15},
    "/api/payments/credit-status": {"balance": 120},
    "/api/jobs": [],
    "POST /api/jobs": {"id": "job-1", "status": "queued", "credits_cost": 18},
}


def test_omitted_resolution_is_pinned_for_the_legacy_backend():
    server, backend = _registered_tools()
    backend.request.side_effect = _routes(PRICED)
    context = _principal.set(TEST_PRINCIPAL)
    try:
        asyncio.run(server._tool_manager._tools["get_video_price"].fn(12, niche="A satisfying marble rolling through a neon obstacle course."))
        asyncio.run(server._tool_manager._tools["create_video"].fn("A satisfying marble rolling through a neon obstacle course.", 12))
    finally:
        _principal.reset(context)
    resolves = [call.kwargs["params"] for call in backend.request.await_args_list
                if call.args[:2] == ("GET", "/api/resolve")]
    assert len(resolves) == 2
    assert all(params["resolution"] == "720p" for params in resolves)


def test_auto_resolution_retries_supported_catalogue_choices_on_resolution_error():
    server, backend = _registered_tools()
    calls = []

    async def respond(method, path, token, **kwargs):
        if path == "/api/config":
            return PRICED["/api/config"]
        if path == "/api/resolve":
            params = kwargs["params"]
            calls.append(params["resolution"])
            if params["resolution"] == "720p":
                raise BackendAPIError(422, "This format doesn't offer resolution '720p'; it offers 1080p.")
            return {"format": "asmr", "resolution": "1080p", "estimated_credits": 53,
                    "estimated_cost": .53, "total_length": 12, "requested_length": 12}
        return PRICED.get(path if method == "GET" else f"{method} {path}", {})

    backend.request.side_effect = respond
    context = _principal.set(TEST_PRINCIPAL)
    try:
        quote = asyncio.run(server._tool_manager._tools["get_video_price"].fn(
            12, niche="A satisfying marble rolling through a neon obstacle course."))
    finally:
        _principal.reset(context)
    assert calls == ["720p", "1080p"]
    assert quote["format"] == "asmr" and quote["resolution"] == "1080p"
    assert quote["estimated_credits"] == 53


def test_marble_price_and_approved_creation_use_the_same_supported_settings():
    server, backend = _registered_tools()
    prompt = "A satisfying marble rolling through a neon obstacle course."
    resolves = []

    async def respond(method, path, token, **kwargs):
        if path == "/api/config":
            return PRICED["/api/config"]
        if path == "/api/resolve":
            params = kwargs["params"]
            resolves.append((params.get("format"), params["resolution"]))
            if params["resolution"] == "720p":
                raise BackendAPIError(422, "This format doesn't offer resolution '720p'; it offers 1080p.")
            return {"format": "asmr", "resolution": "1080p", "estimated_credits": 53,
                    "total_length": 12, "requested_length": 12}
        key = path if method == "GET" else f"{method} {path}"
        return PRICED.get(key, {})

    backend.request.side_effect = respond
    create = server._tool_manager._tools["create_video"].fn
    price = server._tool_manager._tools["get_video_price"].fn
    context = _principal.set(TEST_PRINCIPAL)
    try:
        quote = asyncio.run(price(12, niche=prompt))
        preview = asyncio.run(create(prompt, 12))
        result = asyncio.run(create(prompt, 12, preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)

    assert quote["format"] == preview["settings"]["format"] == "asmr"
    assert quote["resolution"] == preview["settings"]["resolution"] == "1080p"
    assert quote["estimated_credits"] == preview["estimated_credits"] == 53
    assert result["status"] == "queued"
    assert resolves == [("auto", "720p"), ("auto", "1080p")] * 3
    create_body = next(call.kwargs["body"] for call in backend.request.await_args_list
                       if call.args[:2] == ("POST", "/api/jobs"))
    assert create_body["format"] == "asmr" and create_body["resolution"] == "1080p"


def test_create_video_without_confirm_returns_a_preview_and_never_writes():
    server, backend = _registered_tools()
    backend.request.side_effect = _routes(PRICED)
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(server._tool_manager._tools["create_video"].fn("deep sea animals", 15))
    finally:
        _principal.reset(context)

    assert preview["status"] == "needs_approval" and preview["estimated_credits"] == 18
    assert preview["summary"] == "This video costs 18 credits. You have 120, enough for about 6 like this."
    assert preview["preview_token"] in preview["next_step"] and preview["settings"]["total_length"] == 15
    assert {c.args[0] for c in backend.request.await_args_list} == {"GET"}


def test_create_video_confirmed_sends_a_stable_automatic_key_under_the_price_lock():
    server, backend = _registered_tools()
    backend.request.side_effect = _routes(PRICED)
    create = server._tool_manager._tools["create_video"].fn
    context = _principal.set(TEST_PRINCIPAL)
    try:
        token = asyncio.run(create(" deep sea animals ", 15))["preview_token"]
        result = asyncio.run(create(" deep sea animals ", 15, preview_token=token, confirm=True))
        with pytest.raises(ToolError, match="Call create_video without confirm first.*needs_approval"):
            asyncio.run(create("deep sea animals", 15, confirm=True))
        with pytest.raises(ToolError, match="changed since.*Nothing was done.*preview_changed"):
            asyncio.run(create("deep sea plants", 15, preview_token=token, confirm=True))
    finally:
        _principal.reset(context)

    assert result["status_label"] == "In line" and "18 credits" in result["summary"]
    posts = [c for c in backend.request.await_args_list if c.args[0] == "POST"]
    assert len(posts) == 1
    body = posts[0].kwargs["body"]
    assert body["niche"] == "deep sea animals" and body["idempotency_key"].startswith("auto-")
    # The key is derived from the original request before resolved settings are pinned.
    request = video._create_params(
        niche="deep sea animals", total_length=15, format="auto", mode="lite",
        resolution=None, style=None, audio_mode=None, narrator_voice=None,
        narration_language=None, music_track=None, idempotency_key="pending",
    )
    assert video._auto_key(request) == body["idempotency_key"]
    assert video._auto_key({**request, "total_length": 30}) != body["idempotency_key"]


def test_confirm_refuses_when_auto_picks_another_format_and_sends_the_previewed_one():
    server, backend = _registered_tools()
    picked = {"format": "drone"}

    async def respond(method, path, token, **kwargs):
        if path == "/api/resolve":
            return {"estimated_credits": 18, "total_length": 15.0, **picked}
        return PRICED.get(path if method == "GET" else f"{method} {path}", {})
    backend.request.side_effect = respond
    create = server._tool_manager._tools["create_video"].fn
    context = _principal.set(TEST_PRINCIPAL)
    try:
        token = asyncio.run(create("city at night", 15))["preview_token"]
        picked["format"] = "timelapse"  # the engine's Auto choice moved between preview and yes
        with pytest.raises(ToolError, match="preview_changed"):
            asyncio.run(create("city at night", 15, preview_token=token, confirm=True))
        assert not [c for c in backend.request.await_args_list if c.args[0] == "POST"]
        picked["format"] = "drone"
        asyncio.run(create("city at night", 15, preview_token=token, confirm=True))
    finally:
        _principal.reset(context)
    body = next(c for c in backend.request.await_args_list if c.args[0] == "POST").kwargs["body"]
    assert body["format"] == "drone" and body["total_length"] == 15 and isinstance(body["total_length"], int)


@pytest.mark.parametrize(("status", "cost", "fragment"), [
    ("paused", 0, "Resuming is free"), ("failed", 18, "costs 18 credits. You have 120"),
])
def test_retry_video_previews_the_real_cost_then_retries(status, cost, fragment):
    server, backend = _registered_tools()
    job = {"id": str(UUID(int=7)), "status": status, "title": "Marble race", "credits_cost": 18}
    backend.request.side_effect = _routes({**PRICED, f"/api/jobs/{job['id']}": job, f"POST /api/jobs/{job['id']}/retry": {"status": "queued"}})
    retry = server._tool_manager._tools["retry_video"].fn
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(retry(job["id"]))
        done = asyncio.run(retry(job["id"], preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)
    assert preview["status"] == "needs_approval" and preview["credits"] == cost and fragment in preview["summary"]
    assert done["status_label"] == "In line"
    assert [c.args[1] for c in backend.request.await_args_list if c.args[0] == "POST"] == [f"/api/jobs/{job['id']}/retry"]


def test_retry_video_refuses_a_video_that_is_ready():
    server, backend = _registered_tools()
    job = {"id": str(UUID(int=7)), "status": "ready", "credits_cost": 18}
    backend.request.side_effect = _routes({**PRICED, f"/api/jobs/{job['id']}": job})
    context = _principal.set(TEST_PRINCIPAL)
    try:
        with pytest.raises(ToolError, match="nothing to try again.*wrong_state"):
            asyncio.run(server._tool_manager._tools["retry_video"].fn(job["id"]))
    finally:
        _principal.reset(context)


def test_adjustments_name_what_the_engine_changed():
    changes = video._adjustments(
        {"total_length": 60, "audio_mode": "dialogue", "resolution": "720p"},
        {"requested_length": 60, "total_length": 15, "audio_mode": "ambient", "resolution": "720p"},
    )
    assert changes == [
        "Length changed from 60s to 15s, the closest this format allows.",
        "Sound changed from dialogue to natural sound, because this format can't do the first.",
    ]


def test_recent_failures_counts_only_the_last_hour():
    jobs = [
        {"status": "failed", "updated_at": "2026-10-15T11:30:00+00:00"},
        {"status": "failed", "updated_at": "2026-10-15T11:59:00+00:00"},
        {"status": "failed", "updated_at": "2026-10-15T09:00:00+00:00"},
        {"status": "ready", "updated_at": "2026-10-15T11:59:00+00:00"},
    ]
    assert video._recent_failures(jobs, datetime(2026, 10, 15, 12, 0, tzinfo=UTC)) == 2


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
    slots = next(c for c in backend.request.await_args_list if c.args[1].endswith("/slots"))
    assert slots.kwargs["params"] == {
        "status": "planned",
        "from": when.isoformat(),
        "to": when.isoformat(),
    }


def test_create_series_previews_weekly_cost_then_forwards_the_confirmed_schedule():
    server, backend = _registered_tools()
    created = {"id": "workflow-1", "name": "Ocean facts for short-form videos", "status": "active",
               "days_of_week": [0, 2], "times_of_day": ["09:00", "17:30"], "timezone": "UTC",
               "connection_ids": [str(UUID(int=1))], "visibility": "public"}
    backend.request.side_effect = _routes({
        "/api/resolve": {"estimated_credits": 18},
        "/api/payments/credit-status": {"balance": 720},
        "/api/connections": [{"id": str(UUID(int=1)), "account_name": "Ocean TV", "platform": "youtube"}],
        "/api/workflows": [{**created, "next_slot_at": "2030-01-07T09:00:00+00:00"}],
        "POST /api/workflows": created,
    })
    tool = server._tool_manager._tools["create_series"]
    args = ("Ocean facts for short-form videos", [UUID(int=1)], "UTC", [0, 2], ["09:00", "17:30", "09:00"], "public")
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(tool.fn(*args))
        result = asyncio.run(tool.fn(*args, preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)

    assert preview["status"] == "needs_approval" and preview["credits_per_week"] == 72
    assert preview["summary"] == (
        '"Ocean facts for short-form videos": Every Mon, Wed at 9:00 AM and 5:30 PM UTC, posts to '
        "Ocean TV (YouTube), public. About 18 credits a video, 4 a week = 72 credits a week. "
        "Your 720 credits cover about 10 weeks."
    )
    assert result["summary"].startswith('Started "Ocean facts for short-form videos"')
    assert "First video" in result["summary"]
    # The video settings are priced first, so settings the backend cannot price fail now.
    resolve = next(c for c in backend.request.await_args_list if c.args[1] == "/api/resolve")
    assert resolve.kwargs["params"] == {
        "mode": "lite", "total_length": 30, "format": "auto",
        "niche": "Ocean facts for short-form videos", "resolution": "720p",
    }
    posts = [c for c in backend.request.await_args_list if c.args[0] == "POST"]
    assert len(posts) == 1
    assert posts[0].kwargs["body"] == {
        "name": "Ocean facts for short-form videos",
        "theme": "Ocean facts for short-form videos",
        "variation": False,
        "connection_ids": [str(UUID(int=1))],
        "visibility": "public",
        "timezone": "UTC",
        "days_of_week": [0, 2],
        "times_of_day": ["09:00", "17:30"],
        "lead_minutes": 360,
        "grace_minutes": 360,
        "job_defaults": {"mode": "lite", "format": "auto", "total_length": 30, "resolution": "720p"},
    }


def test_short_name_keeps_the_first_words():
    assert workflows._short_name("Daily facts about the deep ocean and its strange animals") == "Daily facts about the deep ocean…"


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
    publish_text = publish_prompt.fn("the marble one")[0]["content"]["text"]

    assert "preview_token" in create_text
    assert "confirm=true" in create_text
    assert "never request passwords or tokens" in publish_text
    assert "queued, not that it has already been uploaded" in publish_text
    assert "upcoming videos" in workflow_prompt.fn("space facts")[0]["content"]["text"]
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
    assert timezone == "Unknown timezone: 'PST'. [code: invalid_input]"
    # 403 here means a route the connector may not use, not a user permission problem.
    assert "website" in str(_common._backend_error(BackendAPIError(403, "MCP access is not allowed")))
    # A write that timed out may have happened, so the model must check before retrying.
    assert str(_common._backend_error(BackendUnavailable(), write=True)).endswith("[code: outcome_unknown]")
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


def test_publish_refuses_repeat_posts_and_accounts_that_need_reconnecting():
    from shortsmaker_mcp.tools import publishing
    yt, tt, ig = str(UUID(int=1)), str(UUID(int=2)), str(UUID(int=3))
    health = {"worker_alive": True, "worker_platforms": ["youtube", "tiktok"]}
    backend = Mock()
    backend.bridge_token.return_value = "bridge-token"
    backend.request = AsyncMock(side_effect=lambda method, path, token, **kw: {
        "/api/connections": [
            {"id": yt, "platform": "youtube", "account_name": "MyChannel", "status": "active"},
            {"id": tt, "platform": "tiktok", "account_name": "me", "status": "error"},
            {"id": ig, "platform": "instagram", "account_name": "pics", "status": "active"},
        ],
        "/api/posts": [{"connection_id": yt, "status": "published", "platform_post_url": "u"}],
        "/api/publishing/health": health,
    }[path])
    check = publishing._check_destinations
    context = _principal.set(TEST_PRINCIPAL)
    try:
        with pytest.raises(ToolError, match=r"me \(TikTok\) needs reconnecting.*needs_reconnect"):
            asyncio.run(check(backend, "9", {tt}, False))
        with pytest.raises(ToolError, match=r"MyChannel \(YouTube\): live.*already_posted"):
            asyncio.run(check(backend, "9", {yt}, False))
        # Connected, but posting doesn't reach Instagram: refused, not queued forever.
        with pytest.raises(ToolError, match=r"Instagram is unavailable.*platform_unavailable"):
            asyncio.run(check(backend, "9", {ig}, True))
        # The user asked to repost.
        assert asyncio.run(check(backend, "9", {yt}, True)) == ({yt: "MyChannel (YouTube)"}, "")
        # Posting is down: fail before queueing, including on a retry.
        health["worker_alive"] = False
        with pytest.raises(ToolError, match="platform_unavailable"):
            asyncio.run(check(backend, "9", {yt}, True))
    finally:
        _principal.reset(context)


def test_publish_preview_names_the_places_time_and_visibility():
    server, backend = _registered_tools()
    yt, ig = str(UUID(int=1)), str(UUID(int=2))
    backend.request.side_effect = _routes({
        f"/api/jobs/{UUID(int=9)}": {"id": str(UUID(int=9)), "status": "ready", "title": "Marble race"},
        "/api/connections": [
            {"id": yt, "platform": "youtube", "account_name": "MyChannel", "status": "active"},
            {"id": ig, "platform": "instagram", "account_name": "me", "status": "active"},
        ],
        "/api/posts": [],
        "/api/publishing/health": {"worker_alive": True,
                                    "operational_platforms": ["youtube", "instagram"]},
    })
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(server._tool_manager._tools["publish_video"].fn(
            str(UUID(int=9)), ["my youtube", "instagram"], "private",
        ))
    finally:
        _principal.reset(context)
    assert preview["summary"] == (
        'Post "Marble race" to MyChannel (YouTube), me (Instagram) right away, private. '
        "Instagram posts are always public."
    )
    assert preview["status"] == "needs_approval"
    assert preview["preview"]["accounts"] == {yt: "MyChannel (YouTube)", ig: "me (Instagram)"}
    assert preview["preview_token"].startswith("p-")
    assert all(c.args[0] == "GET" for c in backend.request.await_args_list)


def test_publish_needs_matching_preview_token_before_queueing():
    server, backend = _registered_tools()
    yt = str(UUID(int=1))
    job_id = str(UUID(int=9))
    backend.request.side_effect = _routes({
        f"/api/jobs/{job_id}": {"id": job_id, "status": "ready", "title": "Marble race"},
        "/api/connections": [{"id": yt, "platform": "youtube", "account_name": "Channel", "status": "active"}],
        "/api/publishing/health": {"worker_alive": True, "operational_platforms": ["youtube"]},
        "/api/posts": [],
        "POST /api/posts": [{"id": "post-1", "status": "scheduled", "connection_id": yt}],
    })
    tool = server._tool_manager._tools["publish_video"]
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(tool.fn(job_id, [yt], "public"))
        with pytest.raises(ToolError, match="needs_approval"):
            asyncio.run(tool.fn(job_id, [yt], "public", confirm=True))
        with pytest.raises(ToolError, match="preview_changed"):
            asyncio.run(tool.fn(job_id, [yt], "private", confirm=True,
                                    preview_token=preview["preview_token"]))
        assert not any(c.args[0] == "POST" for c in backend.request.await_args_list)
        result = asyncio.run(tool.fn(job_id, [yt], "public", confirm=True,
                                     preview_token=preview["preview_token"]))
        assert result["items"][0]["id"] == "post-1"
        writes = [c for c in backend.request.await_args_list if c.args[0] == "POST"]
        assert len(writes) == 1
        assert "idempotency_key" not in writes[0].kwargs["body"]
    finally:
        _principal.reset(context)


def test_names_resolve_to_one_item_or_ask_which():
    items = [{"id": "1", "title": "Marble race"}, {"id": "2", "title": "Marble run"}, {"id": "3", "title": "Cats"}]
    text, describe = (lambda i: i["title"]), (lambda i: f"'{i['title']}'")
    assert _common._pick(items, "the marble race one", text, describe, "video")["id"] == "1"
    with pytest.raises(ToolError, match="Several videos match 'marble'.*Marble race.*Marble run"):
        _common._pick(items, "marble", text, describe, "video")
    with pytest.raises(ToolError, match="No video matches 'dogs'. Recent ones"):
        _common._pick(items, "dogs", text, describe, "video")


def test_series_update_merges_video_settings_instead_of_replacing_them():
    server, backend = _registered_tools()
    backend.request.side_effect = lambda method, path, token, **kw: (
        {"theme": "space", "job_defaults": {"total_length": 30, "audio_mode": "narration"}}
        if method == "GET" and path.startswith("/api/workflows/") else {"ok": True}
    )
    context = _principal.set(TEST_PRINCIPAL)
    try:
        update = server._tool_manager._tools["update_series"].fn
        preview = asyncio.run(update(UUID(int=1), job_defaults={"format": "pov"}))
        asyncio.run(update(UUID(int=1), job_defaults={"format": "pov"},
                           preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)

    patch = next(c for c in backend.request.await_args_list if c.args[0] == "PATCH")
    assert patch.args[1] == f"/api/workflows/{UUID(int=1)}"
    assert patch.kwargs["body"]["job_defaults"] == {
        "mode": "lite", "format": "pov", "total_length": 30, "audio_mode": "narration", "resolution": "720p",
    }


# ---- Plain-language output ----

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_when_uses_series_timezone_or_relative_time_and_tolerates_bad_suffix():
    assert _common._when("2026-10-15T14:30:00+00:00", "Asia/Kolkata", now=NOW) == "today 8:00 PM IST"
    assert _common._when("2026-10-16T08:00:00+00:00", "Europe/London", now=NOW) == "tomorrow 9:00 AM BST"
    assert _common._when("2026-10-15T10:00:00+00:00Z", now=NOW) == "2 hours ago"
    assert _common._when("2026-10-18T12:00:00Z", now=NOW) == "in 3 days"
    assert _common._when("not a time") == ""


def test_status_labels_cover_both_spellings():
    assert _common._label("canceled") == _common._label("cancelled") == "Cancelled"
    assert _common._label("queued") == _common._label("pending") == "In line"
    assert _common._label("brand_new") == "Brand_new"


def test_raw_backend_messages_become_plain_sentences():
    assert _common._public_text("times_of_day.0: Value error, Time must be HH:MM") == "Time must be HH:MM"
    assert "having trouble" in _common._public_text("FAL_AI_KEY not set")
    error = _common._backend_error(
        _common.BackendAPIError(503, "We can't price this video right now."), write=True, spends=True
    )
    assert str(error) == _common._ENGINE_TROUBLE + " No credits were used. Try again in a few minutes. [code: service_busy]"


@pytest.mark.parametrize(("status", "code"), [
    (400, "invalid_input"), (401, "connector_auth"), (402, "insufficient_credits"),
    (403, "not_available_here"), (404, "not_found"), (409, "wrong_state"), (422, "invalid_input"),
    (429, "rate_limited"), (500, "server_error"), (503, "service_busy"),
])
def test_every_backend_error_carries_a_code(status, code):
    assert str(_common._backend_error(_common.BackendAPIError(status, "x"))).endswith(f"[code: {code}]")


def test_errors_keep_the_wait_and_the_support_reference():
    busy = _common._backend_error(_common.BackendAPIError(429, "slow down", retry_after=30))
    assert "about 30 seconds" in str(busy) and "loop" in str(busy)
    crash = _common._backend_error(_common.BackendAPIError(500, "Something went wrong on our side. Reference ab12-cd"))
    assert "reference: ab12-cd" in str(crash) and "[code: server_error]" in str(crash)
    assert str(_common._backend_error(ValueError())).endswith("[code: unexpected]")


def test_failed_video_has_no_link_and_list_row_is_slim():
    row = video._job_summary({
        "id": "j", "status": "failed", "niche": "marble race", "video_url": "https://x/v.mp4",
        "thumbnail_urls": {"before": "https://x/b.jpg", "after": "https://x/a.jpg"},
        "tags": ["a"], "description": "long", "created_at": "2026-10-15T10:00:00+00:00",
    })
    assert "video_url" not in row and "tags" not in row and "description" not in row
    assert row["thumbnail"] == "https://x/a.jpg"
    assert row["title"] == "marble race" and row["status_label"].startswith("Didn't work")


def test_post_without_platform_link_is_sent_not_verified():
    from shortsmaker_mcp.tools import publishing

    assert publishing._post_row({"status": "published"})["state"] == "sent"
    assert publishing._post_row({"status": "published", "platform_post_url": "u"})["state"] == "published"


def test_post_states_cover_every_backend_status():
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    state = lambda **post: _common._post_state(post, now)  # noqa: E731
    assert state(status="scheduled", scheduled_at="2026-10-15T11:59:00Z")["state"] == "queued"
    later = state(status="scheduled", scheduled_at="2026-10-17T12:00:00Z")
    assert later["state"] == "scheduled" and later["status_label"] == "Goes out in 2 days"
    retrying = state(status="scheduled", attempts=2, max_attempts=5, next_attempt_at="2026-10-15T12:10:00Z")
    assert retrying == {"state": "queued", "status_label": "Trying again in 10 minutes",
                        "tries": "2 of 5", "next_try": "in 10 minutes"}
    assert state(status="publishing")["state"] == "publishing"
    assert state(status="published")["status_label"] == "Sent, not confirmed live"
    assert state(status="published", platform_post_url="u")["status_label"] == "Live"
    assert state(status="canceled")["state"] == "cancelled"
    failed = state(status="failed", last_error="Token expired, reconnect the account")
    assert failed["state"] == "failed" and failed["fix"].startswith("Reconnect")
    assert state(status="failed", last_error="Upload rejected")["fix"] == "Retry the post"
    assert "credits" not in failed["status_label"]


def test_connected_is_not_the_same_as_can_post():
    from shortsmaker_mcp.tools import publishing
    yt = {"platform": "youtube", "status": "active"}
    assert publishing._posting(yt, {})[0] == "unavailable"
    assert publishing._posting(yt, {"worker_alive": True, "worker_platforms": ["tiktok"]})[0] == "unavailable"
    assert publishing._posting(yt, {"worker_alive": False})[0] == "unavailable"
    assert publishing._posting({**yt, "status": "error"}, {})[1] == "Reconnect first"
    row = publishing._connection_row(yt, {"worker_alive": True, "worker_platforms": ["youtube"]})
    assert row["status_label"] == "Connected" and row["can_post"] is True


def test_publishing_health_reports_each_connected_platform():
    server, backend = _registered_tools()
    backend.request.side_effect = _routes({
        "/api/publishing/health": {"worker_alive": True, "operational_platforms": ["youtube"]},
        "/api/connections": [
            {"id": "youtube-1", "platform": "youtube", "status": "active"},
            {"id": "tiktok-1", "platform": "tiktok", "status": "active"},
        ],
    })
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(server._tool_manager._tools["check_publishing_health"].fn())
    finally:
        _principal.reset(context)
    assert [(c["platform"], c["can_post"]) for c in result["accounts"]] == [
        ("youtube", True), ("tiktok", False),
    ]
    assert "unavailable" in result["summary"]


def test_series_slot_rows_show_video_posts_retries_and_the_fix():
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    retrying = workflows._slot_row({
        "id": "s1", "status": "planned", "slot_at": "2026-10-15T20:00:00Z", "attempts": 1, "max_attempts": 3,
        "last_error": "Retrying (1 of 3): engine busy", "next_attempt_at": "2026-10-15T12:30:00Z",
    }, "UTC", now)
    assert retrying["tries"] == "1 of 3" and retrying["next_try"] == "today 12:30 PM UTC" and "fix" not in retrying
    posted = workflows._slot_row({
        "id": "s2", "status": "promoted", "job_id": "j", "job_status": "succeeded",
        "posts": [{"status": "failed", "platform": "youtube", "account_name": "Ocean TV", "last_error": "Token revoked"}],
    }, "UTC", now)
    assert posted["video"] == "Ready" and posted["posting"][0]["account"] == "Ocean TV (YouTube)"
    assert posted["posting"][0]["state"] == "failed" and posted["fix"].startswith("Reconnect")
    assert workflows._slot_row({"status": "failed"}, None, now)["fix"].startswith("Try it again")
    assert workflows._slot_row({"status": "skipped", "last_error": "Not enough credits"}, None, now)["fix"].startswith("Add credits")
    assert "fix" not in workflows._slot_row({"status": "skipped", "last_error": "Skipped by you"}, None, now)


def test_schedule_line_reads_like_a_sentence():
    line = workflows._schedule_line(
        {"days_of_week": [0, 1, 2, 3, 4, 5, 6], "times_of_day": ["20:00"], "timezone": "Asia/Kolkata",
         "connection_ids": ["c1"], "visibility": "public"},
        {"c1": "My Channel (YouTube)"},
    )
    assert line == "Every day at 8:00 PM IST, posts to My Channel (YouTube), public"


def test_get_series_merges_next_time_from_the_list():
    server, backend = _registered_tools()
    single = {"id": "s1", "name": "Space", "status": "active", "next_slot_at": None, "counts": {},
              "days_of_week": [0], "times_of_day": ["09:00"], "timezone": "UTC", "connection_ids": []}
    listed = [{**single, "next_slot_at": "2030-01-07T09:00:00+00:00", "counts": {"planned": 3}}]

    async def respond(method, path, token, **kwargs):
        return {"/api/workflows": listed, "/api/connections": []}.get(path, single)

    backend.request.side_effect = respond
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(server._tool_manager._tools["get_series"].fn(UUID(int=1)))
    finally:
        _principal.reset(context)
    assert result["counts"] == {"planned": 3}
    assert "Next video" in result["summary"] and result["next_video"].endswith("UTC")


def test_series_update_previews_before_and_after_then_offers_undo():
    server, backend = _registered_tools()
    sid = str(UUID(int=1))
    series = {"id": sid, "name": "Space", "status": "active", "timezone": "Asia/Kolkata",
              "days_of_week": [0, 1, 2, 3, 4, 5, 6], "times_of_day": ["20:00"], "connection_ids": []}
    backend.request.side_effect = _routes({
        f"/api/workflows/{sid}": series,
        "/api/workflows": [{**series, "counts": {"planned": 5}}],
        "/api/connections": [],
        f"PATCH /api/workflows/{sid}": {**series, "times_of_day": ["21:00"]},
    })
    update = server._tool_manager._tools["update_series"].fn
    context = _principal.set(TEST_PRINCIPAL)
    try:
        preview = asyncio.run(update(sid, times_of_day=["21:00"], name="Space"))
        done = asyncio.run(update(sid, times_of_day=["21:00"], name="Space",
                                  preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)

    # The unchanged name is not listed, and only the real change is sent.
    assert preview["summary"] == (
        'Change "Space": time from 8:00 PM to 9:00 PM. This re-plans your 5 upcoming videos; '
        "any edits you made to them are lost."
    )
    patch = next(c for c in backend.request.await_args_list if c.args[0] == "PATCH")
    assert patch.kwargs["body"] == {"times_of_day": ["21:00"]}
    assert done["changed"] == {"times_of_day": {"from": ["20:00"], "to": ["21:00"]}}
    assert done["summary"].startswith("Changed time from 8:00 PM to 9:00 PM.") and 'Say "undo"' in done["summary"]


def test_upcoming_videos_are_the_next_planned_ones_soonest_first_in_series_time():
    server, backend = _registered_tools()
    sid = str(UUID(int=1))
    backend.request.side_effect = _routes({
        f"/api/workflows/{sid}": {"id": sid, "name": "Space", "timezone": "Asia/Kolkata"},
        f"/api/workflows/{sid}/slots": [
            {"id": "b", "slot_at": "2030-01-02T14:30:00+00:00", "status": "planned", "title": "Moons", "prompt": "p2"},
            {"id": "x", "slot_at": "2030-01-01T10:00:00+00:00", "status": "skipped", "title": "Old"},
            {"id": "a", "slot_at": "2030-01-01T14:30:00+00:00", "status": "planned", "title": "Mars", "prompt": "p1"},
            {"id": "f", "slot_at": "2029-12-31T14:30:00+00:00", "status": "failed", "title": "Comets",
             "last_error": "Gave up after 3 tries"},
        ],
    })
    context = _principal.set(TEST_PRINCIPAL)
    try:
        result = asyncio.run(server._tool_manager._tools["list_series_slots"].fn(sid))
    finally:
        _principal.reset(context)
    assert [row["title"] for row in result["items"]] == ["Mars", "Moons"]
    assert result["items"][0]["when"] == "Tue 1 Jan 8:00 PM IST"
    slots_call = next(c for c in backend.request.await_args_list if c.args[1].endswith("/slots"))
    assert "from" in slots_call.kwargs["params"] and "status" not in slots_call.kwargs["params"]
    assert result["summary"].startswith('Next in "Space": Tue 1 Jan 8:00 PM IST: "Mars"')
    # The same mock answers the last-week read: the failed one needs attention, with its fix.
    assert [row["title"] for row in result["needs_attention"]] == ["Comets"]
    assert '"Comets" (Try it again' in result["summary"] and "fix" in result["next_step"]


def test_cancel_waits_for_the_video_to_stop_and_says_credits_are_back(monkeypatch):
    monkeypatch.setattr(video, "_SETTLE_POLL", 0)
    server, backend = _registered_tools()
    jid = str(UUID(int=3))
    states = iter([{"status": "processing", "credits_cost": 53},
                   {"status": "processing", "credits_cost": 53},
                   {"status": "cancelled", "credits_cost": 53}])

    async def respond(method, path, token, **kwargs):
        return {"status": "cancelling"} if method == "POST" else {"id": jid, **next(states)}

    backend.request.side_effect = respond
    context = _principal.set(TEST_PRINCIPAL)
    try:
        control = server._tool_manager._tools["control_video"].fn
        preview = asyncio.run(control(jid, "cancel"))
        result = asyncio.run(control(jid, "cancel", preview_token=preview["preview_token"], confirm=True))
    finally:
        _principal.reset(context)
    assert result["summary"] == "Cancelled. 53 credits returned."


def test_details_summary_reports_youtube_and_offers_undo():
    text = video._details_summary(
        {"title": {"from": "A", "to": "B"}},
        {"updated": 1, "failed": []},
    )
    assert text == 'Changed title from "A" to "B". Also updated on YouTube. Say "undo" to switch back.'
    failed = video._details_summary(
        {"title": {"from": "A", "to": "B"}},
        {"updated": 0, "failed": [{"account_name": "MyChannel", "error": "Reconnect your YouTube channel to edit already-published videos."}]},
    )
    assert "Saved here, but on MyChannel: Reconnect" in failed


def test_review_fixes_hold_at_the_edges():
    # A malformed row in the jobs list is skipped, not a crash.
    assert video._recent_failures(["junk", None, {"status": "failed"}], NOW) == 0
    # Lists and tools read names the same way.
    assert _common._words("the marble one") == ["marble"]
    # A cancel whose follow-up read failed still reports what the action answered.
    assert video._control_summary("cancel", "cancelled", {}) == "Cancelled. Your credits were returned."
    # A page past the end is not "no videos".
    assert video._videos_summary([], 7, {}) == "No more videos past this point."
    assert workflows._schedule_line({"days_of_week": [], "times_of_day": []}, {}) == "No posting days set"
    # A failed post retry never claims anything about credits; a failed video retry does.
    api_error = _common.BackendAPIError(409, "Reconnect it first")
    assert "credits" not in str(_common._backend_error(api_error, write=True, spends=False))
    assert str(_common._backend_error(api_error, write=True, spends=True)).endswith(
        "No credits were used. Read the item (get_video, get_series or list_posts) and tell the user its status and last_error; don't retry blindly. [code: wrong_state]"
    )
