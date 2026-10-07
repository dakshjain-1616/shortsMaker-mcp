import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from shortsmaker_mcp.backend_client import BackendClient
from shortsmaker_mcp.config import Settings
from shortsmaker_mcp.oauth_auth import (
    BearerValidationMiddleware,
    Principal,
    TokenValidator,
    _principal,
)
from shortsmaker_mcp.tools._common import _api

ISSUER = "https://api.example.test"
RESOURCE = "https://mcp.example.test/mcp/"
SECRET = "long-test-secret-for-bridge-assertions-123456"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True)
PUBLIC_JWK.update({"kid": "key-1", "alg": "RS256", "use": "sig"})


def settings():
    return Settings(
        backend_api_url=ISSUER,
        host="127.0.0.1",
        port=8002,
        api_timeout_seconds=1,
        api_connect_timeout_seconds=1,
        oauth_issuer=ISSUER,
        resource_url=RESOURCE,
        bridge_secret=SECRET,
    )


def access_token(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": RESOURCE,
        "client_id": "client-1",
        "scope": "mcp:read mcp:write",
        "token_use": "mcp_access",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "key-1"})


def validator():
    async def jwks(request):
        return JSONResponse({"keys": [PUBLIC_JWK]})

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=[Route("/oauth/jwks", jwks)])),
        base_url=ISSUER,
    )
    return TokenValidator(settings(), client=client)


def test_jwks_validation_requires_issuer_audience_type_and_expiry():
    async def check():
        verifier = validator()
        principal = await verifier.validate(access_token())
        assert principal == Principal(
            "user-1", frozenset({"mcp:read", "mcp:write"}), "client-1"
        )
        for altered in (
            {"iss": "https://wrong.test"},
            {"aud": "https://api.example.test"},
            {"aud": [RESOURCE, "https://another-resource.test"]},
            {"token_use": "website_access"},
            {"exp": int(time.time()) - 100},
        ):
            with pytest.raises(jwt.InvalidTokenError):
                await verifier.validate(access_token(**altered))
        await verifier._client.aclose()

    asyncio.run(check())


def test_protected_resource_metadata_advertises_exact_resource_and_issuer(monkeypatch):
    from shortsmaker_mcp import main

    monkeypatch.setattr(main, "settings", settings())

    async def check():
        transport = httpx.ASGITransport(app=main.create_app())
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test") as client:
            return await client.get("/.well-known/oauth-protected-resource/mcp")

    response = asyncio.run(check())
    assert response.status_code == 200
    assert response.json()["resource"] == RESOURCE
    assert response.json()["authorization_servers"] == [ISSUER]
    assert response.headers["cache-control"] == "no-store"


def test_http_challenge_advertises_resource_metadata_and_scopes():
    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    async def check():
        verifier = validator()
        middleware = BearerValidationMiddleware(app, verifier, settings())
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=middleware), base_url="https://mcp.example.test"
        )
        missing = await client.get("/mcp/")
        bad = await client.get("/mcp/", headers={"Authorization": "Bearer garbage"})
        good = await client.get("/mcp/", headers={"Authorization": f"Bearer {access_token()}"})
        await client.aclose()
        await verifier._client.aclose()
        return missing, bad, good

    missing, bad, good = asyncio.run(check())
    assert all(response.headers["cache-control"] == "no-store" for response in (missing, bad, good))
    assert missing.status_code == 401
    assert 'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"' in missing.headers["www-authenticate"]
    assert 'scope="mcp:read mcp:write"' in missing.headers["www-authenticate"]
    assert bad.status_code == 401
    assert 'error="invalid_token"' in bad.headers["www-authenticate"]
    assert good.status_code == 200



def test_write_only_token_can_enter_mcp_and_read_tool_is_denied():
    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    async def check():
        verifier = validator()
        middleware = BearerValidationMiddleware(app, verifier, settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=middleware), base_url="https://mcp.example.test"
        ) as client:
            response = await client.get(
                "/mcp/",
                headers={"Authorization": f"Bearer {access_token(scope='mcp:write')}"},
            )
        await verifier._client.aclose()
        return response

    assert asyncio.run(check()).status_code == 200

    from unittest.mock import AsyncMock, Mock

    backend = AsyncMock()
    backend.bridge_token = Mock(return_value="bridge-token")
    context = _principal.set(Principal("user-1", frozenset({"mcp:write"}), "client-1"))
    try:
        with pytest.raises(ToolError, match="mcp:read"):
            asyncio.run(_api(backend, "GET", "/api/credits"))
    finally:
        _principal.reset(context)
    backend.bridge_token.assert_not_called()


def test_write_scope_is_enforced_before_bridge_creation():
    from unittest.mock import AsyncMock, Mock

    backend = AsyncMock()
    backend.bridge_token = Mock(return_value="bridge-token")
    principal = Principal("user-1", frozenset({"mcp:read"}), "client-1")
    context = _principal.set(principal)
    try:
        with pytest.raises(ToolError, match="mcp:write"):
            asyncio.run(_api(backend, "POST", "/api/jobs", body={"name": "test"}))
    finally:
        _principal.reset(context)
    backend.bridge_token.assert_not_called()
    backend.request.assert_not_called()


def test_backend_bridge_has_separate_audience_and_sixty_second_lifetime():
    backend = BackendClient(settings())
    assertion = backend.bridge_token(Principal("user-1", frozenset({"mcp:read"}), "client-1"))
    claims = jwt.decode(assertion, SECRET, algorithms=["HS256"], audience="shortsmaker-api")
    assert claims["iss"] == "shortsmaker-mcp"
    assert claims["token_use"] == "mcp_bridge"
    assert claims["scope"] == "mcp:read"
    assert claims["exp"] - claims["iat"] == 60
