"""``shortsmaker-mcp`` command: load ``.env`` if present, then serve the MCP app with uvicorn."""

from __future__ import annotations

import argparse

import uvicorn
from dotenv import load_dotenv

_EPILOG = """\
Settings come from environment variables or a .env file in the working directory:
  MCP_BACKEND_API_URL (required), MCP_HOST, MCP_PORT,
  MCP_API_TIMEOUT_SECONDS, MCP_API_CONNECT_TIMEOUT_SECONDS, MCP_DASHBOARD_ENABLED,
  MCP_RATE_LIMIT_REQUESTS, MCP_RATE_LIMIT_WINDOW_SECONDS.
See the README for what each one does."""


def main() -> None:
    argparse.ArgumentParser(
        prog="shortsmaker-mcp",
        description="Serve the ShortsMaker MCP endpoint at /mcp/ (Streamable HTTP).",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).parse_args()
    load_dotenv()  # real environment variables win over .env values
    # Imported after load_dotenv(): the app reads its settings from the environment at import.
    from .config import load_settings

    settings = load_settings()
    uvicorn.run("shortsmaker_mcp.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
