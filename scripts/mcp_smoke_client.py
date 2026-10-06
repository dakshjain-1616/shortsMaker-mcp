"""Connect to the local ShortsMaker MCP over Streamable HTTP and run safe read calls.

Usage:
    SHORTSMAKER_ACCESS_TOKEN='...' python scripts/mcp_smoke_client.py

This script intentionally calls only ``tools/list`` and read-only tools. It is a real MCP client
using the installed Python SDK, not a direct backend API client.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation


async def main() -> None:
    token = os.environ.get("SHORTSMAKER_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set SHORTSMAKER_ACCESS_TOKEN to a ShortsMaker access token first.")

    endpoint = os.environ.get("MCP_ENDPOINT", "http://127.0.0.1:8002/mcp/")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx2.AsyncClient(headers=headers, trust_env=False) as http_client:
        async with Client(
            streamable_http_client(endpoint, http_client=http_client),
            client_info=Implementation(name="shortsmaker-local-smoke-client", version="1.0"),
        ) as client:
            tools = await client.list_tools()
            print("Discovered tools:")
            for tool in tools.tools:
                print(f"- {tool.name}")

            credits = await client.call_tool("shortsmaker_get_credit_status", {})
            print("\nCredit-status result:")
            print(json.dumps(credits.model_dump(mode="json"), indent=2, default=str))

            price = await client.call_tool(
                "shortsmaker_resolve_video_price",
                {
                    "total_length": 15,
                    "mode": "lite",
                    "format": "transformation",
                    "resolution": "720p",
                },
            )
            print("\nPrice-preview result:")
            print(json.dumps(price.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
