# Vercel testing deployment

The MCP runs as a Python ASGI function. OAuth and Platform API operations run on the separate
ShortsMaker backend; Postgres is accessed only by that backend. For this test deployment, ngrok
exposes the local backend while its database remains a disposable local Postgres database.

```text
ChatGPT --tool calls--> Vercel MCP --JWKS/API--> ngrok HTTPS --> local backend --> local Postgres
ChatGPT --login and token exchange-----------> ngrok HTTPS --> local backend
```

## Deployment files

- `app.py` exports the installed `shortsmaker_mcp.main.app` at the repository root. Vercel
  resolves the entrypoint before installing the package, so the root wrapper supports the
  existing `src/` layout without changing package imports.
- `pyproject.toml` declares `app:app` under `tool.vercel`. Vercel's default Python installation
  installs the package and its pinned dependencies. No custom build or start command is needed.
- `.python-version` selects Python 3.12.
- `vercel.json` enables Fluid compute and a 120-second function limit. Backend requests retain
  their own 45-second timeout; Vercel does not run video generation or publishing workers.
- `.vercelignore` excludes local environments, caches, tests, scripts, and key files from upload.
  `.gitignore` keeps local secrets out of the GitHub repository.

The SDK transport uses `stateless_http=True` and JSON responses. Each POST is authenticated
independently; there is no session ID or need for affinity between instances. This is also the
local/Docker transport, so tests exercise the same behavior. The mounted SDK app still needs
its session manager's startup lifecycle; the parent Starlette lifespan owns that manager and
closes the HTTP clients on shutdown. Authenticated GET/DELETE receive 405 with `Allow: POST`;
the service does not keep an idle SSE stream open. Unauthenticated requests receive the 401
OAuth challenge before the method check.

## Set up the local backend

1. Use an explicit `DATABASE_URL` pointing to a dedicated local `_test` database. Apply the
   backend's schema, including `supabase/schema/18_mcp_oauth_state.sql`, only to that database.
   Keep `RUN_PUBLISHER_IN_API=false` and `RUN_SCHEDULER_IN_API=false` for authentication tests.
2. Start ngrok forwarding to the backend's HTTP listener, for example `ngrok http 8001`. Use
   the HTTPS origin it supplies, without a path, as the backend's `MCP_OAUTH_ISSUER` and
   `API_BASE_URL`, so returned API links also point to the local test backend.
3. Set the backend's `MCP_RESOURCE_URL` to the stable Vercel test-project URL including `/mcp/`.
   Configure its RSA private signing key, `MCP_OAUTH_KID`, and a dedicated random bridge secret
   of at least 32 characters. Keep the signing key and bridge secret stable across restarts.
4. Use disposable test accounts and the backend's existing sign-in configuration. Google login
   needs the ngrok origin registered for that Google client. A real login must resolve an active
   user in the local Postgres database. The automated tests' fixture login is not a public login.

Keep the local backend, Postgres, and ngrok running for the entire test. Expose only the backend
HTTP port. The MCP never needs a tunnel to Postgres. If the ngrok origin changes, update the
backend and Vercel issuer/API settings, restart/redeploy, and reconnect the OAuth client.

## Import the GitHub repository

1. Push the contents of this `mcp` directory as a standalone repository, including `src`,
   `app.py`, dependency/configuration files, and `docs`. If importing the parent repository,
   set Vercel's Root Directory to `mcp`; for a standalone repository use the repository root.
2. Create a separate Vercel test project. Keep Python framework detection and default build and
   installation settings; do not select Next.js or configure a static output directory.
3. Use the project's stable domain, such as `https://shortsmaker-mcp-test.vercel.app`. Add the
   variables below before deployment. `.env.vercel.example` contains placeholders only.
4. Ensure deployment protection permits external access to this test project's discovery and
   MCP endpoints. ChatGPT must reach OAuth metadata without a separate Vercel sign-in wall.
5. Deploy and test the stable domain. Branch-preview domains are not automatically trusted:
   the token audience, advertised resource, and Host/Origin allowlist use the configured domain.

| Vercel variable | Example / requirement |
|---|---|
| `MCP_BACKEND_API_URL` | `https://your-backend.ngrok-free.app` |
| `MCP_OAUTH_ISSUER` | Same backend HTTPS origin; must match the backend issuer exactly. |
| `MCP_RESOURCE_URL` | `https://shortsmaker-mcp-test.vercel.app/mcp/`; must match the backend value exactly. |
| `MCP_PUBLIC_URL` | `https://shortsmaker-mcp-test.vercel.app`; optional, but if set must match the resource origin. |
| `MCP_BRIDGE_SECRET` | Same dedicated 32+ character secret as the backend. |
| `MCP_DASHBOARD_ENABLED` | `false` |
| `MCP_API_TIMEOUT_SECONDS` | `45` |
| `MCP_API_CONNECT_TIMEOUT_SECONDS` | `10` |
| `MCP_CREDITS_PER_USD` | `100`, or the backend's configured `CREDITS_PER_DOLLAR`. |

On Vercel, startup rejects missing OAuth settings, non-HTTPS/local URLs, an incorrect resource
path, mismatched public origin, a short bridge secret, or an enabled dashboard. Enter settings
for the environment used by the stable test-project domain. Vercel's environment name does not
change the backend database: that backend must remain connected to local test Postgres.

Do not set `DATABASE_URL`, the RSA private key, or backend provider credentials on the MCP.
The request limiter is per instance; it is not a shared global limit across Vercel replicas.

## Acceptance

Check these public endpoints after deployment:

```bash
curl -i https://shortsmaker-mcp-test.vercel.app/health/live
curl -i https://shortsmaker-mcp-test.vercel.app/.well-known/oauth-protected-resource/mcp
curl -i https://shortsmaker-mcp-test.vercel.app/mcp/
```

Expect health 200, metadata 200 with the exact resource/issuer, and MCP 401 with a
`WWW-Authenticate` header pointing to resource metadata. Also check backend OAuth discovery
and `/oauth/jwks` through ngrok. The free ngrok browser warning may require clicking Visit;
ngrok says programmatic API requests are unaffected.

Then add the exact `/mcp/` URL to an OAuth-capable ChatGPT connector. Complete login, consent,
and a read tool. Verify refresh and perform a confirmed metadata edit on disposable local data.
Check read-only grants cannot write, tokens for another audience are rejected, and the dashboard
returns 404. Real generation/publishing requires separate test engine/provider configuration.

Local regression tests and packaging checks do not verify the actual Vercel build, public
proxy behavior, ngrok reachability, or ChatGPT linking. Complete those checks after deployment
before calling the public connection verified. Production databases must not be used for tests.

Local verification on 2026-10-06: 141 MCP tests passed, including separate-process request
routing and identity/scope isolation. Lint and compilation passed. A clean `uv sync --no-dev
--no-editable` installation passed entrypoint, lifecycle, metadata, token, tool, and bridge
checks. The joined actual backend/MCP HTTP flow passed again with local Postgres and fixture
sign-in, including a confirmed local metadata write, refresh, revocation, and rejection cases.

References: [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python),
[Vercel function limits](https://vercel.com/docs/functions/limitations),
[ngrok free-plan behavior](https://ngrok.com/docs/pricing-limits/free-plan-limits).
