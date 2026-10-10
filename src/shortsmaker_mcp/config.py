"""Environment-backed configuration for the standalone MCP service."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _backend_url() -> str:
    value = os.getenv("MCP_BACKEND_API_URL", "").strip().rstrip("/")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise RuntimeError("MCP_BACKEND_API_URL must be an absolute http(s) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("MCP_BACKEND_API_URL must be an absolute http(s) URL")
    return value


@dataclass(frozen=True)
class Settings:
    backend_api_url: str
    host: str
    port: int
    api_timeout_seconds: float
    api_connect_timeout_seconds: float
    dashboard_enabled: bool = False
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0
    oauth_issuer: str | None = None
    resource_url: str | None = None
    bridge_secret: str | None = None


def _validate_vercel(settings: Settings) -> None:
    """Fail at startup when a Vercel deployment still has local or incomplete settings."""
    urls = {
        "MCP_BACKEND_API_URL": settings.backend_api_url,
        "MCP_OAUTH_ISSUER": settings.oauth_issuer,
        "MCP_RESOURCE_URL": settings.resource_url,
    }
    for name, value in urls.items():
        try:
            parsed = urlparse(value or "")
            _ = parsed.port  # Validate malformed ports as well as malformed hostnames.
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a public HTTPS URL on Vercel") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"{name} must be a public HTTPS URL on Vercel")
        if name == "MCP_RESOURCE_URL":
            if parsed.path != "/mcp/":
                raise RuntimeError("MCP_RESOURCE_URL must end in /mcp/ on Vercel")
        elif parsed.path:
            raise RuntimeError(f"{name} must be an HTTPS origin without a path on Vercel")
    if not settings.bridge_secret or len(settings.bridge_secret) < 32:
        raise RuntimeError("MCP_BRIDGE_SECRET must be at least 32 characters on Vercel")
    if settings.dashboard_enabled:
        raise RuntimeError("MCP_DASHBOARD_ENABLED must be false on Vercel")


def load_settings() -> Settings:
    backend_api_url = _backend_url()
    resource_url = os.getenv("MCP_RESOURCE_URL", "").strip() or None
    try:
        resource = urlparse(resource_url or "")
        _ = resource.port
    except ValueError as exc:
        raise RuntimeError("MCP_RESOURCE_URL must be an absolute http(s) URL") from exc
    settings = Settings(
        backend_api_url=backend_api_url,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=_positive_int("MCP_PORT", 8002),
        api_timeout_seconds=_positive_float("MCP_API_TIMEOUT_SECONDS", 45.0),
        api_connect_timeout_seconds=_positive_float("MCP_API_CONNECT_TIMEOUT_SECONDS", 10.0),
        dashboard_enabled=_boolean("MCP_DASHBOARD_ENABLED", False),
        rate_limit_requests=_positive_int("MCP_RATE_LIMIT_REQUESTS", 120),
        rate_limit_window_seconds=_positive_float("MCP_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        oauth_issuer=os.getenv("MCP_OAUTH_ISSUER", "").strip().rstrip("/") or backend_api_url,
        resource_url=resource_url,
        bridge_secret=os.getenv("MCP_BRIDGE_SECRET", "").strip() or None,
    )
    if os.getenv("VERCEL") == "1":
        _validate_vercel(settings)
    return settings
