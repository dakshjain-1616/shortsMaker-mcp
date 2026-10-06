"""End-to-end tests: the real MCP service in its own process, a stateful mock Platform API, and
the real MCP SDK client plus raw HTTP. No staging token, credits, or real posts are involved.
"""

from __future__ import annotations

import asyncio
import json
import os
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


def run(stack, coro_fn, token="good-token", base="mcp"):
    async def go():
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {oauth_token(stack, token, base)}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(f"{stack[base]}/mcp/", http_client=http),
                client_info=Implementation(name="e2e", version="1"),
            ) as client:
                return await coro_fn(client)

    return asyncio.run(go())


def call(stack, tool, args=None, **kw):
    return run(stack, lambda c: c.call_tool(tool, args or {}), **kw)


def text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


# ---------------------------------------------------------------- discovery


def test_tool_and_prompt_catalogue(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    prompts = run(stack, lambda c: c.list_prompts()).prompts
    names = {t.name for t in tools}

    assert len(tools) == 29 and len(prompts) == 3
    assert all(n.startswith("shortsmaker_") for n in names)
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.output_schema is not None, tool.name
        if not tool.annotations.read_only_hint:
            assert "confirm" in tool.input_schema["properties"], tool.name


def test_initialize_and_tools_work_across_separate_instances(stack):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {oauth_token(stack)}"}
    initialized = httpx.post(f"{stack['mcp']}/mcp/", headers=headers, json=INIT)
    assert initialized.status_code == 200
    assert "mcp-session-id" not in initialized.headers
    headers["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
    # This instance has never received initialize and has no shared process memory.
    listed = httpx.post(f"{stack['replica']}/mcp/", headers=headers,
                       json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed.status_code == 200 and len(listed.json()["result"]["tools"]) == 29
    assert "mcp-session-id" not in listed.headers
    called = httpx.post(f"{stack['replica']}/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "shortsmaker_get_credit_status", "arguments": {}}})
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"] == {"balance": 100}
    assert httpx.get(f"{stack['replica']}/dashboard").status_code == 404


def test_stateless_requests_use_each_tokens_identity_and_scopes(stack):
    for instance, label, expected_balance in (
        ("mcp", "good-token", 100), ("replica", "other-token", 5), ("mcp", "other-token", 5),
    ):
        response = httpx.post(f"{stack[instance]}/mcp/", headers={
            **MCP_HEADERS, "MCP-Protocol-Version": "2025-11-25",
            "Authorization": f"Bearer {oauth_token(stack, label)}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "shortsmaker_get_credit_status", "arguments": {}}})
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == {"balance": expected_balance}
    assert [entry["user"] for entry in backend_calls(stack)] == ["user-a", "user-b", "user-b"]
    # A previous write grant cannot survive into the next request's read-only grant.
    response = httpx.post(f"{stack['mcp']}/mcp/", headers={
        **MCP_HEADERS, "MCP-Protocol-Version": "2025-11-25",
        "Authorization": f"Bearer {oauth_token(stack, scope='mcp:read')}"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "shortsmaker_pause_workflow", "arguments": {"workflow_id": UUID0, "confirm": True}}})
    assert response.status_code == 200 and response.json()["result"]["isError"] is True
    assert len(backend_calls(stack)) == 3


def test_prompts_render_and_reject_bad_arguments(stack):
    result = run(stack, lambda c: c.get_prompt("shortsmaker_create_video_workflow", {"niche": "space", "total_length": "15"}))
    assert "confirm" in result.messages[0].content.text.lower()
    with pytest.RaisesGroup(MCPError, flatten_subgroups=True):
        run(stack, lambda c: c.get_prompt("shortsmaker_publish_video_workflow", {"job_id": "not-a-uuid"}))


# ---------------------------------------------------------------- happy paths


def test_read_tools_return_backend_data(stack):
    assert call(stack, "shortsmaker_get_credit_status").structured_content == {"balance": 100}
    assert call(stack, "shortsmaker_list_videos").structured_content == {
        "videos": [], "total": 0, "offset": 0, "next_offset": None
    }
    options = call(stack, "shortsmaker_get_video_options", {"mode": "pro", "resolution": "1080p"})
    assert not options.is_error
    lengths = [c for c in backend_calls(stack) if c["path"] == "/api/lengths"]
    assert lengths[0]["query"] == {"mode": "pro", "resolution": "1080p"}


def test_price_quote_includes_credits_matching_the_charge(stack):
    quote = call(stack, "shortsmaker_resolve_video_price", {"total_length": 12}).structured_content
    assert quote["estimated_cost"] == 0.10 and quote["estimated_credits"] == 10
    job = call(stack, "shortsmaker_create_video", {"niche": "x", "total_length": 12, "idempotency_key": "q", "confirm": True}).structured_content
    assert job["credits_cost"] == quote["estimated_credits"]


def test_billing_changes_are_not_exposed_but_credits_can_be_read(stack):
    names = {t.name for t in run(stack, lambda c: c.list_tools()).tools}
    prompts = {p.name for p in run(stack, lambda c: c.list_prompts()).prompts}

    assert {"shortsmaker_get_credit_status", "shortsmaker_get_subscription", "shortsmaker_list_subscription_plans"} <= names
    assert not names & {
        "shortsmaker_buy_credits",
        "shortsmaker_start_subscription",
        "shortsmaker_open_billing_portal",
        "shortsmaker_cancel_subscription",
    }
    assert "shortsmaker_billing_workflow" not in prompts
    result = call(stack, "shortsmaker_buy_credits", {"amount_usd": 10, "confirm": True})
    assert result.is_error and backend_calls(stack) == []


def test_every_read_only_tool_runs_without_side_effects(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    skip_args = {
        "shortsmaker_resolve_video_price": {"total_length": 15},
        "shortsmaker_get_video": {"job_id": UUID0},
        "shortsmaker_get_workflow": {"workflow_id": UUID0},
        "shortsmaker_list_workflow_slots": {"workflow_id": UUID0},
    }
    for tool in (t for t in tools if t.annotations.read_only_hint):
        result = call(stack, tool.name, skip_args.get(tool.name, {}))
        # get_video on a missing job is a clean ToolError, everything else must succeed
        assert (not result.is_error) or tool.name == "shortsmaker_get_video", (tool.name, text(result))
    assert {c["method"] for c in backend_calls(stack)} == {"GET"}


def test_create_video_end_to_end_and_summary_strips_large_fields(stack):
    created = call(
        stack,
        "shortsmaker_create_video",
        {"niche": "  deep sea  ", "total_length": 15, "idempotency_key": "k1", "confirm": True},
    ).structured_content
    assert created["status"] == "queued" and created["niche"] == "deep sea"
    assert "config" not in created and "owner" not in created
    assert call(stack, "shortsmaker_get_credit_status").structured_content == {"balance": 90}
    fetched = call(stack, "shortsmaker_get_video", {"job_id": created["id"]}).structured_content
    assert fetched["id"] == created["id"]


def test_list_videos_pages_newest_first_with_next_offset(stack):
    for i in range(7):
        call(stack, "shortsmaker_create_video", {"niche": f"video-{i}", "total_length": 15, "idempotency_key": f"page-{i}", "confirm": True})

    def page(**args):
        data = call(stack, "shortsmaker_list_videos", args).structured_content
        return [v["niche"] for v in data["videos"]], data

    niches, data = page()  # default page size is 5
    assert niches == [f"video-{i}" for i in (6, 5, 4, 3, 2)]
    assert data["total"] == 7 and data["next_offset"] == 5

    niches, data = page(offset=data["next_offset"])
    assert niches == ["video-1", "video-0"] and data["next_offset"] is None

    niches, data = page(limit=3, offset=3)
    assert niches == ["video-3", "video-2", "video-1"] and data["next_offset"] == 6

    niches, data = page(offset=50)
    assert niches == [] and data["next_offset"] is None


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 51}, {"offset": -1}])
def test_list_videos_rejects_bad_paging(stack, args):
    assert call(stack, "shortsmaker_list_videos", args).is_error


def test_idempotency_key_replay_does_not_double_charge(stack):
    args = {"niche": "x", "total_length": 15, "idempotency_key": "same", "confirm": True}
    first = call(stack, "shortsmaker_create_video", args).structured_content
    second = call(stack, "shortsmaker_create_video", args).structured_content
    assert first["id"] == second["id"]
    assert call(stack, "shortsmaker_get_credit_status").structured_content == {"balance": 90}


def test_users_are_isolated_and_concurrent_calls_keep_their_own_token(stack):
    async def go():
        async def one(token):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {oauth_token(stack, token)}"}, trust_env=False) as h:
                async with Client(streamable_http_client(f"{stack['mcp']}/mcp/", http_client=h)) as c:
                    return [(await c.call_tool("shortsmaker_get_credit_status", {})).structured_content["balance"] for _ in range(5)]

        return await asyncio.gather(*[one("good-token" if i % 2 == 0 else "other-token") for i in range(10)])

    results = asyncio.run(go())
    assert all(r == [100] * 5 for r in results[0::2])
    assert all(r == [5] * 5 for r in results[1::2])

    mine = call(stack, "shortsmaker_create_video", {"niche": "mine", "total_length": 15, "idempotency_key": "iso", "confirm": True})
    job_id = mine.structured_content["id"]
    stolen = call(stack, "shortsmaker_get_video", {"job_id": job_id}, token="other-token")
    assert stolen.is_error and "not found" in text(stolen).lower()


# ---------------------------------------------------------------- confirmation gate


WRITE_ARGS = {
    "shortsmaker_create_workflow": {
        "name": "n", "theme": "theme", "connection_ids": [UUID0], "timezone": "UTC",
        "days_of_week": [0], "times_of_day": ["09:00"],
    },
    "shortsmaker_update_workflow": {"workflow_id": UUID0, "name": "x"},
    "shortsmaker_pause_workflow": {"workflow_id": UUID0},
    "shortsmaker_resume_workflow": {"workflow_id": UUID0},
    "shortsmaker_stop_workflow": {"workflow_id": UUID0},
    "shortsmaker_delete_workflow": {"workflow_id": UUID0},
    "shortsmaker_update_workflow_slot": {"slot_id": UUID0, "title": "t"},
    "shortsmaker_control_workflow_slot": {"slot_id": UUID0, "action": "skip"},
    "shortsmaker_create_video": {"niche": "x", "total_length": 15, "idempotency_key": "k"},
    "shortsmaker_control_video": {"job_id": UUID0, "action": "cancel"},
    "shortsmaker_update_video_metadata": {"job_id": UUID0, "title": "t"},
    "shortsmaker_start_connection": {"platform": "youtube"},
    "shortsmaker_disconnect": {"connection_id": UUID0},
    "shortsmaker_publish_video": {"job_id": UUID0, "connection_ids": [UUID0]},
    "shortsmaker_control_post": {"post_id": UUID0, "action": "cancel"},
}


def test_every_write_tool_refuses_without_confirmation_and_never_reaches_backend(stack):
    tools = run(stack, lambda c: c.list_tools()).tools
    writes = {t.name for t in tools if not t.annotations.read_only_hint}
    assert writes == set(WRITE_ARGS), writes ^ set(WRITE_ARGS)

    for name in sorted(writes):
        for extra in ({}, {"confirm": False}):
            result = call(stack, name, {**WRITE_ARGS[name], **extra})
            assert result.is_error, name
            assert "confirm" in text(result).lower(), (name, text(result))
    assert backend_calls(stack) == []


def test_confirmed_writes_forward_to_the_right_backend_routes(stack):
    expected = {
        "shortsmaker_pause_workflow": ("POST", f"/api/workflows/{UUID0}/pause"),
        "shortsmaker_delete_workflow": ("DELETE", f"/api/workflows/{UUID0}"),
        "shortsmaker_control_workflow_slot": ("POST", f"/api/workflow-slots/{UUID0}/skip"),
        "shortsmaker_control_video": ("POST", f"/api/jobs/{UUID0}/cancel"),
        "shortsmaker_control_post": ("POST", f"/api/posts/{UUID0}/cancel"),
        "shortsmaker_disconnect": ("DELETE", f"/api/connections/{UUID0}"),
        "shortsmaker_start_connection": ("POST", "/api/connections/youtube/authorize"),
        "shortsmaker_update_video_metadata": ("PATCH", f"/api/jobs/{UUID0}"),
        "shortsmaker_publish_video": ("POST", "/api/posts"),
    }
    for name, (method, path) in expected.items():
        httpx.delete(f"{stack['backend']}/__calls")
        result = call(stack, name, {**WRITE_ARGS[name], "confirm": True})
        assert not result.is_error, (name, text(result))
        sent = backend_calls(stack)
        assert [(c["method"], c["path"]) for c in sent] == [(method, path)], name


# ---------------------------------------------------------------- argument edge cases


@pytest.mark.parametrize(
    "tool,args,fragment",
    [
        ("shortsmaker_resolve_video_price", {"total_length": 0}, ""),
        ("shortsmaker_resolve_video_price", {"total_length": -1}, ""),
        ("shortsmaker_resolve_video_price", {"total_length": 3601}, ""),
        ("shortsmaker_resolve_video_price", {"total_length": "abc"}, ""),
        ("shortsmaker_resolve_video_price", {"total_length": 15, "mode": "x" * 33}, ""),
        ("shortsmaker_get_video", {"job_id": "not-a-uuid"}, ""),
        ("shortsmaker_create_video", {"niche": "   ", "total_length": 15, "idempotency_key": "k", "confirm": True}, "required"),
        ("shortsmaker_create_video", {"niche": "x" * 10_001, "total_length": 15, "idempotency_key": "k", "confirm": True}, ""),
        ("shortsmaker_create_video", {"niche": "x", "total_length": 15, "idempotency_key": " ", "confirm": True}, "required"),
        ("shortsmaker_create_video", {"niche": "x", "total_length": 15, "confirm": True}, ""),
        ("shortsmaker_publish_video", {"job_id": UUID0, "connection_ids": [], "confirm": True}, ""),
        ("shortsmaker_publish_video", {"job_id": UUID0, "connection_ids": [UUID0], "scheduled_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "ends_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "lead_minutes": 29, "confirm": True}, ""),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "grace_minutes": 10081, "confirm": True}, ""),
        ("shortsmaker_update_workflow_slot", {"slot_id": UUID0, "slot_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("shortsmaker_list_workflow_slots", {"workflow_id": UUID0, "from_time": "2030-01-01T12:00:00"}, "timezone"),
        ("shortsmaker_resume_workflow", {"workflow_id": UUID0, "ends_at": "2030-01-01T12:00:00", "confirm": True}, "timezone"),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "times_of_day": ["25:00"], "confirm": True}, "HH:MM"),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "days_of_week": [7], "confirm": True}, ""),
        ("shortsmaker_create_workflow", {**WRITE_ARGS["shortsmaker_create_workflow"], "times_of_day": ["09:00", "09:00"], "confirm": True}, "HH:MM"),
        ("shortsmaker_update_workflow", {"workflow_id": UUID0, "confirm": True}, "at least one"),
        ("shortsmaker_update_video_metadata", {"job_id": UUID0, "confirm": True}, "at least one"),
        ("shortsmaker_control_workflow_slot", {"slot_id": UUID0, "action": "skip", "suggested_changes": "x", "confirm": True}, "regenerate_idea"),
        ("shortsmaker_control_video", {"job_id": UUID0, "action": "delete", "confirm": True}, ""),
        ("shortsmaker_list_posts", {"status": "bogus"}, ""),
        ("shortsmaker_does_not_exist", {}, ""),
    ],
)
def test_invalid_arguments_are_rejected_before_the_backend(stack, tool, args, fragment):
    result = call(stack, tool, args)
    assert result.is_error, tool
    assert fragment.lower() in text(result).lower()
    assert backend_calls(stack) == []


