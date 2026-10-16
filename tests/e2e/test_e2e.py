"""End-to-end tests: the real MCP service in its own process, a stateful mock Platform API, and
the real MCP SDK client plus raw HTTP. No staging token, credits, or real posts are involved.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.extension import advertise
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Implementation

ROOT = Path(__file__).resolve().parents[2]
UUID0 = "00000000-0000-0000-0000-000000000001"
FUTURE = "2030-01-01T12:00:00Z"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "e2e", "version": "1"},
    },
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(TEST_KEY.public_key(), as_dict=True)
TEST_JWK.update({"kid": "e2e-key", "alg": "RS256", "use": "sig"})
BRIDGE_SECRET = "e2e-bridge-secret-with-at-least-32-chars"


def oauth_token(stack, label="good-token", base="mcp", *, scope="mcp:read mcp:write"):
    users = {"good-token": "user-a", "other-token": "user-b", "cache-token": "user-a", "outage-token": "user-a"}
    if label not in users:
        return label
    now = int(time.time())
    return jwt.encode(
        {"iss": stack["backend"], "sub": users[label], "aud": f"{stack[base]}/mcp/",
         "client_id": "e2e", "scope": scope, "token_use": "mcp_access",
         "iat": now, "exp": now + 600},
        TEST_KEY, algorithm="RS256", headers={"kid": "e2e-key"},
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(args: list[str], env: dict[str, str], port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", *args, "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"server on port {port} did not start")


def _mcp_env(backend_url: str, resource_url: str, **extra: str) -> dict[str, str]:
    return {"MCP_BACKEND_API_URL": backend_url, "MCP_API_TIMEOUT_SECONDS": "3",
            "MCP_OAUTH_ISSUER": backend_url, "MCP_RESOURCE_URL": resource_url,
            "MCP_BRIDGE_SECRET": BRIDGE_SECRET, **extra}


@pytest.fixture(scope="module")
def stack():
    backend_port, mcp_port, limited_port, replica_port = (_free_port() for _ in range(4))
    backend_url = f"http://127.0.0.1:{backend_port}"
    procs = [
        _start(["tests.e2e.mock_backend:app"], {"PYTHONPATH": str(ROOT),
            "MCP_BRIDGE_SECRET": BRIDGE_SECRET, "MCP_TEST_PUBLIC_JWK": json.dumps(TEST_JWK)}, backend_port),
        _start(["shortsmaker_mcp.main:app", "--app-dir", "src"],
            _mcp_env(backend_url, f"http://127.0.0.1:{mcp_port}/mcp/", MCP_RATE_LIMIT_REQUESTS="100000", MCP_DASHBOARD_ENABLED="true"),
            mcp_port,
        ),
        # Separate process, same canonical resource: models a fresh Vercel instance.
        _start(["app:app"],
            _mcp_env(backend_url, f"http://127.0.0.1:{mcp_port}/mcp/", MCP_RATE_LIMIT_REQUESTS="100000"),
            replica_port,
        ),
        _start(
            ["shortsmaker_mcp.main:app", "--app-dir", "src"],
            _mcp_env(
                backend_url, f"http://127.0.0.1:{limited_port}/mcp/", MCP_RATE_LIMIT_REQUESTS="3", MCP_DASHBOARD_ENABLED="false"
            ),
            limited_port,
        ),
    ]
    yield {
        "backend": backend_url,
        "mcp": f"http://127.0.0.1:{mcp_port}",
        "limited": f"http://127.0.0.1:{limited_port}",
        "replica": f"http://127.0.0.1:{replica_port}",
        "procs": procs,
    }
    for proc in procs:
        proc.terminate()
    for proc in procs:
        proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def fresh_backend(stack):
    httpx.delete(f"{stack['backend']}/__calls")


def backend_calls(stack) -> list[dict]:
    return [c for c in httpx.get(f"{stack['backend']}/__calls").json() if c["path"] != "/api/features"]


def run(stack, coro_fn, token="good-token", base="mcp", path="/mcp/", extensions=None):
    async def go():
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {oauth_token(stack, token, base)}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(f"{stack[base]}{path}", http_client=http),
                client_info=Implementation(name="e2e", version="1"),
                extensions=extensions,
            ) as client:
                return await coro_fn(client)

    return asyncio.run(go())


def call(stack, tool, args=None, **kw):
    return run(stack, lambda c: c.call_tool(tool, args or {}), **kw)


def make_video(stack, args, **kw):
    """Make a video the way an agent must: preview, then confirm with the preview's token."""
    args = {k: v for k, v in args.items() if k != "confirm"}
    preview = call(stack, "create_video", args, **kw)
    if preview.is_error:
        return preview
    token = preview.structured_content["preview_token"]
    return call(stack, "create_video", {**args, "preview_token": token, "confirm": True}, **kw)


