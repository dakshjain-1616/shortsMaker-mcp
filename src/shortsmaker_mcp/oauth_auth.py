"""OAuth resource-server boundary for the HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import jwt
from jwt.exceptions import InvalidTokenError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .rate_limit import RequestRateLimiter


@dataclass(frozen=True)
class Principal:
    user_id: str
    scopes: frozenset[str]
    client_id: str


_principal: ContextVar[Principal | None] = ContextVar("shortsmaker_mcp_principal", default=None)


def current_principal() -> Principal:
    principal = _principal.get()
    if principal is None:
        raise RuntimeError("No authenticated MCP request is active")
    return principal


def _bearer(scope: Scope) -> str | None:
    headers = [value for key, value in scope.get("headers", ()) if key.lower() == b"authorization"]
    if len(headers) != 1:
        return None
    scheme, separator, token = headers[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()


class TokenValidator:
    """Verify backend-signed access JWTs against the backend's published public keys."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.issuer = settings.oauth_issuer
        self.resource = settings.resource_url
        self._client = client or httpx.AsyncClient(timeout=5.0, trust_env=False)
        self._owns_client = client is None
        self._keys: dict[str, object] = {}
        self._keys_until = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _refresh_keys(self) -> None:
        if not self.issuer:
            raise RuntimeError("MCP_OAUTH_ISSUER is required")
        response = await self._client.get(f"{self.issuer}/oauth/jwks")
        response.raise_for_status()
        try:
            document = response.json()
        except ValueError as exc:
            raise RuntimeError("Invalid OAuth JWKS") from exc
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise RuntimeError("Invalid OAuth JWKS")
        keys = {}
        for item in document["keys"]:
            if not isinstance(item, dict) or item.get("kty") != "RSA" or item.get("alg") not in (None, "RS256"):
                continue
            if item.get("use") not in (None, "sig"):
                continue
            kid = item.get("kid")
            if isinstance(kid, str) and kid:
                try:
                    keys[kid] = jwt.PyJWK.from_dict(item, algorithm="RS256").key
                except jwt.PyJWTError as exc:
                    raise RuntimeError("Invalid OAuth signing key") from exc
        if not keys:
            raise RuntimeError("OAuth JWKS contains no RSA signing keys")
        self._keys = keys
        self._keys_until = time.monotonic() + 300

    async def validate(self, token: str) -> Principal:
        if not self.issuer or not self.resource:
            raise RuntimeError("MCP OAuth issuer and resource URL are required")
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if unverified.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise InvalidTokenError("Unsupported token signing key")
        async with self._lock:
            if kid not in self._keys or self._keys_until <= time.monotonic():
                await self._refresh_keys()
            key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError("Unknown token signing key")
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=self.resource,
            leeway=30,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "client_id", "scope", "token_use"], "strict_aud": True},
        )
        if claims.get("token_use") != "mcp_access":
            raise InvalidTokenError("Wrong token type")
        user_id, client_id, scope = claims["sub"], claims["client_id"], claims["scope"]
        if not all(isinstance(value, str) and value for value in (user_id, client_id, scope)):
            raise InvalidTokenError("Invalid token identity or scope")
        scopes = frozenset(scope.split())
        if not scopes or not scopes <= {"mcp:read", "mcp:write"}:
            raise InvalidTokenError("Invalid token scopes")
        return Principal(user_id, scopes, client_id)


def resource_metadata_url(resource_url: str | None) -> str:
    if not resource_url:
        return ""
    parsed = urlparse(resource_url)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path.rstrip('/')}"


## On the first unauthenticated request client has no token , so it returns 401 with a login link to authenticate user
class BearerValidationMiddleware:
    """Require a valid MCP access token on every protocol HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        validator: TokenValidator,
        settings: Settings,
        rate_limiter: RequestRateLimiter | None = None,
    ):
        self.app = app
        self.validator = validator
        self.rate_limiter = rate_limiter or RequestRateLimiter(120, 60.0)
        self.resource_metadata_url = resource_metadata_url(settings.resource_url)

    def _challenge(self, error: str | None = None, scope: str = "mcp:read mcp:write") -> str:
        parts = [f'resource_metadata="{self.resource_metadata_url}"', f'scope="{scope}"']
        if error:
            parts.append(f'error="{error}"')
        return "Bearer " + ", ".join(parts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.validator.issuer or not self.validator.resource or not self.resource_metadata_url:
            await JSONResponse(
                {"detail": "MCP OAuth is not configured."}, status_code=503
            )(scope, receive, send)
            return
        client = scope.get("client")
        client_key = client[0] if client else "unknown"
        if not self.rate_limiter.allow(client_key):
            await JSONResponse(
                {"detail": "Too many MCP requests. Try again shortly."},
                status_code=429,
                headers={"Retry-After": str(int(self.rate_limiter.window_seconds))},
            )(scope, receive, send)
            return
        token = _bearer(scope)
        if not token:
            await JSONResponse(
                {"detail": "MCP authorization is required."},
                status_code=401,
                headers={"WWW-Authenticate": self._challenge()},
            )(scope, receive, send)
            return
        try:
            principal = await self.validator.validate(token)
        except InvalidTokenError:
            await JSONResponse(
                {"detail": "Invalid or expired MCP access token."},
                status_code=401,
                headers={"WWW-Authenticate": self._challenge("invalid_token")},
            )(scope, receive, send)
            return
        except (httpx.HTTPError, RuntimeError):
            await JSONResponse(
                {"detail": "MCP authorization is temporarily unavailable."}, status_code=503
            )(scope, receive, send)
            return
        context = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(context)