def test_unicode_and_boundary_values_are_accepted(stack):
    args = {"niche": "日本語 🌊 " + "x" * 9980, "total_length": 3600, "idempotency_key": "é" * 200, "confirm": True}
    result = call(stack, "shortsmaker_create_video", args)
    assert not result.is_error, text(result)
    sent = backend_calls(stack)[0]["body"]
    assert sent["total_length"] == 3600 and sent["niche"].startswith("日本語 🌊")


def test_read_filters_still_accept_naive_updated_since(stack):
    # Only writes and slot windows enforce timezones; these read filters never did.
    for tool in ("shortsmaker_list_posts", "shortsmaker_list_videos"):
        assert not call(stack, tool, {"updated_since": "2030-01-01T12:00:00"}).is_error, tool


def test_duplicate_connection_ids_are_deduplicated(stack):
    call(stack, "shortsmaker_publish_video", {"job_id": UUID0, "connection_ids": [UUID0, UUID0], "confirm": True})
    assert backend_calls(stack)[0]["body"]["connection_ids"] == [UUID0]


# ---------------------------------------------------------------- backend failure edge cases


def test_insufficient_credits_message_is_passed_through(stack):
    result = call(stack, "shortsmaker_create_video", {"niche": "x", "total_length": 15, "idempotency_key": "p", "confirm": True}, token="other-token")
    # other-token has 5 credits, job costs 10
    assert result.is_error and "Insufficient credits" in text(result)