def text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


# ---------------------------------------------------------------- discovery


def test_tool_and_prompt_catalogue(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    prompts = run(stack, lambda c: c.list_prompts()).prompts
    names = {t.name for t in tools}

    assert len(tools) == 30 and len(prompts) == 4
    # Clients already group tools under the server name, so names are bare snake_case verbs.
    assert all(re.fullmatch(r"[a-z]+(_[a-z]+)+", n) and len(n) <= 64 for n in names), names
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.output_schema is not None, tool.name
        if not tool.annotations.read_only_hint:
            assert "confirm" in tool.input_schema["properties"], tool.name


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_modern_protocol_discovers_and_calls_a_read_tool(stack, path):
    async def verify(client):
        assert client.protocol_version == "2026-07-28"
        assert client.session.discover_result is not None
        assert len((await client.list_tools()).tools) == 30
        result = await client.call_tool("get_credits", {})
        assert result.structured_content == {"balance": 100, "summary": "You have 100 credits."}

    run(stack, verify, path=path)


def test_initialize_and_tools_work_across_separate_instances(stack):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {oauth_token(stack)}"}
    initialized = httpx.post(f"{stack['mcp']}/mcp/", headers=headers, json=INIT)
    assert initialized.status_code == 200
    assert "mcp-session-id" not in initialized.headers
    headers["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
    # This instance has never received initialize and has no shared process memory.
    listed = httpx.post(f"{stack['replica']}/mcp/", headers=headers,
                       json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed.status_code == 200 and len(listed.json()["result"]["tools"]) == 30
    assert "mcp-session-id" not in listed.headers
    called = httpx.post(f"{stack['replica']}/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_credits", "arguments": {}}})
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"] == {"balance": 100, "summary": "You have 100 credits."}
    assert httpx.get(f"{stack['replica']}/dashboard").status_code == 404


def test_stateless_requests_use_each_tokens_identity_and_scopes(stack):
    for instance, label, expected_balance in (
        ("mcp", "good-token", 100), ("replica", "other-token", 5), ("mcp", "other-token", 5),
    ):
        response = httpx.post(f"{stack[instance]}/mcp/", headers={
            **MCP_HEADERS, "MCP-Protocol-Version": "2025-11-25",
            "Authorization": f"Bearer {oauth_token(stack, label)}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_credits", "arguments": {}}})
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] ["balance"] == expected_balance
    assert [entry["user"] for entry in backend_calls(stack)] == ["user-a", "user-b", "user-b"]
    # A previous write grant cannot survive into the next request's read-only grant.
    response = httpx.post(f"{stack['mcp']}/mcp/", headers={
        **MCP_HEADERS, "MCP-Protocol-Version": "2025-11-25",
        "Authorization": f"Bearer {oauth_token(stack, scope='mcp:read')}"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "pause_series", "arguments": {"series_id": UUID0, "confirm": True}}})
    assert response.status_code == 200 and response.json()["result"]["isError"] is True
    assert len(backend_calls(stack)) == 3


def test_prompts_render_and_reject_bad_arguments(stack):
    result = run(stack, lambda c: c.get_prompt("new_video", {"topic": "space"}))
    assert "confirm" in result.messages[0].content.text.lower() and "'space'" in result.messages[0].content.text
    # Slash commands are often run with no arguments; the prompt then asks for the topic.
    bare = run(stack, lambda c: c.get_prompt("new_series", {}))
    assert "what the series should be about" in bare.messages[0].content.text
    with pytest.RaisesGroup(MCPError, flatten_subgroups=True):
        run(stack, lambda c: c.get_prompt("share_video", {"job_id": "x" * 201}))


# ---------------------------------------------------------------- branding and gallery

GALLERY_URI = "ui://faceless-video-maker/gallery.html"
APPS_CLIENT = [advertise("io.modelcontextprotocol/ui", {"mimeTypes": ["text/html;profile=mcp-app"]})]


