# MCP Surface

canopy-web exposes its whole REST API as a
[FastMCP](https://github.com/jlowin/fastmcp) 4.x server, mounted at
`/api/mcp/` over **Streamable HTTP** (wired in `config/asgi.py`). External MCP
clients (Claude Code, other agents) call these tools to inspect and mutate
canopy-web state. This doc covers the module layout, auth, the generated tools
and the few hand-written ones.

**Every REST route is a tool.** `apps/mcp/api_tools.py` generates one tool per
route of the single `NinjaAPI` from its OpenAPI schema (FastMCP's
`OpenAPIProvider` over `api.get_openapi_schema()`) — name = operationId,
parameters = the route's path/query/body, description = its docstring — and a
call is a request to that route, dispatched **in-process** (Django's ASGI
handler is the httpx transport; no socket, no loopback URL) **as the caller**
(the principal rides in the ASGI scope under `canopy.mcp_principal`, which
`BearerTokenAuthMiddleware` resolves to `request.user` and which no network
request can set, because only that transport builds the scope). The caller's
acting cap holds too (`apps/workspaces/role_cap.py`): the MCP request's
`X-Canopy-Acts-As` and `X-Canopy-Turn-Id` are forwarded (`FORWARDED_HEADERS`)
and a caller token's own turn is read from its claims, so a turn started as an
editor or viewer cannot act at its full role through MCP. The web app
calls the same API, so anything it can do an MCP client can do — same
middleware, tenancy, ACL and RFC 7807 errors — and **a new route is on MCP the
moment it exists; there is no step to forget.** Don't add a declarative
"expose this" marker or a loopback/shared-bearer dispatcher: generation is the
mechanism, and a loopback would run every tool as one identity.

**Exposure is the default.** Leaving a route out is never how you secure it: a
tool is exactly as powerful as the caller's token already is against REST, so
the refusal belongs in the route. There are **no web-app-only actions** —
invites, roles, an agent's interface, credentials and admins, vaults and Slack
config all work over MCP under the caller's role. Don't add browser-only gates;
they take from MCP the administration it is most useful for. Two deliberate
exceptions: `create_token` is not a tool (it would put a fresh raw PAT into a
chat transcript — mint tokens in the web app or with
`/canopy:canopy-web-pat-mint`), and a browser action that reveals or changes a
secret needs a passkey step-up a token can never hold (`apps/passkeys/stepup.py`;
each such route says in `api-surface.md` whether a token is refused or let
through). The OAuth consent page itself still needs a person, or a token could
sign itself in.

**No agent tools.** This server is canopy the PRODUCT — board, sessions, turns,
runners, schedules, mail, every REST route, page actions. It does not offer
per-agent tools that run an agent remotely (no `<agent>__<capability>`, no
`agent_reply`): a call like that would run as a turn on the agent OWNER's machine,
under their credentials. To use an agent, install its plugin from your agent
catalog (`/plugins/marketplace.json`, marketplace `canopy-agents`; see
`plugin-marketplace.md`) into your own Claude Code and run it
locally as yourself — see `/get-started`. A call to an `<agent>__…` tool gets
MCP's unknown-tool error. An agent's declared interface still confines
the callers who DO reach it through canopy (web chat, Slack, email, embedded
sites).

A handful of **hand-written** tools cover what is not a REST route
(caller envelope, site gateway, page tools, /ide close review). A plain write is
never one of them: it is a route, so it gets the route gate, the settings audit
and the acting cap (`share_session_to_slack`, `audit_session_noise` and
`purge_session_noise` are routes under `/api/canopy-sessions/`, keeping their
tool names). The implementation lives in `apps/mcp/`.

## Module layout (`apps/mcp/`)

| File | Responsibility |
|---|---|
| `auth.py` | `CanopyPATVerifier` — the server's one FastMCP `TokenVerifier`: resolves a bearer (PAT, OAuth access token, caller token, or a DPoP-proved host-grant token) to a Django user through `apps.tokens.principal.resolve_bearer`, the one resolver REST and the WebSocket handshake use too. |
| `server.py` | The `FastMCP("canopy-web", auth=CanopyPATVerifier(...))` instance, its middleware (`TurnScopeMiddleware`, `DelegatedScopeMiddleware`) and providers (page actions, generated API tools), and `build_http_app()` for the ASGI mount. Importing `apps.mcp.tools` registers the static tools. |
| `api_tools.py` | The generated API tools: `CanopyAPIProvider` (one tool per REST route), the in-process transport, `EXCLUDED` / `EXCLUDED_PREFIXES`. |
| `tools/` | Hand-written static tools: `caller.py`, `site.py`, `page.py`, `close_review.py` — each declares its gate in `apps/api/route_gates.py::HAND_TOOL_GATES` (`tests/test_every_route_declares_its_gate.py` fails one that does not). |
| `page_tools.py` | `PageActionProvider` — each open page's declared actions as per-caller tools. |
| `turn_scope.py` | `TurnScopeMiddleware` — a caller token sees only its capability's canopy tools. |
| `chat_scope.py` | `current_chat_session()` — the chat an `X-Canopy-Chat-Key` header names, which narrows page tools to that one chat. |
| `delegation.py` | canopy-web as a host of its own MCP: `gate()` (the SDK's `DPoPGate`, mounted in `config/asgi.py`), `access_token_for()` (a host-grant token → the visitor's `AccessToken`), and `DelegatedScopeMiddleware` (only the grant's scopes' tools). |
| `rate_limit.py` | Per-user write rate limit (mutating tools). |
| `audit.py` | `current_user_id()` + `write_audit()` (writes `MCPAuditLog`). |
| `models.py` | `MCPAuditLog` — one row per tool call. |

## Auth model

```python
mcp = FastMCP("canopy-web", auth=CanopyPATVerifier(resource_base_url=f"{base}/api/mcp"))
```

One verifier resolves every credential to a canopy `PersonalToken`-backed (or
caller/host-grant) principal; there is no shared server bearer. Tools read the
caller via `get_access_token()`.

* **PAT.** A per-user Personal Access Token
  (`apps.tokens`) presented as `Authorization: Bearer <raw>`.
  `CanopyPATVerifier.verify_token()` resolves it with
  `apps.tokens.principal.resolve_bearer` (PATs only), which stamps
  `last_used_at` and `last_used_client` exactly as REST does, and returns a FastMCP `AccessToken` whose claims
  carry the user (`sub`/`user_id`/`email`). Tools read the user via
  `get_access_token()`. A miss returns `None` → 401.

* **OAuth sign-in (what MCP clients do by default).** Adding
  `https://<canopy-web>/api/mcp/` to Claude Code, Claude Desktop or any MCP
  client signs you in with no token to copy. The 401 carries
  `WWW-Authenticate: Bearer resource_metadata="…"` (the verifier is built with
  `resource_base_url`), the RFC 9728 document names canopy's issuer, whose
  RFC 8414 document offers `authorization_code` + PKCE (S256) and
  `refresh_token` beside the host grant's jwt-bearer. The client registers
  itself (`POST /oauth/register`, RFC 7591 — public clients only; registering
  grants nothing), sends your browser to `/oauth/authorize` (Google sign-in if
  needed, then a consent page naming the client; a session minted from a token
  cannot approve), and trades the code at `/oauth/token`. **The access token is
  an ordinary `PersonalToken` that lives an hour**, tied to an `OAuthGrant`, so
  every tool and route treats it exactly like a hand-minted PAT. Refresh tokens
  last 30 days and rotate on every use; a retired one presented again revokes
  the grant, as does replaying a code. Settings → **Connected apps** lists the
  grants (`GET /api/tokens/connected-apps`) and disconnects them at once. Code:
  `apps/oauth_server/mcp_oauth.py` (the authorization server; the protocol rules it
  shares with the HCP service — PKCE, redirect_uri at the token endpoint,
  one-time redemption with replay → revoke — are `apps/common/oauth_core.py`),
  `views_mcp_oauth.py` (register + consent), `views_oauth.token` (one token
  endpoint for both grants). On labs the RFC-located documents sit at the ROOT
  of the shared host (`/.well-known/…/canopy…`), which the ALB routes to canopy.

* **Host-grant token (canopy-web as a host of its own MCP).**
  An ADDITIONAL credential beside PATs and caller tokens. When the
  widget on one of canopy's own registered pages mints, canopy issues itself
  an ID-JAG for that page's read-only scopes and redeems it at its own
  `/oauth/token` (`apps/tokens/self_host.py`, the SDK's `GrantHandler`). The
  result is a DPoP-bound access token (`canopy_host_delegated_token`, the
  SDK's table) that canopy's gateway (`site_call` for site `canopy-web`)
  presents here as `Authorization: DPoP <token>` with a fresh proof. The
  SDK's `DPoPGate` in front of the MCP app (`apps/mcp/delegation.py::gate`)
  verifies the proof (method, the public MCP URL, `ath`, freshness,
  single-use `jti`) and hands FastMCP a plain bearer plus the proving key's
  thumbprint; `CanopyPATVerifier` then resolves it with the SDK's
  `ResourceVerifier` ONLY when a key was proved (a bound token sent as a
  plain bearer is a 401). Tools run AS the visitor (`user_id` = their canopy
  user, `sub` = `delegated:<id>`, `auth_method: delegated`) — their own ACL —
  and `DelegatedScopeMiddleware` lists and allows only the tools their scopes
  map to (`self_host.SCOPE_TOOLS`; the page → scope → tool table is in
  [embedding-a-canopy-agent.md §11](embedding-a-canopy-agent.md#canopy-web-as-a-host-of-its-own-mcp-agents-act-as-you-on-canopys-pages));
  prompts are closed to it, and resources too except the MCP Apps View of a
  tool in its grant (`delegation._view_allowed`). So a member's delegated token
  reaches at most their own ACL ∩ its page's scopes — read-only except
  `self_host.WRITABLE_SCOPES` (`sessions:close`, whose one write is app-only).
  A DPoP request to a canopy that is not configured as a host is a 401.

Auth is enforced inside the MCP app by the verifier; `config/asgi.py` adds only
the DPoP gate (below). Don't reintroduce a shared `CANOPY_MCP_BEARER`-style
server token — every tool must run as a person.

## Generated API tools (`api_tools.py`)

* **Coverage.** Every route, minus `EXCLUDED` / `EXCLUDED_PREFIXES`, each with
  its reason: routes whose caller is not a person — the runner protocol
  (heartbeat, claim, streams, turn lifecycle…), a browser tab's plumbing (page
  state, attach/detach, push subscription, MCP Apps View routes), an embedding
  host's or a contact's surface (`/api/contact/`, `/api/embed/`), A2A
  discovery, anonymous public reads, raw-bytes responses, multipart uploads;
  `create_token` (a tool would put a fresh raw PAT into a chat transcript; the
  OAuth consent page likewise still needs a person); and the person's own
  agent-memory switches and HCP grant/audit/client-registry routes (an agent
  must never be able to turn those for itself). `tests/test_mcp_api_tools.py`
  fails on an exclusion naming a route that no longer exists.
* **Names.** The operationId, which `CanopyNinjaAPI` makes the view function's
  own name, qualified by app only when two modules share it
  (`canopy_sessions_send`, `session_sharing_list_sessions`).
* **Tenant.** Every tool takes an optional `workspace`; the call goes to
  `/api/w/{workspace}/…` (membership checked by `WorkspaceResolveMiddleware`).
  Omitted, the call goes to the flat route exactly as a PAT caller's would —
  scoped by the read rule in [roles.md § A read that names no workspace stays
  inside one organisation](roles.md#a-read-that-names-no-workspace-stays-inside-one-organisation). A route that already names
  `{workspace}` in its path keeps its own argument.
* **Identity.** The caller from the MCP access token (`user_id` +
  `auth_method`); a token with no canopy user is refused. The request counts as
  a machine (`is_machine`). Caller tokens and host-grant tokens are still
  confined by `TurnScopeMiddleware` / `DelegatedScopeMiddleware`, which list
  only the tools they name.
* **Audit + rate limit.** Every call writes `MCPAuditLog`; every non-GET counts
  against the per-user write limit.
* **Errors.** A non-2xx is a `ToolError` carrying the status and the RFC 7807
  body the REST route returned.
* **Collisions.** None allowed. FastMCP resolves hand-written tools ahead of
  providers, so a route sharing a hand-written tool's name would be silently
  unreachable; `tests/test_mcp_api_tools.py` fails on one. A hand-written tool
  that duplicates a route is deleted, not kept beside it — the route keeps the
  name. Tool names the host-grant scopes and page contract use
  (`list_fleet_tasks`, `skill_history`, `skill_revision_diff`) are therefore
  routes (`/api/agents/{slug}/skill-history/{revisions,diff}`), not MCP-only.
* **Not tools by design.** The MCP Apps View routes
  (`/api/canopy-sessions/{id}/apps/{tool_call_id}/…`, and the contact twins
  under `/api/contact/`) refuse a PAT, so a View's commit token never reaches a
  model — see `docs/architecture/embedding-internals.md`.

## Hand-written tools

| Tool | Kind | Purpose |
|---|---|---|
| *(caller tokens)* | auth | A CONFINED turn's session reaches this server with a `cct_…` caller token, never the runner owner's PAT: tools run as the conversation's current asker and `TurnScopeMiddleware` lists only the tools its capability names. A **scoped** caller token is the full-profile variant for a colleague's full turn — every tool, each run as the asker. Minting, lifetime and the rules: [access.md § How confinement is enforced](access.md#how-confinement-is-enforced). |
| `who_is_asking` | read | The caller envelope for a turn (`apps/harness/caller_context.py`): who asked, `verified` (about THIS message — a spoof of a once-verified address is not verified), `relationship` to the agent (owner/admin/member/contact/system; `caller` is read as `contact`), and the workspace's contact profile (`notes`, `attributes`, this-message vs best grade, `is_blocked`). The same document the claiming runner writes to `~/.canopy/caller/<turn_id>.json`; this is the mid-turn re-read, so a contact blocked or re-annotated since the claim shows as such. Gated like the REST twin `GET /api/harness/turns/{id}/caller-context`: tenant membership, unknown ⇒ "turn not found". |
| `site_tools` | read, **caller-token only** | The tools of the Connected site the visitor is on that this turn may use (host grant contract v1): what the host lists AS THE VISITOR for their grant, narrowed by the capability's `ceiling` only when the owner set one (the page's `backing_tool` is a hint to the agent, not a filter). The turn is read from the caller token, never an argument. Refused — never a fallback to the agent's own credential — when the visitor holds no unexpired grant ("I need you back on the page"). |
| `site_call` | write, **caller-token only** | Call one host tool as the visitor, through canopy (`apps/tokens/host_gateway.py`): canopy attaches the visitor's host-issued token as `Authorization: DPoP` with a fresh proof per request (`htm`/`htu`/`ath`/`iat`/`jti`) and `Canopy-Actor: <agent>`. Re-checks site, capability `sites:`, grant freshness, resource, DPoP key and the effective tool set on every call. Audited per call; the token never appears in a result, error, log or audit row, and never reaches a runner. Added to a confined profile automatically for any capability with `sites:`. |
| `current_page` | read | The page(s) the caller is looking at (`apps/mcp/tools/page.py`), narrowed to one chat when the request carries an `X-Canopy-Chat-Key`. Its siblings `read_page_state` / `list_page_actions` / `invoke_page_action` are generated routes. |
| `page_<action>` | write, **dynamic** | Each open page's declared actions (`registerAction` in the host page) as tools, computed per caller by `PageActionProvider` (`apps/mcp/page_tools.py`); executed by the browser tab through `apps/canopy_sessions/page_actions.py`. Static tools take precedence, so a page cannot capture a canopy tool. |
| `propose_session_closes` / `close_proposed_sessions` | read / write (app-only, rate-limited) | /ide close review (`apps/mcp/tools/close_review.py`, rule in `apps/canopy_sessions/close_review.py`). The agent proposes `[{session_id, quote, why}]`; the result carries the MCP Apps View `ui://canopy/ide/close-review`, which re-reads the rows as the viewer and whose Close button calls `close_proposed_sessions` — app-only, and refused to every credential but a person's host grant, so only a click closes. Closing is `control.close_session`, per session, after re-checking it is not running or waiting. |

## Mount + lifespan (`config/asgi.py`)

Streamable HTTP requires the MCP app's lifespan to run (session
management). The Django bare ASGI app has no lifespan, so the app is a
Starlette router that mounts the MCP app under `/api/mcp` and the Django
app at `/`, with a lifespan that runs the MCP app's (plus the capability sweep,
Slack ingest and canopy-pat rotation schedulers) for the life of the process:

```python
mcp_app = build_http_app()  # mcp.http_app(path="/", transport="streamable-http")
application = Starlette(
    middleware=[Middleware(_McpWithoutSlash)],  # bare /api/mcp → /api/mcp/
    routes=[Mount("/api/mcp", app=dpop_gate(mcp_app)), Mount("/", app=django_with_ws)],
    lifespan=_lifespan,  # async with mcp_app.lifespan(app): … schedulers …
)
```

`dpop_gate` (`apps/mcp/delegation.py::gate`) is the SDK's `DPoPGate`; it acts
only on `Authorization: DPoP` and passes every other request through untouched.

## How to connect Claude Code

```bash
claude mcp add --transport http canopy https://<canopy-web>/api/mcp/
```

Claude opens canopy's sign-in in your browser the first time; approve it and
you are connected (Settings → Connected apps shows it). A personal access token
still works for scripts and headless clients:

```json
{
  "canopy-web": {
    "url": "https://<canopy-web>/api/mcp/",
    "headers": { "Authorization": "Bearer cpat_<your-token>" }
  }
}
```

Mint a PAT on `/settings`, with `/canopy:canopy-web-pat-mint`, or
`manage.py create_token --email <you> --label <name>`.
