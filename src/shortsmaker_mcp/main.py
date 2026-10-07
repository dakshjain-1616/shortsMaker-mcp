"""ASGI entrypoint for the standalone ShortsMaker MCP service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, Router
from starlette.types import ASGIApp

from .backend_client import BackendClient
from .config import Settings, load_settings
from .oauth_auth import BearerValidationMiddleware, TokenValidator
from .rate_limit import RequestRateLimiter
from .tool_auth import ToolAuthMiddleware
from .tools import register_tools

settings = load_settings()
backend = BackendClient(settings)
token_validator = TokenValidator(settings)
server = MCPServer(
    "ShortsMaker",
    description="User-scoped video generation and publishing tools for ShortsMaker.",
    instructions=(
        "Use read-only tools to inspect options, price, credits, jobs, connections, and posts. "
        "Before any write, explain the exact action, cost, destination, and asynchronous outcome "
        "to the user and obtain explicit approval. Never claim a job or post is complete merely "
        "because the API accepted it. Provider credentials must be handled through browser OAuth, "
        "not pasted into chat. You can read credits, subscription, and plans, but you cannot buy "
        "credits or change billing: direct the user to the ShortsMaker website for that."
    ),
    version="0.2.0",
)
register_tools(server, backend)
server.middleware.append(ToolAuthMiddleware(settings.resource_url, server.list_tools))
_DASHBOARD_PATH = Path(__file__).with_name("static") / "dashboard.html"


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    # Only this service's own public hostname is a valid Host/Origin; the backend's is not.
    hosts = ["localhost:*", "127.0.0.1:*", "[::1]:*"]
    origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]

    public_url = settings.public_url or settings.resource_url
    if public_url:
        public = urlparse(public_url)
        hosts += [public.netloc, f"{public.hostname}:*"]
        origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def _dashboard(request):
    return FileResponse(_DASHBOARD_PATH, media_type="text/html")


def _health_live(request):
    return JSONResponse({"status": "ok", "service": "shortsmaker-mcp", "version": server.version})


class _NormalizeMcpEndpointPath:
    """Serve the canonical MCP endpoint without redirecting clients that omit its final slash."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def _protected_resource_metadata(request):
    if not settings.resource_url or not settings.oauth_issuer:
        return JSONResponse({"detail": "MCP OAuth is not configured."}, status_code=503)
    return JSONResponse(
        {
            "resource": settings.resource_url,
            "authorization_servers": [settings.oauth_issuer],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "no-store"}
    )


def create_app() -> ASGIApp:
    """Build the protected MCP endpoint and local testing dashboard."""
    protocol_app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        # Requests may reach different Vercel instances. Each carries its own OAuth token.
        stateless_http=True,
        transport_security=_transport_security(settings),
    )
    manager = server.session_manager

    @asynccontextmanager
    async def lifespan(app):
        # Mounted apps do not run their own lifespan; own this app's manager explicitly.
        try:
            async with manager.run():
                yield
        finally:
            await backend.aclose()
            await token_validator.aclose()

    protected_mcp = BearerValidationMiddleware(
        # JSON requests finish within one function invocation. Do not open an idle GET/SSE
        # stream on Vercel. Authenticate first so an initial GET still receives OAuth discovery.
        Router(routes=[Route("/", endpoint=protocol_app, methods=["POST"])]),
        token_validator,
        settings,
        RequestRateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds),
    )
    routes = [
        Route("/health/live", _health_live, methods=["GET"]),
        Route(
            "/.well-known/oauth-protected-resource", _protected_resource_metadata, methods=["GET"]
        ),
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            _protected_resource_metadata,
            methods=["GET"],
        ),
        Mount("/mcp", app=protected_mcp),
    ]
    if settings.dashboard_enabled:
        routes[3:3] = [Route("/dashboard", _dashboard), Route("/dashboard/", _dashboard)]
    application = Starlette(
        routes=routes,
        lifespan=lifespan,
    )
    return _NormalizeMcpEndpointPath(application)


app = create_app()