def test_server_identity_carries_brand_and_icons(stack):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {oauth_token(stack)}"}
    info = httpx.post(f"{stack['mcp']}/mcp/", headers=headers, json=INIT).json()["result"]["serverInfo"]
    assert info["title"] == "Faceless Video Maker"
    assert info["websiteUrl"] == "https://stage.facelessvideomaker.ai"  # MCP_APP_URL default
    icon_urls = [icon["src"] for icon in info["icons"]]
    assert all(url.startswith("http") for url in icon_urls)
    for url in icon_urls:
        # The icons are served by this service itself, so they work before the website ships.
        path = "/" + url.split("/", 3)[3]
        response = httpx.get(f"{stack['mcp']}{path}")
        assert response.status_code == 200 and response.headers["content-type"].startswith("image/")


def test_gallery_is_linked_from_list_tools_and_served(stack):
    tools = {t.name: t for t in run(stack, lambda c: c.list_tools()).tools}
    for name in ("list_videos", "get_video", "list_series"):
        assert tools[name].meta["ui"]["resourceUri"] == GALLERY_URI, name
        # The OAuth middleware still decorates the gallery tools.
        assert "securitySchemes" in tools[name].meta, name

    page = run(stack, lambda c: c.read_resource(GALLERY_URI)).contents[0]
    assert page.mime_type == "text/html;profile=mcp-app"
    assert "ui/initialize" in page.text and ".innerHTML" not in page.text  # titles are user content
    assert "__APP_URL__" not in page.text and "https://stage.facelessvideomaker.ai" in page.text


def test_gallery_flag_follows_client_support(stack):
    assert call(stack, "list_videos").structured_content["shown_as_gallery"] is False
    shown = run(stack, lambda c: c.call_tool("list_series", {}), extensions=APPS_CLIENT)
    assert shown.structured_content["shown_as_gallery"] is True


# ---------------------------------------------------------------- happy paths


def test_read_tools_return_backend_data(stack):
    assert call(stack, "get_credits").structured_content == {"balance": 100, "summary": "You have 100 credits."}
    assert call(stack, "list_videos").structured_content == {
        "summary": "No videos here yet.", "videos": [], "total": 0, "hidden": {}, "offset": 0, "next_offset": None, "shown_as_gallery": False
    }
    options = call(stack, "get_video_options", {"mode": "pro", "resolution": "1080p"})
    assert not options.is_error
    lengths = [c for c in backend_calls(stack) if c["path"] == "/api/lengths"]
    assert lengths[0]["query"] == {"format": "auto", "mode": "pro", "resolution": "1080p"}


def test_price_quote_includes_credits_matching_the_charge(stack):
    quote = call(stack, "get_video_price", {"total_length": 12}).structured_content
    assert "estimated_cost" not in quote and quote["estimated_credits"] == 10
    job = make_video(stack, {"niche": "x", "total_length": 12, "idempotency_key": "q", "confirm": True}).structured_content
    assert job["credits_cost"] == quote["estimated_credits"]


def test_billing_changes_are_not_exposed_but_credits_can_be_read(stack):
    names = {t.name for t in run(stack, lambda c: c.list_tools()).tools}
    prompts = {p.name for p in run(stack, lambda c: c.list_prompts()).prompts}

    assert {"get_credits", "get_subscription", "list_plans"} <= names
    assert not names & {
        "buy_credits",
        "start_subscription",
        "open_billing_portal",
        "cancel_subscription",
    }
    assert "billing_workflow" not in prompts
    result = call(stack, "buy_credits", {"amount_usd": 10, "confirm": True})
    assert result.is_error and backend_calls(stack) == []


