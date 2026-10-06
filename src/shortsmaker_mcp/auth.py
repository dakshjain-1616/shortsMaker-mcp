"""Prototype bearer-token boundary for the standalone MCP service."""

from __future__ import annotations

import hashlib
import time
from contextvars import ContextVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .backend_client import BackendClient, BackendUnavailable
from .rate_limit import RequestRateLimiter

_token: ContextVar[str | None] = ContextVar("shortsmaker_mcp_bearer_token", default=None)


def current_token() -> str:
    value = _token.get()
    if not value:
        raise RuntimeError("No authenticated MCP request is active")
    return value


def _bearer(scope: Scope) -> str | None:
    raw = next((value for key, value in scope.get("headers", ()) if key == b"authorization"), b"")
    value = raw.decode("latin-1")
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()


class BearerValidationMiddleware:
    """Validate the prototype token through the existing Platform API.

    The MCP service does not mint or cryptographically validate tokens yet. It asks the existing
    API to validate the supplied access token, then stores it only for the lifetime of this HTTP
    request so tool calls can forward it upstream.

    Successful validations are cached briefly (keyed by a token hash) so a burst of MCP requests
    does not double every upstream call. A revoked token therefore stays accepted here for at
    most ``validation_ttl_seconds``; the backend still rejects it on every tool call.
    """

    _MAX_CACHED_TOKENS = 1024

    def __init__(
        self,
        app: ASGIApp,
        backend: BackendClient,
        rate_limiter: RequestRateLimiter | None = None,
        validation_ttl_seconds: float = 30.0,
    ):
        self.app = app
        self.backend = backend
        self.rate_limiter = rate_limiter or RequestRateLimiter(120, 60.0)
        self.validation_ttl_seconds = validation_ttl_seconds
        self._validated_until: dict[str, float] = {}

    async def _validate(self, token: str) -> None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        if self._validated_until.get(key, 0.0) > now:
            return
        await self.backend.validate_token(token)
        if len(self._validated_until) >= self._MAX_CACHED_TOKENS:
            self._validated_until = {k: v for k, v in self._validated_until.items() if v > now}
            if len(self._validated_until) >= self._MAX_CACHED_TOKENS:
                self._validated_until.clear()
        self._validated_until[key] = now + self.validation_ttl_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
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
                {"detail": "Missing or invalid authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )(scope, receive, send)
            return

        try:
            await self._validate(token)
        except BackendUnavailable:
            await JSONResponse(
                {"detail": "ShortsMaker API is temporarily unavailable."}, status_code=503
            )(scope, receive, send)
            return
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 401:
                await JSONResponse(
                    {"detail": "Invalid or expired ShortsMaker access token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )(scope, receive, send)
                return
            if status_code == 429:
                await JSONResponse(
                    {"detail": "ShortsMaker API rate limit reached. Try again shortly."},
                    status_code=429,
                    headers={"Retry-After": "10"},
                )(scope, receive, send)
                return
            if status_code == 403:
                await JSONResponse({"detail": "This ShortsMaker account is not active."}, status_code=403)(
                    scope, receive, send
                )
                return
            await JSONResponse(
                {"detail": "ShortsMaker authentication could not be verified."}, status_code=503
            )(scope, receive, send)
            return

        context_token = _token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _token.reset(context_token)
