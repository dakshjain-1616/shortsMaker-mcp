"""Environment-backed configuration for the standalone MCP service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
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
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("MCP_BACKEND_API_URL must be an absolute http(s) URL")
    return value


def _optional_public_url() -> str | None:
    value = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("MCP_PUBLIC_URL must be an absolute http(s) URL")
    return value


@dataclass(frozen=True)
class Settings:
    backend_api_url: str
    host: str
    port: int
    api_timeout_seconds: float
    api_connect_timeout_seconds: float
    public_url: str | None = None
    dashboard_enabled: bool = True
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0
    # Must match the backend's CREDITS_PER_DOLLAR; used only to show a quote in credits.
    credits_per_usd: float = 100.0
    oauth_issuer: str | None = None
    resource_url: str | None = None
    bridge_secret: str | None = None


def load_settings() -> Settings:
    return Settings(
        backend_api_url=_backend_url(),
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=_positive_int("MCP_PORT", 8002),
        api_timeout_seconds=_positive_float("MCP_API_TIMEOUT_SECONDS", 45.0),
        api_connect_timeout_seconds=_positive_float("MCP_API_CONNECT_TIMEOUT_SECONDS", 10.0),
        public_url=_optional_public_url(),
        dashboard_enabled=_boolean("MCP_DASHBOARD_ENABLED", True),
        rate_limit_requests=_positive_int("MCP_RATE_LIMIT_REQUESTS", 120),
        rate_limit_window_seconds=_positive_float("MCP_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        credits_per_usd=_positive_float("MCP_CREDITS_PER_USD", 100.0),
        oauth_issuer=os.getenv("MCP_OAUTH_ISSUER", "").strip().rstrip("/") or None,
        # resource we are accessing and the the server that authorizes access
        resource_url=os.getenv("MCP_RESOURCE_URL", "").strip() or None,
        bridge_secret=os.getenv("MCP_BRIDGE_SECRET", "").strip() or None,
    )
