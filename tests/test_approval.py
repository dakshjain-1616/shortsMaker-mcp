from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from shortsmaker_mcp.oauth_auth import Principal, _principal
from shortsmaker_mcp.tools._common import _approval


def test_approval_is_bound_to_user_operation_and_expiry():
    alice = _principal.set(Principal("alice", frozenset({"mcp:write"}), "client"))
    try:
        preview = {"target": "video-1", "changes": {"title": "New"}}
        token = _approval(preview, False, None, "update_video_details")["preview_token"]
        assert _approval(preview, True, token, "update_video_details") is None
        with pytest.raises(ToolError, match="preview_changed"):
            _approval({"target": "video-2", "changes": {"title": "New"}}, True, token, "update_video_details")
        with pytest.raises(ToolError, match="preview_changed"):
            _approval(preview, True, token, "delete_series")
        with patch("shortsmaker_mcp.tools._common.datetime") as clock:
            from datetime import UTC, datetime, timedelta
            clock.now.return_value = datetime.now(UTC) + timedelta(minutes=16)
            with pytest.raises(ToolError, match="preview_changed"):
                _approval(preview, True, token, "update_video_details")
    finally:
        _principal.reset(alice)

    bob = _principal.set(Principal("bob", frozenset({"mcp:write"}), "client"))
    try:
        with pytest.raises(ToolError, match="preview_changed"):
            _approval(preview, True, token, "update_video_details")
    finally:
        _principal.reset(bob)
