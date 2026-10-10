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
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://mcp.example.test/mcp/")
    monkeypatch.setenv("MCP_DASHBOARD_ENABLED", "false")
    monkeypatch.setenv("MCP_RATE_LIMIT_REQUESTS", "12")
    monkeypatch.setenv("MCP_RATE_LIMIT_WINDOW_SECONDS", "30")

    settings = load_settings()

    assert settings.resource_url == "https://mcp.example.test/mcp/"
    assert settings.dashboard_enabled is False
    assert settings.rate_limit_requests == 12
    assert settings.rate_limit_window_seconds == 30


def test_settings_reject_malformed_resource_url(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://api.example.test")
    monkeypatch.setenv("MCP_RESOURCE_URL", "http://[broken/mcp/")

    with pytest.raises(RuntimeError, match=r"MCP_RESOURCE_URL must be an absolute http\(s\) URL"):
        load_settings()


def test_dashboard_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "http://backend.test")
    monkeypatch.delenv("MCP_DASHBOARD_ENABLED", raising=False)
    assert load_settings().dashboard_enabled is False


@pytest.fixture
def vercel_env(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MCP_BACKEND_API_URL", "https://backend-test.ngrok-free.app")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://mcp-test.vercel.app/mcp/")
    monkeypatch.setenv("MCP_BRIDGE_SECRET", "test-bridge-secret-with-more-than-32-characters")
    monkeypatch.setenv("MCP_DASHBOARD_ENABLED", "false")
    monkeypatch.delenv("MCP_OAUTH_ISSUER", raising=False)
    return monkeypatch


def test_vercel_accepts_public_https_settings(vercel_env):
    settings = load_settings()
    assert settings.resource_url == "https://mcp-test.vercel.app/mcp/"
    assert settings.oauth_issuer == settings.backend_api_url
    assert settings.dashboard_enabled is False


@pytest.mark.parametrize("name", ["MCP_RESOURCE_URL", "MCP_BRIDGE_SECRET"])
def test_vercel_rejects_missing_oauth_settings(vercel_env, name):
    vercel_env.delenv(name)
    with pytest.raises(RuntimeError, match=name):
        load_settings()


@pytest.mark.parametrize(
    "name,value",
    [
        ("MCP_BACKEND_API_URL", "http://127.0.0.1:8001"),
        ("MCP_OAUTH_ISSUER", "https://localhost"),
        ("MCP_OAUTH_ISSUER", "https://backend.test/oauth"),
        ("MCP_RESOURCE_URL", "https://mcp-test.vercel.app/mcp"),
        ("MCP_RESOURCE_URL", "https://mcp-test.vercel.app/mcp/?x=1"),
        ("MCP_RESOURCE_URL", "https://user:password@mcp-test.vercel.app/mcp/"),
        ("MCP_RESOURCE_URL", "https://mcp-test.vercel.app:bad/mcp/"),
        ("MCP_BRIDGE_SECRET", "too-short"),
        ("MCP_DASHBOARD_ENABLED", "true"),
    ],
)
def test_vercel_rejects_unusable_or_unsafe_settings(vercel_env, name, value):
    vercel_env.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        load_settings()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_timeout_must_be_finite(monkeypatch, value):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "http://backend.test")
    monkeypatch.setenv("MCP_API_TIMEOUT_SECONDS", value)
    with pytest.raises(RuntimeError, match="positive number"):
        load_settings()
