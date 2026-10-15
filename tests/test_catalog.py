"""The tool catalogue is the agent's whole manual: pin its shape and lint its descriptions.

Regenerate the snapshot after a deliberate change with UPDATE_SNAPSHOT=1 pytest tests/test_catalog.py.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp.oauth_auth import _principal
from tests.test_tools import TEST_PRINCIPAL, _registered_tools

SNAPSHOT = Path(__file__).with_name("tool_catalog.json")
ID = UUID(int=1)


def _server():
    server, backend = _registered_tools()
    return server._tool_manager._tools, backend


def _run(coro):
    context = _principal.set(TEST_PRINCIPAL)
    try:
        return asyncio.run(coro)
    finally:
        _principal.reset(context)


def test_catalog_matches_snapshot():
    registered, _ = _server()
    catalog = {
        name: {
            "annotations": tool.annotations.model_dump(exclude_none=True),
            "required": sorted(tool.parameters.get("required", [])),
            "params": sorted(tool.parameters["properties"]),
        }
        for name, tool in sorted(registered.items())
    }
    if os.environ.get("UPDATE_SNAPSHOT"):
        # One line per tool, so a change shows up as a one-line diff.
        lines = (f"{json.dumps(name)}: {json.dumps(entry, sort_keys=True)}" for name, entry in catalog.items())
        SNAPSHOT.write_text("{\n" + ",\n".join(lines) + "\n}\n")
    assert catalog == json.loads(SNAPSHOT.read_text())


@pytest.mark.parametrize("name", sorted(_server()[0]))
def test_description_says_when_how_and_what_comes_back(name):
    tool = _server()[0][name]
    text = tool.description
    assert text.startswith("Use when"), name
    assert "Don't" in text and "Returns" in text, name
    assert len(text) <= 4096, name
    if not tool.annotations.read_only_hint:
        assert "confirm" in tool.parameters["properties"], name


def test_list_connections_points_to_connect_when_empty_or_broken():
    registered, backend = _server()
    backend.request.return_value = []
    empty = _run(registered["list_connections"].fn())
    assert empty["count"] == 0 and "connect_account" in empty["next_step"]

    backend.request.return_value = [
        {"id": "a", "platform": "youtube", "status": "active"},
        {"id": "b", "platform": "tiktok", "status": "error", "last_error": "Token revoked"},
    ]
    broken = _run(registered["list_connections"].fn())
    assert broken["count"] == 2 and "tiktok" in broken["next_step"] and "youtube" not in broken["next_step"]

    backend.request.return_value = [{"id": "a", "platform": "youtube", "status": "active"}]
    assert "next_step" not in _run(registered["list_connections"].fn())


def test_list_posts_flags_failures():
    registered, backend = _server()
    backend.request.return_value = [{"id": "p", "status": "failed", "last_error": "Quota"}]
    assert "last_error" in _run(registered["list_posts"].fn())["next_step"]


def test_publish_title_over_100_is_refused_before_any_call():
    registered, backend = _server()
    with pytest.raises(ToolError, match="100 characters"):
        _run(registered["publish_video"].fn(ID, [ID], "public", title=" " + "x" * 101 + " ", confirm=True))
    backend.request.assert_not_awaited()


def test_create_video_refuses_unknown_voice_before_charging():
    from tests.test_tools import PRICED, _routes

    registered, backend = _server()
    config = {"styles": ["cinematic"], "audio": {"voices": [{"id": "kore"}], "music_tracks": [{"id": "calm-piano"}]}}
    backend.request.side_effect = _routes({**PRICED, "/api/config": config})
    create = registered["create_video"].fn
    with pytest.raises(ToolError, match="'atlas' is not offered.*kore.*Nothing was charged"):
        _run(create("space", 30, narrator_voice="atlas", expected_credits=18, confirm=True))
    assert all(c.args[0] == "GET" for c in backend.request.await_args_list)

    # A config without the list (older engine) does not block the choice.
    backend.request.side_effect = _routes(PRICED)
    assert _run(create("space", 30, narrator_voice="atlas", expected_credits=18, confirm=True))["status"] == "queued"


def test_create_video_refuses_a_resolution_the_format_does_not_offer():
    from tests.test_tools import PRICED, _routes

    registered, backend = _server()
    config = {"resolutions_by_format": {"pov": {"available": ["720p"], "default": "720p"}}}
    backend.request.side_effect = _routes({**PRICED, "/api/config": config})
    with pytest.raises(ToolError, match="doesn't offer resolution '1080p'; it offers 720p"):
        _run(registered["create_video"].fn("space", 30, format="pov", resolution="1080p"))


@pytest.mark.parametrize(
    ("name", "args", "method", "path", "next_word"),
    [
        ("control_post", (ID, "cancel"), "POST", f"/api/posts/{ID}/cancel", "Cancelled"),
        ("control_post", (ID, "retry"), "POST", f"/api/posts/{ID}/retry", "not posted"),
        ("control_series_slot", (ID, "skip"), "POST", f"/api/workflow-slots/{ID}/skip", "Skipped"),
        ("control_series_slot", (ID, "retry"), "POST", f"/api/workflow-slots/{ID}/retry", "not made"),
        ("control_series_slot", (ID, "regenerate_idea", "funnier"), "POST",
         f"/api/workflow-slots/{ID}/regenerate-idea", "New idea"),
        ("delete_series", (ID,), "DELETE", f"/api/workflows/{ID}", "Deleted"),
        ("stop_series", (ID,), "POST", f"/api/workflows/{ID}/stop", "Stopped"),
        ("pause_series", (ID,), "POST", f"/api/workflows/{ID}/pause", "Paused"),
        ("control_video", (ID, "cancel"), "POST", f"/api/jobs/{ID}/cancel", "refunded"),
        ("connect_account", ("tiktok",), "POST", "/api/connections/tiktok/authorize", "list_connections"),
    ],
)
def test_confirmed_writes_hit_the_route_and_say_what_happens_next(name, args, method, path, next_word):
    registered, backend = _server()
    backend.request.return_value = {"ok": True}
    result = _run(registered[name].fn(*args, confirm=True))
    writes = [c.args[:2] for c in backend.request.await_args_list if c.args[0] != "GET"]
    assert writes == [(method, path)]  # reads around the write are fine; exactly one write
    assert next_word in result["next_step"]


def test_slot_and_metadata_writes_refuse_empty_or_misplaced_fields():
    registered, backend = _server()
    with pytest.raises(ToolError, match="only valid for regenerate_idea"):
        _run(registered["control_series_slot"].fn(ID, "skip", "funnier", confirm=True))
    with pytest.raises(ToolError, match="at least one slot field"):
        _run(registered["update_series_slot"].fn(ID, confirm=True))
    with pytest.raises(ToolError, match="at least one detail to change"):
        _run(registered["update_video_details"].fn(ID, confirm=True))
    backend.request.assert_not_awaited()


def test_disconnect_and_slot_update_forward_confirmed_values():
    registered, backend = _server()
    backend.request.return_value = {"ok": True}
    _run(registered["disconnect_account"].fn(ID, confirm=True))
    assert backend.request.await_args.args[:2] == ("DELETE", f"/api/connections/{ID}")
    _run(registered["update_series_slot"].fn(ID, title=" New idea ", confirm=True))
    assert backend.request.await_args.args[:2] == ("PATCH", f"/api/workflow-slots/{ID}")
    assert backend.request.await_args.kwargs["body"] == {"title": "New idea"}
    _run(registered["update_video_details"].fn(ID, tags=[" a "], visibility="unlisted", confirm=True))
    assert backend.request.await_args.kwargs["body"] == {"tags": ["a"], "visibility": "unlisted"}
