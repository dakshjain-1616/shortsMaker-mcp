import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shortsmaker_mcp.backend_client import BackendAPIError, BackendClient, BackendUnavailable
from shortsmaker_mcp.config import Settings


def _client() -> BackendClient:
    return BackendClient(
        Settings(
            backend_api_url="http://backend.test",
            host="127.0.0.1",
            port=8002,
            api_timeout_seconds=1,
            api_connect_timeout_seconds=1,
        )
    )


def test_request_forwards_bearer_token_and_json_body():
    response = httpx.Response(200, json={"ok": True})
    request = AsyncMock(return_value=response)
    client = _client()

    with patch("httpx.AsyncClient.request", request):
        result = asyncio.run(
            client.request(
                "POST",
                "/api/example",
                "access-token",
                params={"page": 1},
                body={"name": "demo"},
            )
        )

    assert result == {"ok": True}
    request.assert_awaited_once()
    kwargs = request.await_args.kwargs
    assert kwargs["headers"] == {"Authorization": "Bearer access-token"}
    assert kwargs["params"] == {"page": 1}
    assert kwargs["json"] == {"name": "demo"}


def test_request_raises_backend_api_error():
    response = httpx.Response(401, json={"detail": "Invalid token"})
    request = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient.request", request):
        with pytest.raises(BackendAPIError) as exc_info:
            asyncio.run(_client().request("GET", "/api/features", "bad-token"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


def test_request_translates_transport_errors():
    request = AsyncMock(side_effect=httpx.ConnectError("offline"))

    with patch("httpx.AsyncClient.request", request):
        with pytest.raises(BackendUnavailable):
            asyncio.run(_client().request("GET", "/api/features", "access-token"))
