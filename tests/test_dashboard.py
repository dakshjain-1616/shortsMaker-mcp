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
        "shortsmaker_get_credit_status",
        "shortsmaker_get_subscription",
        "shortsmaker_list_subscription_plans",
        "shortsmaker_get_account_features",
        "shortsmaker_resolve_video_price",
        "shortsmaker_get_video_options",
        "shortsmaker_list_videos",
        "shortsmaker_get_video",
        "shortsmaker_list_connections",
        "shortsmaker_list_posts",
        "shortsmaker_publishing_health",
        "shortsmaker_list_workflows",
        "shortsmaker_get_workflow",
        "shortsmaker_list_workflow_slots",
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
        "shortsmaker_create_video",
        "shortsmaker_control_video",
        "shortsmaker_update_video_metadata",
        "shortsmaker_start_connection",
        "shortsmaker_disconnect",
        "shortsmaker_publish_video",
        "shortsmaker_control_post",
    ):
        assert tool_name in html

    for removed in ("buy_credits", "start_subscription", "billing_portal", "cancel_subscription"):
        assert removed not in html

    assert "confirm: true" in html
    assert "newIdempotencyKey" in html
