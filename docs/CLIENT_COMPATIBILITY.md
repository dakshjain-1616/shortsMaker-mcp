# Client compatibility and authorization verification

Use the exact public `https://<mcp-host>/mcp/` URL. `/mcp` is also accepted without a redirect.
This service supports OAuth-protected Streamable HTTP with JSON responses. It does not serve
legacy HTTP/SSE endpoints or a standalone stdio entrypoint. Clients must support Streamable
HTTP plus public-client authorization code/PKCE; client-secret and private-key-JWT exchanges
are not implemented.

## Client matrix

| Client | Connection path | Implementation and evidence | Remaining acceptance |
|---|---|---|---|
| Claude.ai, Claude Desktop remote connectors | Public remote connector URL; CIMD or DCR | Public OAuth discovery, validated CIMD, PKCE, login/consent, client-specific callback. Real Cognito login, callback/code exchange, refresh, reads, and metadata writes observed with the deployed MCP and local backend. | Repeat acceptance after deploying these new changes; test Google only if enabled. |
| ChatGPT custom MCP/plugin | OAuth connection; public CIMD or DCR | Per-tool `securitySchemes` in the wire catalogue and `_meta` mirror, plus `mcp/www_authenticate` scope-upgrade challenges. HTTP missing/invalid-token challenges remain enforced. | Complete linking, refresh, a read, and a confirmed write in an actual ChatGPT account. |
| Claude Code | HTTP remote server; browser OAuth | Same public-client OAuth and HTTP transport. | Actual CLI login/refresh and tools. |
| VS Code / GitHub Copilot | HTTP MCP server; DCR and browser OAuth | Native registration accepts its HTTPS/loopback callbacks and supported grant extensions. Native loopback IP callback ports may vary; host/path/query stay strict. Regression covers token exchange using the actual chosen callback. | Actual editor login, loopback or hosted return, and tools. |
| Cursor remote MCP | Remote URL with OAuth in supported versions | Standard HTTP/DCR/public PKCE path. | Actual installed client version and its registered callback/auth mode. |
| Standard MCP SDK clients | Streamable HTTP and bearer credential from OAuth | Real SDK-client and raw HTTP integration tests. Wire versions tested: 2025-03-26, 2025-06-18, and 2025-11-25. Requests can reach different instances. | Vendor-specific browser/account behavior. |

The matrix is a support target and evidence record, not a claim that every vendor UI/version
has been tested. SSE-only, stdio-only, or confidential-client-only configurations require an
appropriate client adapter or a separately scoped integration; changing security checks to
accept arbitrary callbacks is not a compatibility solution.

## Browser and backend flow

1. MCP returns a bearer challenge naming protected-resource metadata. That document names the
   backend issuer; backend discovery names authorization, token, registration, and JWKS routes.
2. The client registers or uses validated public client metadata and opens backend
   `/oauth/authorize`. It retains its PKCE verifier and its own `state`.
3. The backend authenticates with normal Cognito/Google services, resolves the active user in
   its configured Postgres, and asks for consent. No synthetic login is shipped.
4. Approval returns a visible HTML continue link plus best-effort automatic navigation to the
   saved, validated client callback with `code`, `state`, and `iss`. Manual navigation remains
   available if a webview blocks the script. Denial returns the OAuth error to the callback.
5. The client exchanges the code and verifier. The actual callback URI must match at exchange,
   including the native listener port selected during authorization. Code/refresh rotation is
   atomic; replay and revoked refresh credentials are rejected.
6. MCP validates the RSA JWT, then signs a separate 60-second backend assertion. The backend
   validates that assertion and independently checks scope, route allowlist, user status,
   and ownership. The OAuth access token is not a general Platform API credential.

All three deployed values must agree: `MCP_OAUTH_ISSUER`, exact `MCP_RESOURCE_URL`, and
`MCP_BRIDGE_SECRET`. `MCP_BACKEND_API_URL` must reach the intended backend. A bridge mismatch
fails at tool calls even after successful browser login. It is now reported as connector
backend authentication/configuration failure, not an expired user access token.

## Information exposed to clients

Tools expose named operations and typed arguments, not arbitrary HTTP paths, SQL, headers,
user identities to impersonate, or a general API proxy. Scope and confirmation checks execute
on the server. Persisted `error`/`last_error` diagnostics are replaced with safe public messages;
upstream error bodies are not returned verbatim. Internal provider `estimated_cost` is removed
from price quotes after computing the user-facing credit estimate. MCP discovery/protocol responses and credential-bearing OAuth pages/responses are marked
`Cache-Control: no-store`.

OAuth discovery URLs, approved provider authorization links, and user-authorized media links
are intentionally visible. Hiding those URLs would break linking or media access. Private
engine/database addresses, signing keys, bridge secrets, and provider credentials must not
be included in tool results. The backend's ordinary API still requires authentication; its
internal worker/admin routes are outside the MCP bridge allowlist.

## Acceptance and deployment

For each real client, verify linking, both sign-in methods that are enabled, denied consent,
automatic/manual callback return, a read, confirmed write, missing confirmation, read-only
write rejection, second-user ownership rejection, refresh, revocation, and reconnection.
Use an isolated staging or local test database, never production for acceptance.

All read tools and all confirmed write tool route mappings run in the automated MCP suite
against a mock backend. Joined actual backend/MCP verification covers OAuth, bridge, discovery,
SQL-verified metadata writes, ownership, scopes, and credential lifecycle. Actual generation
completion and social publishing need working workers, isolated storage, and test provider
accounts; a mock engine or a successful queue response is not proof of those external outcomes.

Local changes require an MCP redeployment to reach Vercel. Do not change private-key or bridge
values during ordinary redeploys. After configuration changes, confirm live discovery and
perform the acceptance flow rather than assuming old client caches will update.

## Official references

- [OpenAI tool authentication](https://developers.openai.com/plugins/build/auth)
- [OpenAI custom MCP servers](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [Claude remote connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [VS Code MCP guide](https://code.visualstudio.com/api/extension-guides/ai/mcp)
- [VS Code OAuth registration implementation](https://github.com/microsoft/vscode/blob/main/src/vs/base/common/oauth.ts)
- [Cursor MCP](https://cursor.com/docs/mcp)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Native OAuth loopback redirects, RFC 8252](https://www.rfc-editor.org/rfc/rfc8252)

## Verification on 2026-10-07

The full backend suite passed 430 tests against disposable local Postgres. The full MCP suite
passed 155 tests, including all tool route mappings, confirmation checks, user/scope isolation,
three protocol versions, explicit wire auth declarations, upgrade challenges, and diagnostic
redaction. The joined actual loopback backend/MCP test passed native callback selection, code
exchange/replay, catalogue discovery, read/write scope enforcement, a SQL-verified owned
metadata edit, foreign-owner rejection, and refresh/revocation. Lint, compilation, and
whitespace checks passed. All temporary test processes/files/rows were removed.

Existing real-session backend logs also show the user's Cognito sign-in, consent, token
exchange/refresh, and deployed-MCP read/metadata-write requests succeeding with local data.
This observation is for Claude's published client identity. Other vendor account/browser
acceptance remains pending. No production database was accessed and changes remain uncommitted.