def test_every_read_only_tool_runs_without_side_effects(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    skip_args = {
        "get_video_price": {"total_length": 15},
        "get_video": {"job_id": UUID0},
        "get_series": {"series_id": UUID0},
        "list_series_slots": {"series_id": UUID0},
    }
    for tool in (t for t in tools if t.annotations.read_only_hint):
        result = call(stack, tool.name, skip_args.get(tool.name, {}))
        # get_video on a missing job is a clean ToolError, everything else must succeed
        assert (not result.is_error) or tool.name == "get_video", (tool.name, text(result))
    assert {c["method"] for c in backend_calls(stack)} == {"GET"}


def test_create_video_end_to_end_and_summary_strips_large_fields(stack):
    preview = call(stack, "create_video", {"niche": "  deep sea  ", "total_length": 90}).structured_content
    assert preview["status"] == "needs_approval" and preview["estimated_credits"] == 10
    assert preview["adjustments"] == ["Length changed from 90s to 60s, the closest this format allows."]
    assert preview["summary"].startswith("This video costs 10 credits. You have 100, enough for about 10")
    assert all(c["method"] == "GET" for c in backend_calls(stack))  # a preview never writes

    assert preview["settings"]["total_length"] == 60
    created = call(stack, "create_video", {
        "niche": "  deep sea  ", "total_length": 90, "preview_token": preview["preview_token"], "confirm": True,
    }).structured_content
    assert created["status_label"] == "In line" and "replayed" not in created
    # The job is made with the previewed (snapped) length, not re-resolved from the ask.
    post = next(c for c in backend_calls(stack) if c["method"] == "POST" and c["path"] == "/api/jobs")
    assert post["body"]["total_length"] == 60
    assert "config" not in created and "owner" not in created
    assert call(stack, "get_credits").structured_content == {"balance": 90, "summary": "You have 90 credits."}
    fetched = call(stack, "get_video", {"job_id": "deep sea"}).structured_content  # found by name
    assert fetched["id"] == created["id"] and fetched["title"] == "deep sea"


def test_retried_create_without_a_key_is_a_replay_not_a_second_charge(stack):
    args = {"niche": "marble race", "total_length": 15}
    first = make_video(stack, args).structured_content
    again = make_video(stack, args).structured_content
    assert again["id"] == first["id"] and again["replayed"] is True
    assert "nothing new was charged" in again["summary"]
    assert call(stack, "get_credits").structured_content["balance"] == 90


def test_price_lock_refuses_a_stale_approval_before_any_write(stack):
    result = call(stack, "create_video", {"niche": "x", "total_length": 15, "preview_token": "p-stale", "confirm": True})
    assert result.is_error and "[code: preview_changed]" in text(result) and "Nothing was done" in text(result)
    # A preview for other settings doesn't approve these.
    other = call(stack, "create_video", {"niche": "y", "total_length": 15}).structured_content["preview_token"]
    result = call(stack, "create_video", {"niche": "x", "total_length": 15, "preview_token": other, "confirm": True})
    assert result.is_error and "[code: preview_changed]" in text(result)
    assert all(c["method"] == "GET" for c in backend_calls(stack))


def test_a_failed_video_is_tried_again_with_retry_video_not_create(stack):
    args = {"niche": "failed-marble", "total_length": 15}
    first = make_video(stack, args).structured_content
    again = make_video(stack, args).structured_content
    assert again["replayed"] is True and "retry_video" in again["next_step"]
    preview = call(stack, "retry_video", {"job_id": first["id"]}).structured_content
    assert preview["status"] == "needs_approval" and preview["credits"] == 10
    assert call(stack, "get_credits").structured_content["balance"] == 90  # a preview never charges
    done = call(stack, "retry_video", {"job_id": first["id"], "preview_token": preview["preview_token"], "confirm": True})
    assert not done.is_error and done.structured_content["status_label"] == "In line"
    assert call(stack, "get_credits").structured_content["balance"] == 80
    refused = call(stack, "retry_video", {"job_id": first["id"]})
    assert refused.is_error and "[code: wrong_state]" in text(refused)


def test_list_videos_pages_newest_first_with_next_offset(stack):
    for i in range(7):
        make_video(stack, {"niche": f"video-{i}", "total_length": 15, "idempotency_key": f"page-{i}", "confirm": True})

    def page(**args):
        data = call(stack, "list_videos", args).structured_content
        return [v["title"] for v in data["videos"]], data

    niches, data = page(limit=5)
    assert niches == [f"video-{i}" for i in (6, 5, 4, 3, 2)]
    assert data["total"] == 7 and data["next_offset"] == 5

    niches, data = page(limit=5, offset=data["next_offset"])
    assert niches == ["video-1", "video-0"] and data["next_offset"] is None

    niches, data = page(limit=3, offset=3)
    assert niches == ["video-3", "video-2", "video-1"] and data["next_offset"] == 6

    niches, data = page(offset=50)
    assert niches == [] and data["next_offset"] is None


def test_list_videos_hides_failed_videos_unless_asked(stack):
    for niche in ("failed-early", "kept", "failed-late"):
        make_video(stack, {"niche": niche, "total_length": 15, "idempotency_key": niche, "confirm": True})

    data = call(stack, "list_videos", {}).structured_content
    assert [v["title"] for v in data["videos"]] == ["kept"]
    assert data["hidden"] == {"failed": 2} and data["total"] == 1

    failed = call(stack, "list_videos", {"show": "failed"}).structured_content
    assert [v["title"] for v in failed["videos"]] == ["failed-late", "failed-early"]
    assert failed["hidden"] == {}


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 51}, {"offset": -1}])
def test_list_videos_rejects_bad_paging(stack, args):
    assert call(stack, "list_videos", args).is_error


