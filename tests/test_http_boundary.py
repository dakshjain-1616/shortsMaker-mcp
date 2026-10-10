"""Checks for the live HTTP entrypoint, not the removed website-token prototype."""

import asyncio
import os
from dataclasses import replace

import httpx

os.environ.setdefault("MCP_BACKEND_API_URL", "http://backend.test")

from shortsmaker_mcp import main
from shortsmaker_mcp.config import Settings


def test_live_health_endpoint_is_available_without_authentication():
    async def check():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            return await client.get("/health/live")

    response = asyncio.run(check())

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_transport_security_excludes_backend_host_and_includes_public_host():
    settings = replace(
        Settings(
            backend_api_url="http://backend.test",
            host="127.0.0.1",
            port=8002,
            api_timeout_seconds=1,
            api_connect_timeout_seconds=1,
        ),
        backend_api_url="https://api.backend.test",
        resource_url="https://mcp.example.com/mcp/",
    )

    security = main._transport_security(settings)

    assert "mcp.example.com" in security.allowed_hosts
    assert "https://mcp.example.com" in security.allowed_origins
    assert not any("backend.test" in value for value in security.allowed_hosts + security.allowed_origins)
