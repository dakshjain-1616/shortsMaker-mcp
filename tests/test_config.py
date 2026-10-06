import pytest

from shortsmaker_mcp.config import load_settings


def test_settings_require_absolute_backend_url(monkeypatch):
    monkeypatch.delenv("MCP_BACKEND_API_URL", raising=False)

    with pytest.raises(RuntimeError, match=r"absolute http\(s\) URL"):
        load_settings()


def test_settings_load_explicit_values(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://api.example.test/")
    monkeypatch.setenv("MCP_PORT", "9002")

    settings = load_settings()

    assert settings.backend_api_url == "https://api.example.test"
    assert settings.port == 9002


def test_settings_load_public_deployment_controls(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://api.example.test")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mcp.example.test")
    monkeypatch.setenv("MCP_DASHBOARD_ENABLED", "false")
    monkeypatch.setenv("MCP_RATE_LIMIT_REQUESTS", "12")
    monkeypatch.setenv("MCP_RATE_LIMIT_WINDOW_SECONDS", "30")

    settings = load_settings()

    assert settings.public_url == "https://mcp.example.test"
    assert settings.dashboard_enabled is False
    assert settings.rate_limit_requests == 12
    assert settings.rate_limit_window_seconds == 30


def test_settings_reject_invalid_public_url(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://api.example.test")
    monkeypatch.setenv("MCP_PUBLIC_URL", "mcp.example.test")

    with pytest.raises(RuntimeError, match=r"MCP_PUBLIC_URL must be an absolute http\(s\) URL"):
        load_settings()


def test_credits_per_usd_defaults_and_validates(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://api.example.test")

    assert load_settings().credits_per_usd == 100.0

    monkeypatch.setenv("MCP_CREDITS_PER_USD", "55")
    assert load_settings().credits_per_usd == 55.0

    monkeypatch.setenv("MCP_CREDITS_PER_USD", "0")
    with pytest.raises(RuntimeError, match="positive number"):
        load_settings()
