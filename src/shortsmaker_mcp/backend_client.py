"""Small HTTP client for the existing user-facing Platform API."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt

from .config import Settings
from .oauth_auth import Principal

logger = logging.getLogger("shortsmaker_mcp.backend")


class BackendUnavailable(Exception):
    """The Platform API could not be reached or returned an unusable response."""


class BackendAPIError(Exception):
    """The Platform API returned a deliberate HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class BackendClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.backend_api_url
        self.credits_per_usd = settings.credits_per_usd
        self.bridge_secret = settings.bridge_secret
        # One pooled client: avoids a TCP/TLS handshake per upstream call.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.api_timeout_seconds,
                connect=settings.api_connect_timeout_seconds,
            ),
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def bridge_token(self, principal: Principal) -> str:
        """Create a short-lived user assertion for the backend API only."""
        if not self.bridge_secret or len(self.bridge_secret) < 32:
            raise RuntimeError("MCP_BRIDGE_SECRET must be at least 32 characters")
        now = int(time.time())
        return jwt.encode(
            {
                "iss": "shortsmaker-mcp",
                "aud": "shortsmaker-api",
                "token_use": "mcp_bridge",
                "sub": principal.user_id,
                "scope": " ".join(sorted(principal.scopes)),
                "iat": now,
                "exp": now + 60,
            },
            self.bridge_secret,
            algorithm="HS256",
        )

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            logger.warning("Backend timeout for %s %s", method, path)
            raise BackendUnavailable from exc
        except httpx.HTTPError as exc:
            logger.warning("Backend unavailable for %s %s: %s", method, path, type(exc).__name__)
            raise BackendUnavailable from exc

        try:
            result = response.json() if response.content else None
        except ValueError as exc:
            logger.warning("Backend returned non-JSON for %s %s", method, path)
            raise BackendUnavailable from exc

        if not response.is_success:
            detail = result.get("detail") if isinstance(result, dict) else None
            message = detail if isinstance(detail, str) else f"Request failed ({response.status_code})."
            raise BackendAPIError(response.status_code, message[:500])
        return result

    async def validate_token(self, token: str) -> Any:
        return await self.request("GET", "/api/features", token)
