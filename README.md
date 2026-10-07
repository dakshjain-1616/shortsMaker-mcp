# ShortsMaker remote MCP

This service exposes ShortsMaker tools over Streamable HTTP. The backend is its OAuth authorization server. MCP clients connect with the public `/mcp/` URL, discover OAuth, and open the backend login and consent page. The MCP validates OAuth access tokens and signs a separate short-lived assertion for each Platform API request.

See the [Vercel deployment guide](docs/VERCEL.md) for the MCP, backend, and local Postgres testing setup.

## Deploy on Vercel

Push this directory as its own GitHub repository and import it into a dedicated Vercel test
project. Use the repository root (or `mcp` if using the parent repository), Python 3.12, and
Vercel's default Python installation. `app.py` exports the ASGI app; `pyproject.toml` declares
that entrypoint and `vercel.json` enables Fluid compute with a 120-second function limit.

Set the variables from `.env.vercel.example` in Vercel's environment settings, using the stable
test-project domain and the HTTPS ngrok origin for your local backend. Use the same issuer,
resource URL, and dedicated bridge secret on the backend. The backend must explicitly connect
to a local disposable Postgres database. Vercel does not need database credentials or the
backend's private OAuth signing key.

The transport is stateless in every environment. Initialization, tool listing, and tool calls
can reach different instances; each request must carry its own OAuth token. Authenticated GET
and DELETE return 405 rather than opening an idle SSE stream. Unauthenticated requests still
return the OAuth discovery challenge. The dashboard is disabled by default and cannot be
enabled on Vercel. See [deployment and acceptance steps](docs/VERCEL.md).

## Connect a client

Add the exact public HTTPS MCP URL, such as `https://mcp.example.com/mcp/`, to a client that supports remote Streamable HTTP MCP and OAuth. No bearer token or custom Authorization header belongs in the client configuration. The client should follow the 401 challenge, discover OAuth metadata, launch the backend login page, and exchange a PKCE authorization code.

`.mcp.json.example` is a URL-only Claude Code example. In Claude.ai or ChatGPT, add a custom remote MCP connector using the same URL. Verify each client version against staging before enabling production users.

Initial consent offers `mcp:read` and `mcp:write`. Read-only grants call only read tools. Every consequential write also requires `confirm=true` after the user approves the exact action. Billing changes remain on the ShortsMaker website.

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
uvicorn shortsmaker_mcp.main:app --app-dir src --host 127.0.0.1 --port 8002 --env-file .env
```

Set `MCP_BACKEND_API_URL` and `MCP_OAUTH_ISSUER` to the local backend origin, `MCP_RESOURCE_URL` to `http://127.0.0.1:8002/mcp/`, and `MCP_BRIDGE_SECRET` to the same dedicated key as the backend. The backend also needs its OAuth signing key, the Postgres OAuth state table, and existing authentication settings. Localhost HTTP is for development; public deployments require HTTPS.

`/health/live` is unauthenticated. `/mcp/` requires an OAuth bearer token and returns a 401 challenge with protected-resource metadata when one is missing. `/dashboard` is a private development aid: opt in locally with `MCP_DASHBOARD_ENABLED=true`. Use an OAuth-capable client for end-to-end login tests.

## Tools

Read tools: `shortsmaker_get_video_options`, `shortsmaker_resolve_video_price`, `shortsmaker_get_credit_status`, `shortsmaker_get_subscription`, `shortsmaker_list_subscription_plans`, `shortsmaker_get_account_features`, `shortsmaker_list_videos`, `shortsmaker_get_video`, `shortsmaker_list_connections`, `shortsmaker_list_posts`, `shortsmaker_publishing_health`, `shortsmaker_list_workflows`, `shortsmaker_get_workflow`, and `shortsmaker_list_workflow_slots`.

Write tools: `shortsmaker_create_video`, `shortsmaker_control_video`, `shortsmaker_update_video_metadata`, `shortsmaker_start_connection`, `shortsmaker_disconnect`, `shortsmaker_publish_video`, `shortsmaker_control_post`, `shortsmaker_create_workflow`, `shortsmaker_update_workflow`, `shortsmaker_pause_workflow`, `shortsmaker_resume_workflow`, `shortsmaker_stop_workflow`, `shortsmaker_delete_workflow`, `shortsmaker_update_workflow_slot`, and `shortsmaker_control_workflow_slot`.

The server also exposes guided workflow prompts. Video listing is paginated using `total` and `next_offset`. The price tool estimates credits; video creation reports the exact charged amount. Workflow edits can replan future slots, and retry operations may start generation work; inspect current state before confirming a write.

## Configuration

| Variable | Purpose |
|---|---|
| `MCP_BACKEND_API_URL` | Platform API base URL. |
| `MCP_OAUTH_ISSUER` | Backend OAuth issuer origin. |
| `MCP_RESOURCE_URL` | Exact public `/mcp/` URL, including trailing slash. |
| `MCP_BRIDGE_SECRET` | Dedicated key shared with the backend for API assertions. |
| `MCP_PUBLIC_URL` | Public origin for Host and Origin validation. |
| `MCP_HOST`, `MCP_PORT` | Listener address and port. |
| `MCP_DASHBOARD_ENABLED` | Private local dashboard toggle; set false publicly. |
| `MCP_RATE_LIMIT_REQUESTS`, `MCP_RATE_LIMIT_WINDOW_SECONDS` | Per-process request limiter. |
| `FORWARDED_ALLOW_IPS` | Reverse proxy IPs trusted by uvicorn. |
| `MCP_CREDITS_PER_USD` | Must match backend `CREDITS_PER_DOLLAR`. |

Protocol requests do not require session affinity. Preserve Authorization, Host, Origin, and
MCP protocol headers through the proxy. The in-process rate limiter applies separately to each
instance; use a gateway limiter if a shared deployment-wide limit is needed.

## Verification

Run `ruff check src tests app.py`, `pytest -q`, and `python -m compileall -q src tests app.py`. Automated MCP tests use a mock backend and do not spend credits or publish content. They include separate-process tests that initialize on one instance and list/call tools on another, and verify identity/scope isolation across requests. Verify login, refresh, a read, and an approved write with real OAuth-capable clients on isolated test accounts. See the [acceptance checklist](docs/VERCEL.md#acceptance).

The legacy `scripts/mcp_smoke_client.py` accepts an already-issued MCP OAuth access token through `SHORTSMAKER_ACCESS_TOKEN` for protocol checks; it does not perform login itself. Obtain a token through an OAuth-capable client. A regular ShortsMaker website token cannot be used for MCP.

Client setup, OAuth redirection, public/private response boundaries, and the remaining real-client
acceptance checks are recorded in [Client compatibility](docs/CLIENT_COMPATIBILITY.md).
