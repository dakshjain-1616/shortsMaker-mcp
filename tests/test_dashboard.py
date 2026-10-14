from pathlib import Path


def test_dashboard_exposes_read_only_controls():
    html = (
        Path(__file__).parents[1] / "src" / "shortsmaker_mcp" / "static" / "dashboard.html"
    ).read_text()

    for element_id in (
        "price",
        "credits",
        "subscription",
        "plans",
        "features",
        "options",
        "videos",
        "video",
        "connections",
        "posts",
        "health",
        "workflows",
        "workflow",
        "workflow-slots",
    ):
        assert f'id="{element_id}"' in html

    for tool_name in (
        "get_credits",
        "get_subscription",
        "list_plans",
        "get_account_features",
        "get_video_price",
        "get_video_options",
        "list_videos",
        "get_video",
        "list_connections",
        "list_posts",
        "check_publishing_health",
        "list_series",
        "get_series",
        "list_series_slots",
    ):
        assert tool_name in html

    assert "localStorage" in html


def test_dashboard_exposes_explicitly_gated_write_controls():
    html = (
        Path(__file__).parents[1] / "src" / "shortsmaker_mcp" / "static" / "dashboard.html"
    ).read_text()

    for element_id in (
        "create-video",
        "create-confirm",
        "create-idempotency",
        "control-video",
        "control-confirm",
        "update-metadata",
        "metadata-confirm",
        "start-connection",
        "connection-confirm",
        "disconnect",
        "disconnect-confirm",
        "publish-video",
        "publish-confirm",
        "control-post",
        "post-confirm",
    ):
        assert f'id="{element_id}"' in html

    for tool_name in (
        "create_video",
        "control_video",
        "update_video_details",
        "connect_account",
        "disconnect_account",
        "publish_video",
        "control_post",
    ):
        assert tool_name in html

    for removed in ("buy_credits", "start_subscription", "billing_portal", "cancel_subscription"):
        assert removed not in html

    assert "confirm: true" in html
    assert "newIdempotencyKey" in html