def test_backend_500_detail_is_sanitised(stack):
    result = call(stack, "shortsmaker_create_video", {"niche": "boom500", "total_length": 15, "idempotency_key": "b", "confirm": True})
    assert result.is_error
    assert "psycopg2" not in text(result) and "10.0.0.5" not in text(result)


def test_backend_non_json_response_is_a_clean_error(stack):
    result = call(stack, "shortsmaker_create_video", {"niche": "badjson", "total_length": 15, "idempotency_key": "j", "confirm": True})
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
        result = call(stack, "shortsmaker_get_credit_status")
        assert result.is_error
        assert httpx.get(f"{stack['mcp']}/health/live").status_code == 200
    finally:
        stack["procs"][0] = _start(["tests.e2e.mock_backend:app"], {"PYTHONPATH": str(ROOT),
            "MCP_BRIDGE_SECRET": BRIDGE_SECRET, "MCP_TEST_PUBLIC_JWK": json.dumps(TEST_JWK)}, port)


def test_rate_limit_trips_per_client_and_health_stays_open(stack):
    codes = [_post(stack, {"Authorization": "Bearer good-token"}, base="limited").status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}
    assert httpx.get(f"{stack['limited']}/health/live").status_code == 200


def test_dashboard_flag_and_health(stack):
    assert httpx.get(f"{stack['mcp']}/dashboard").status_code == 200
    assert httpx.get(f"{stack['limited']}/dashboard").status_code == 404
    health = httpx.get(f"{stack['mcp']}/health/live").json()
    assert health["status"] == "ok" and health["version"] == "0.2.0"
