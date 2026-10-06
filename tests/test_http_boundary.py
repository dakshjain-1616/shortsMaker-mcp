import asyncio
import os
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
from starlette.responses import JSONResponse

os.environ.setdefault("MCP_BACKEND_API_URL", "http://backend.test")

from shortsmaker_mcp.auth import BearerValidationMiddleware
from shortsmaker_mcp.backend_client import BackendAPIError, BackendClient
from shortsmaker_mcp.config import Settings
from shortsmaker_mcp.main import app, server
from shortsmaker_mcp.rate_limit import RequestRateLimiter


def _backend_settings() -> Settings:
    return Settings(
        backend_api_url="http://backend.test",
        host="127.0.0.1",
        port=8002,
        api_timeout_seconds=1,
        api_connect_timeout_seconds=1,
    )


def _backend() -> BackendClient:
    return BackendClient(_backend_settings())


def test_missing_bearer_is_rejected_at_http_boundary():
    async def check():
        protocol_app = server.streamable_http_app(streamable_http_path="/", json_response=True)
        transport = httpx.ASGITransport(app=BearerValidationMiddleware(protocol_app, _backend()))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            return await client.get("/")

    response = asyncio.run(check())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_valid_bearer_reaches_protocol_app():
    async def check():
        backend = _backend()
        backend.validate_token = AsyncMock(return_value={"promo_end_card": False})
        app = BearerValidationMiddleware(
            server.streamable_http_app(streamable_http_path="/", json_response=True), backend
        )
        transport = httpx.ASGITransport(app=app)
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                return await client.post(
                    "/",
                    headers={
                        "Authorization": "Bearer test-token",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                    },
                )

    response = asyncio.run(check())

    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "ShortsMaker"


def test_mcp_boundary_rate_limits_requests_per_client():
    async def check():
        protocol_app = server.streamable_http_app(streamable_http_path="/", json_response=True)
        middleware = BearerValidationMiddleware(
            protocol_app,
            _backend(),
            RequestRateLimiter(limit=1, window_seconds=60),
        )
        transport = httpx.ASGITransport(app=middleware)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            first = await client.get("/")
            second = await client.get("/")
            return first, second

    first, second = asyncio.run(check())

    assert first.status_code == 401
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"


def test_live_health_endpoint_is_available_without_authentication():
    async def check():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            return await client.get("/health/live")

    response = asyncio.run(check())

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def _ok_app(scope, receive, send):
    await JSONResponse({})(scope, receive, send)


def _post_with_token(middleware):
    async def check():
        transport = httpx.ASGITransport(app=middleware)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            return await client.get("/", headers={"Authorization": "Bearer test-token"})

    return asyncio.run(check())


def test_successful_token_validation_is_cached_within_ttl():
    backend = _backend()
    backend.validate_token = AsyncMock(return_value={})
    middleware = BearerValidationMiddleware(_ok_app, backend, validation_ttl_seconds=60)

    for _ in range(3):
        _post_with_token(middleware)

    backend.validate_token.assert_awaited_once()


def test_token_is_revalidated_after_ttl_expires():
    backend = _backend()
    backend.validate_token = AsyncMock(return_value={})
    middleware = BearerValidationMiddleware(_ok_app, backend, validation_ttl_seconds=0.001)

    _post_with_token(middleware)
    time.sleep(0.01)
    _post_with_token(middleware)

    assert backend.validate_token.await_count == 2


def test_rejected_token_is_not_cached():
    backend = _backend()
    backend.validate_token = AsyncMock(side_effect=BackendAPIError(401, "bad"))
    middleware = BearerValidationMiddleware(_ok_app, backend, validation_ttl_seconds=60)

    first, second = _post_with_token(middleware), _post_with_token(middleware)

    assert first.status_code == second.status_code == 401
    assert backend.validate_token.await_count == 2


def test_backend_rate_limit_during_validation_returns_429():
    backend = _backend()
    backend.validate_token = AsyncMock(side_effect=BackendAPIError(429, "slow down"))

    response = _post_with_token(BearerValidationMiddleware(_ok_app, backend))

    assert response.status_code == 429
    assert "retry-after" in response.headers


def test_transport_security_excludes_backend_host_and_includes_public_host():
    from shortsmaker_mcp import main

    settings = replace(
        _backend_settings(), backend_api_url="https://api.backend.test", public_url="https://mcp.example.com"
    )

    security = main._transport_security(settings)

    assert "mcp.example.com" in security.allowed_hosts
    assert "https://mcp.example.com" in security.allowed_origins
    assert not any("backend.test" in value for value in security.allowed_hosts + security.allowed_origins)