def test_idempotency_key_replay_does_not_double_charge(stack):
    args = {"niche": "x", "total_length": 15, "idempotency_key": "same", "confirm": True}
    first = make_video(stack, args).structured_content
    second = make_video(stack, args).structured_content
    assert first["id"] == second["id"]
    assert call(stack, "get_credits").structured_content == {"balance": 90, "summary": "You have 90 credits."}


def test_users_are_isolated_and_concurrent_calls_keep_their_own_token(stack):
    async def go():
        async def one(token):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {oauth_token(stack, token)}"}, trust_env=False) as h:
                async with Client(streamable_http_client(f"{stack['mcp']}/mcp/", http_client=h)) as c:
                    return [(await c.call_tool("get_credits", {})).structured_content["balance"] for _ in range(5)]

        return await asyncio.gather(*[one("good-token" if i % 2 == 0 else "other-token") for i in range(10)])

    results = asyncio.run(go())
    assert all(r == [100] * 5 for r in results[0::2])
    assert all(r == [5] * 5 for r in results[1::2])

    mine = make_video(stack, {"niche": "mine", "total_length": 15, "idempotency_key": "iso", "confirm": True})
    job_id = mine.structured_content["id"]
    stolen = call(stack, "get_video", {"job_id": job_id}, token="other-token")
    assert stolen.is_error and "not found" in text(stolen).lower()


# ---------------------------------------------------------------- confirmation gate


WRITE_ARGS = {
    "create_series": {
        "name": "n", "theme": "theme", "connection_ids": [UUID0], "timezone": "UTC",
        "days_of_week": [0], "times_of_day": ["09:00"], "job_defaults": {"total_length": 15},
        "visibility": "public",
    },
    "update_series": {"series_id": UUID0, "name": "x"},
    "pause_series": {"series_id": UUID0},
    "resume_series": {"series_id": UUID0},
    "stop_series": {"series_id": UUID0},
    "delete_series": {"series_id": UUID0},
    "update_series_slot": {"slot_id": UUID0, "title": "t"},
    "control_series_slot": {"slot_id": UUID0, "action": "skip"},
    "create_video": {"niche": "x", "total_length": 15, "idempotency_key": "k"},
    "control_video": {"job_id": UUID0, "action": "cancel"},
    "retry_video": {"job_id": UUID0},
    "update_video_details": {"job_id": UUID0, "title": "t"},
    "connect_account": {"platform": "youtube"},
    "disconnect_account": {"connection_id": UUID0},
    "publish_video": {"job_id": UUID0, "connection_ids": [UUID0], "visibility": "public"},
    "control_post": {"post_id": UUID0, "action": "cancel"},
}


