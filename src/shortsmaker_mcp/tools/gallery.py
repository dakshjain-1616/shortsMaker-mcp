"""The visual gallery (an MCP App) that clients with UI support render for video and series lists.

Clients without MCP Apps support, such as Claude Code, ignore ``_meta.ui`` and use the same
structured result as text, so the gallery tools stay ordinary tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver.context import Context

GALLERY_URI = "ui://faceless-video-maker/gallery.html"
GALLERY_META = {"ui": {"resourceUri": GALLERY_URI}}
_GALLERY_HTML = Path(__file__).parent.parent / "static" / "gallery.html"


def gallery_extension(media_origins: list[str], app_url: str) -> Apps:
    """The Apps extension: advertises UI support and serves the gallery page.

    Thumbnails and videos are presigned storage URLs, so the page may only load media from
    ``media_origins``; it makes no network requests of its own.
    """
    apps = Apps()
    apps.add_html_resource(
        GALLERY_URI,
        _GALLERY_HTML.read_text(encoding="utf-8").replace("__APP_URL__", app_url),
        title="Faceless Video Maker gallery",
        description="Your videos and Autopilot series, with thumbnails and players.",
        csp=ResourceCsp(resource_domains=media_origins),
        prefers_border=False,
    )
    return apps


def shown_as_gallery(ctx: Context[Any] | None) -> bool:
    """Whether the client will render this result in the gallery, so the model need not list it."""
    return ctx is not None and client_supports_apps(ctx)
