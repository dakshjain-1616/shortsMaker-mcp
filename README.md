# Faceless Video Maker remote MCP

This service exposes Faceless Video Maker tools over Streamable HTTP. The backend is its OAuth authorization server. MCP clients connect with the public `/mcp/` URL, discover OAuth, and open the backend login and consent page. The MCP validates OAuth access tokens and signs a separate short-lived assertion for each Platform API request.

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

Add the public HTTPS MCP URL, such as `https://mcp.example.com/mcp/`, to a client that supports remote Streamable HTTP MCP and OAuth. `/mcp` also works for HTTP requests and OAuth resource parameters; discovery and tokens use `/mcp/` as the canonical resource. No bearer token or custom Authorization header belongs in the client configuration. The client should follow the 401 challenge, discover OAuth metadata, open the configured real frontend auth page, and exchange a PKCE authorization code after frontend consent.

`.mcp.json.example` is a URL-only Claude Code example. In Claude.ai or ChatGPT, add a custom remote MCP connector using the same URL. Verify each client version against staging before enabling production users.

Initial consent offers `mcp:read` and `mcp:write`. Read-only grants call only read tools. Every consequential write also requires `confirm=true` after the user approves the exact action. Billing changes remain on the website (https://facelessvideomaker.ai).

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
uvicorn shortsmaker_mcp.main:app --app-dir src --host 127.0.0.1 --port 8002 --env-file .env
```

Set `MCP_BACKEND_API_URL` to the local backend origin, `MCP_RESOURCE_URL` to `http://127.0.0.1:8002/mcp/`, and `MCP_BRIDGE_SECRET` to the same dedicated key as the backend. The OAuth issuer defaults to the backend URL. The backend also needs its OAuth signing key, the Postgres OAuth state table, and existing authentication settings. Localhost HTTP is for development; public deployments require HTTPS.

`/health/live` is unauthenticated. `/mcp/` requires an OAuth bearer token and returns a 401 challenge with protected-resource metadata when one is missing. `/dashboard` is a private development aid: opt in locally with `MCP_DASHBOARD_ENABLED=true`. Use an OAuth-capable client for end-to-end login tests.

## Tools

Read tools: `get_video_options`, `get_video_price`, `get_credits`, `get_subscription`, `list_plans`, `get_account_features`, `list_videos`, `get_video`, `list_connections`, `list_posts`, `check_publishing_health`, `list_series`, `get_series`, and `list_series_slots`.

Write tools: `create_video`, `control_video`, `update_video_details`, `connect_account`, `disconnect_account`, `publish_video`, `control_post`, `create_series`, `update_series`, `pause_series`, `resume_series`, `stop_series`, `delete_series`, `update_series_slot`, and `control_series_slot`.

Prompts (slash commands in clients that show them): `new_video`, `new_series`, `share_video`, and `review_series`. The first two walk through the choices one question at a time; run them with no arguments to start from scratch.

The price tool estimates credits; video creation reports the exact charged amount. Series edits can replan future slots, and retry operations may start generation work; inspect current state before confirming a write. The backend API still calls a series a "workflow" (`/api/workflows`).

## User experience

- **Lists show working items first.** `list_videos` puts videos still being made first, then the newest finished ones, and hides failed and cancelled videos unless `show` asks for them; the `hidden` counts tell the assistant they exist. `list_series` puts running series first, then paused ones, and counts stopped or ended series in `stopped_count`. Both follow the web app's dashboard rules.
- **Gallery.** `list_videos`, `get_video`, and `list_series` link to `ui://faceless-video-maker/gallery.html`, an MCP App that clients with UI support (Claude.ai, Claude Desktop, ChatGPT, Cursor, VS Code) render as thumbnails, players, and series cards. Other clients, such as Claude Code, use the same structured result as text. Results carry `shown_as_gallery` so the assistant summarizes instead of repeating what is on screen. The page lives in `src/shortsmaker_mcp/static/gallery.html` and loads media only from `MCP_MEDIA_ORIGINS`.
- **Guided choices.** The server instructions tell the assistant to ask one question at a time with a few options and a recommended default, using the client's own question UI where it has one. MCP elicitation is not used: Claude.ai and the stateless JSON transport do not support it.
- **Branding.** The server reports the title "Faceless Video Maker", https://facelessvideomaker.ai, and icons served by this service at `/icon.svg`, `/icon.png`, and `/favicon.ico`. Claude.ai picks a connector icon from the favicon of the MCP host's domain, so host the MCP on the brand domain (for example `mcp.facelessvideomaker.ai`).

## Configuration

| Variable | Purpose |
|---|---|
| `MCP_BACKEND_API_URL` | Platform API base URL. |
| `MCP_MEDIA_ORIGINS` | Comma-separated https origins the gallery may load thumbnails and videos from. Defaults to `https://*.amazonaws.com`. |
| `MCP_OAUTH_ISSUER` | Optional backend OAuth issuer override; defaults to `MCP_BACKEND_API_URL`. |
| `MCP_RESOURCE_URL` | Exact public `/mcp/` URL, including trailing slash. |
| `MCP_BRIDGE_SECRET` | Dedicated key shared with the backend for API assertions. |
| `MCP_HOST`, `MCP_PORT` | Listener address and port. |
| `MCP_DASHBOARD_ENABLED` | Private local dashboard toggle; set false publicly. |
| `MCP_RATE_LIMIT_REQUESTS`, `MCP_RATE_LIMIT_WINDOW_SECONDS` | Per-process request limiter. |
| `FORWARDED_ALLOW_IPS` | Reverse proxy IPs trusted by uvicorn. |

The transport origin is derived from `MCP_RESOURCE_URL`; no second public URL setting is needed.
Protocol requests do not require session affinity. Preserve Authorization, Host, Origin, and
MCP protocol headers through the proxy. The in-process rate limiter applies separately to each
instance; use a gateway limiter if a shared deployment-wide limit is needed.

## Verification

Run `ruff check src tests app.py`, `pytest -q`, and `python -m compileall -q src tests app.py`. Automated MCP tests use a mock backend and do not spend credits or publish content. They include separate-process tests that initialize on one instance and list/call tools on another, and verify identity/scope isolation across requests. Verify login, refresh, a read, and an approved write with real OAuth-capable clients on isolated test accounts. See the [acceptance checklist](docs/VERCEL.md#acceptance).

The legacy `scripts/mcp_smoke_client.py` accepts an already-issued MCP OAuth access token through `SHORTSMAKER_ACCESS_TOKEN` for protocol checks; it does not perform login itself. Obtain a token through an OAuth-capable client. A regular website sign-in token cannot be used for MCP.

Client setup, OAuth redirection, public/private response boundaries, and the remaining real-client
acceptance checks are recorded in [Client compatibility](docs/CLIENT_COMPATIBILITY.md).