def test_every_write_tool_refuses_without_confirmation_and_never_reaches_backend(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    writes = {t.name for t in tools if not t.annotations.read_only_hint}
    assert writes == set(WRITE_ARGS), writes ^ set(WRITE_ARGS)

    for name in sorted(writes):
        for extra in ({}, {"confirm": False}):
            result = call(stack, name, {**WRITE_ARGS[name], **extra})
            assert result.is_error or result.structured_content["status"] in {"needs_approval", "unchanged"}, name
    assert [c for c in backend_calls(stack) if c["method"] != "GET"] == []


def test_confirmed_writes_forward_to_the_right_backend_routes(stack):
    expected = {
        "pause_series": ("POST", f"/api/workflows/{UUID0}/pause"),
        "delete_series": ("DELETE", f"/api/workflows/{UUID0}"),
        "control_series_slot": ("POST", f"/api/workflow-slots/{UUID0}/skip"),
        "control_video": ("POST", f"/api/jobs/{UUID0}/cancel"),
        "control_post": ("POST", f"/api/posts/{UUID0}/cancel"),
        "disconnect_account": ("DELETE", f"/api/connections/{UUID0}"),
        "connect_account": ("POST", "/api/connections/youtube/authorize"),
        "update_video_details": ("PATCH", f"/api/jobs/{UUID0}"),
        "publish_video": ("POST", "/api/posts"),
    }
    # Editing and cancelling read the video around the write, so they need one that exists.
    job_id = make_video(stack, {"niche": "real", "total_length": 15}).structured_content["id"]
    ready_id = make_video(stack, {"niche": "ready-to-publish", "total_length": 15}).structured_content["id"]
    real = {"control_video": {"job_id": job_id}, "update_video_details": {"job_id": job_id},
            "publish_video": {"job_id": ready_id}}
    for name in real:
        expected[name] = (expected[name][0], expected[name][1].replace(UUID0, job_id))
    for name, (method, path) in expected.items():
        start = len(backend_calls(stack))  # not cleared: clearing would also delete the video
        args = {**WRITE_ARGS[name], **real.get(name, {})}
        preview = call(stack, name, args)
        assert not preview.is_error, (name, text(preview))
        result = call(stack, name, {**args, "preview_token": preview.structured_content["preview_token"], "confirm": True})
        assert not result.is_error, (name, text(result))
        writes = [(c["method"], c["path"]) for c in backend_calls(stack)[start:] if c["method"] != "GET"]
        assert writes == [(method, path)], name  # pre-flight reads are fine; exactly one write


# ---------------------------------------------------------------- argument edge cases


@pytest.mark.parametrize(
    "tool,args,fragment",
    [
        ("get_video_price", {"total_length": 0}, ""),
        ("get_video_price", {"total_length": -1}, ""),
        ("get_video_price", {"total_length": 3601}, ""),
        ("get_video_price", {"total_length": "abc"}, ""),
        ("get_video_price", {"total_length": 15, "mode": "x" * 33}, ""),
        ("get_video", {"job_id": "x" * 201}, ""),
        ("create_video", {"niche": "   ", "total_length": 15, "idempotency_key": "k", "confirm": True}, "required"),
        ("create_video", {"niche": "x" * 10_001, "total_length": 15, "idempotency_key": "k", "confirm": True}, ""),
        ("create_video", {"niche": "x", "total_length": 15, "idempotency_key": " ", "confirm": True}, "required"),
        ("publish_video", {"job_id": UUID0, "connection_ids": [], "confirm": True}, ""),
        ("publish_video", {"job_id": UUID0, "connection_ids": [UUID0], "visibility": "public", "scheduled_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("create_series", {**WRITE_ARGS["create_series"], "ends_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("create_series", {**WRITE_ARGS["create_series"], "lead_minutes": 29, "confirm": True}, ""),
        ("create_series", {**WRITE_ARGS["create_series"], "grace_minutes": 10081, "confirm": True}, ""),
        ("update_series_slot", {"slot_id": UUID0, "slot_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("list_series_slots", {"series_id": UUID0, "from_time": "2030-01-01T12:00:00"}, "timezone"),
        ("resume_series", {"series_id": UUID0, "ends_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("create_series", {**WRITE_ARGS["create_series"], "times_of_day": ["25:00"], "confirm": True}, "HH:MM"),
        ("create_series", {**WRITE_ARGS["create_series"], "days_of_week": [7], "confirm": True}, ""),
        ("create_series", {**WRITE_ARGS["create_series"], "timezone": "PST", "confirm": True}, "IANA"),
        ("create_series", {**WRITE_ARGS["create_series"], "ends_at": "2020-01-01T00:00:00Z", "confirm": True}, "past"),
        ("update_series", {"series_id": UUID0, "confirm": True}, "at least one"),
        ("update_video_details", {"job_id": UUID0, "confirm": True}, "at least one"),
        ("control_series_slot", {"slot_id": UUID0, "action": "skip", "suggested_changes": "x", "confirm": True}, "regenerate_idea"),
        ("control_video", {"job_id": UUID0, "action": "delete", "confirm": True}, ""),
        ("list_posts", {"status": "bogus"}, ""),
        ("does_not_exist", {}, ""),
    ],
)
def test_invalid_arguments_are_rejected_before_the_backend(stack, tool, args, fragment):
    result = call(stack, tool, args)
    assert result.is_error, tool
    assert fragment.lower() in text(result).lower()
    assert backend_calls(stack) == []


def test_unicode_and_boundary_values_are_accepted(stack):
    args = {"niche": "日本語 🌊 " + "x" * 9980, "total_length": 3600, "idempotency_key": "é" * 200, "confirm": True}
    result = make_video(stack, args)
    assert not result.is_error, text(result)
    sent = next(c for c in backend_calls(stack) if c["method"] == "POST")["body"]
    # The longest ask is accepted; the job carries the length the preview resolved it to.
    assert sent["total_length"] == 60 and sent["niche"].startswith("日本語 🌊")


def test_read_filters_still_accept_naive_updated_since(stack):
    # Only writes and slot windows enforce timezones; these read filters never did.
    for tool in ("list_posts", "list_videos"):
        assert not call(stack, tool, {"updated_since": "2030-01-01T12:00:00"}).is_error, tool


def test_duplicate_connection_ids_are_deduplicated(stack):
    job_id = make_video(stack, {"niche": "ready-dedupe", "total_length": 15}).structured_content["id"]
    args = {"job_id": job_id, "connection_ids": [UUID0, UUID0], "visibility": "public"}
    preview = call(stack, "publish_video", args)
    call(stack, "publish_video", {**args, "preview_token": preview.structured_content["preview_token"], "confirm": True})
    post = next(c for c in backend_calls(stack) if c["method"] == "POST" and c["path"] == "/api/posts")
    assert post["body"]["connection_ids"] == [UUID0]


# ---------------------------------------------------------------- backend failure edge cases


def test_insufficient_credits_message_is_passed_through(stack):
    result = make_video(stack, {"niche": "x", "total_length": 15, "idempotency_key": "p", "confirm": True}, token="other-token")
    # other-token has 5 credits, job costs 10
    assert result.is_error and "need 10, have 5." in text(result) and "get_credits" in text(result)


def test_backend_500_detail_is_sanitised(stack):
    result = make_video(stack, {"niche": "boom500", "total_length": 15, "idempotency_key": "b", "confirm": True})
    assert result.is_error
    assert "psycopg2" not in text(result) and "10.0.0.5" not in text(result)


def test_backend_non_json_response_is_a_clean_error(stack):
    result = make_video(stack, {"niche": "badjson", "total_length": 15, "idempotency_key": "j", "confirm": True})
    assert result.is_error and "<html>" not in text(result)


# ---------------------------------------------------------------- HTTP boundary edge cases


def _post(stack, headers=None, base="mcp", path="/mcp/", **kw):
    headers = {**MCP_HEADERS, **(headers or {})}
    authorization = headers.get("Authorization", "")
    scheme, separator, label = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and label in {"good-token", "other-token", "cache-token", "outage-token"}:
        headers["Authorization"] = f"{scheme} {oauth_token(stack, label, base)}"
    return httpx.post(f"{stack[base]}{path}", headers=headers, json=kw.pop("json", INIT), **kw)


@pytest.mark.parametrize(
    "header,status",
    [
        (None, 401),
        ("Bearer", 401),
        ("Basic Zm9vOmJhcg==", 401),
        ("good-token", 401),
        ("Bearer wrong-token", 401),
        ("Bearer inactive-token", 401),
        ("Bearer throttled-token", 401),
    ],
)
def test_authorization_header_edge_cases(stack, header, status):
    response = _post(stack, {"Authorization": header} if header else None)
    assert response.status_code == status
    if status == 401:
        assert "www-authenticate" in response.headers


def test_lowercase_bearer_scheme_is_accepted(stack):
    assert _post(stack, {"Authorization": "bearer good-token"}).status_code == 200


def test_valid_token_is_accepted_across_requests(stack):
    for _ in range(5):
        assert _post(stack, {"Authorization": "Bearer cache-token"}).status_code == 200


def test_malformed_and_oversized_bodies_do_not_crash_the_service(stack):
    auth = {"Authorization": "Bearer good-token"}
    assert _post(stack, auth, content=b"{not json", json=None).status_code in {400, 422}
    assert _post(stack, auth, json={"jsonrpc": "2.0", "id": 2, "method": "nope"}).status_code in {200, 400, 404, 422}
    big = _post(stack, auth, json={**INIT, "params": {**INIT["params"], "pad": "x" * 5_000_000}})
    assert big.status_code < 500
    assert httpx.get(f"{stack['mcp']}/health/live").status_code == 200


def test_get_without_session_and_wrong_methods(stack):
    missing = httpx.get(f"{stack['mcp']}/mcp/")
    assert missing.status_code == 401 and "www-authenticate" in missing.headers
    signed = {"Authorization": f"Bearer {oauth_token(stack)}"}
    for method in ("GET", "DELETE", "PUT"):
        response = httpx.request(method, f"{stack['mcp']}/mcp/", headers=signed)
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"


def test_mcp_endpoint_without_trailing_slash_does_not_redirect(stack):
    missing = _post(stack, path="/mcp")
    assert missing.status_code == 401
    assert "www-authenticate" in missing.headers

    authorized = _post(stack, {"Authorization": "Bearer good-token"}, path="/mcp")
    assert authorized.status_code == 200
    assert authorized.json()["result"]["serverInfo"]["name"] == "faceless-video-maker"


def test_rebinding_protection_rejects_foreign_host_and_origin(stack):
    auth = {"Authorization": "Bearer good-token"}
    assert _post(stack, {**auth, "Host": "evil.example.com"}).status_code in {400, 403, 421}
    assert _post(stack, {**auth, "Origin": "https://evil.example.com"}).status_code in {400, 403}
    assert _post(stack, auth).status_code == 200


def test_backend_outage_returns_503_not_a_crash(stack):
    backend_proc = stack["procs"][0]
    port = int(stack["backend"].rsplit(":", 1)[1])
    # The verified access token remains usable, but tool execution fails safely.
    backend_proc.terminate()
    backend_proc.wait(timeout=10)
    try:
        result = call(stack, "get_credits")
        assert result.is_error
        assert httpx.get(f"{stack['mcp']}/health/live").status_code == 200
    finally:
        stack["procs"][0] = _start(["tests.e2e.mock_backend:app"], {"PYTHONPATH": str(ROOT),
            "MCP_BRIDGE_SECRET": BRIDGE_SECRET, "MCP_TEST_PUBLIC_JWK": json.dumps(TEST_JWK)}, port)


def test_rate_limit_trips_per_user_and_health_stays_open(stack):
    codes = [_post(stack, {"Authorization": "Bearer good-token"}, base="limited").status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}
    # Buckets are per user: hosted clients share IPs, so one user must not lock out another.
    assert _post(stack, {"Authorization": "Bearer other-token"}, base="limited").status_code == 200
    assert httpx.get(f"{stack['limited']}/health/live").status_code == 200


def test_dashboard_flag_and_health(stack):
    assert httpx.get(f"{stack['mcp']}/dashboard").status_code == 200
    assert httpx.get(f"{stack['limited']}/dashboard").status_code == 404
    health = httpx.get(f"{stack['mcp']}/health/live").json()
    assert health["status"] == "ok" and health["version"] == "0.3.0"


@pytest.mark.parametrize('version', ['2025-03-26', '2025-06-18', '2025-11-25'])
def test_client_protocols_publish_oauth_tool_metadata(stack, version):
    headers = {**MCP_HEADERS, 'Authorization': 'Bearer '+oauth_token(stack)}
    initialized = httpx.post(stack['mcp']+'/mcp/', headers=headers, json={
        **INIT, 'params': {**INIT['params'], 'protocolVersion': version},
    })
    assert initialized.status_code == 200
    headers['MCP-Protocol-Version'] = initialized.json()['result']['protocolVersion']
    response = httpx.post(stack['mcp']+'/mcp/', headers=headers, json={
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
    })
    assert response.status_code == 200
    tools = response.json()['result']['tools']
    assert len(tools) == 30
    for tool in tools:
        scope = 'mcp:read' if tool['annotations']['readOnlyHint'] else 'mcp:write'
        expected = [{'type': 'oauth2', 'scopes': [scope]}]
        assert tool['securitySchemes'] == expected
        assert tool['_meta']['securitySchemes'] == expected
    assert 'bridge' not in response.text.lower()


def test_scope_upgrade_has_tool_level_oauth_challenge_without_backend_call(stack):
    response = httpx.post(stack['mcp']+'/mcp/', headers={
        **MCP_HEADERS, 'Authorization': 'Bearer '+oauth_token(stack, scope='mcp:read'),
        'MCP-Protocol-Version': '2025-11-25',
    }, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'pause_series', 'arguments': {'series_id': UUID0, 'confirm': True}},
    })
    assert response.status_code == 200
    result = response.json()['result']
    assert result['isError'] is True
    challenge = result['_meta']['mcp/www_authenticate'][0]
    assert 'error="insufficient_scope"' in challenge
    assert 'error_description=' in challenge and 'scope="mcp:write"' in challenge
    assert backend_calls(stack) == []


def test_scope_denial_is_a_valid_result_for_the_official_mcp_client(stack):
    async def check():
        token = oauth_token(stack, scope="mcp:read")
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(f"{stack['mcp']}/mcp/", http_client=http),
                client_info=Implementation(name="scope-e2e", version="1"),
            ) as client:
                return await client.call_tool(
                    "pause_series",
                    {"series_id": UUID0, "confirm": True},
                )

    result = asyncio.run(check())

    assert result.is_error
    assert "mcp:write" in result.meta["mcp/www_authenticate"][0]
    assert backend_calls(stack) == []
