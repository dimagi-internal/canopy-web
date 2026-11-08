# MCP Surface

canopy-web serves its whole REST API as a [FastMCP](https://github.com/jlowin/fastmcp)
server over Streamable HTTP at `/api/mcp/` (mount and lifespan: `config/asgi.py`).
External MCP clients (Claude Code, other agents) inspect and change canopy-web
through it. Start reading at `apps/mcp/server.py` (instance, middleware, providers),
`apps/mcp/api_tools.py` (generated tools) and `apps/mcp/tools/` (hand-written ones).

**Every REST route is a tool.** `apps/mcp/api_tools.py::CanopyAPIProvider` generates
one tool per route of the single `NinjaAPI` from its OpenAPI schema (name =
operationId, description = the route docstring), and a call is a request to that
route, dispatched **in-process as the caller**: the principal rides in the ASGI
scope under `MCP_PRINCIPAL_SCOPE_KEY`, which `BearerTokenAuthMiddleware` resolves
to `request.user` and which no network request can set. The caller's acting cap
holds too (`apps/workspaces/role_cap.py`): `FORWARDED_HEADERS` carries
`X-Canopy-Acts-As` and `X-Canopy-Turn-Id`, so a turn started as an editor or
viewer cannot act at its full role through MCP. Same middleware, tenancy, ACL and
RFC 7807 errors as the web app, and **a new route is on MCP the moment it exists;
there is no step to forget.** Do not add a declarative "expose this" marker or a
loopback/shared-bearer dispatcher: a loopback would run every tool as one identity.

**Exposure is the default.** Leaving a route out is never how you secure it: a tool
is exactly as powerful as the caller's token already is against REST, so the refusal
belongs in the route. There are **no web-app-only actions** (invites, roles, an
agent's read mode and host sites, credentials, vaults and Slack config all work over
MCP under the caller's role); do not add browser-only gates. Deliberate exceptions:
`create_token` is not a tool (a raw PAT would land in a chat transcript; mint in the
web app or with `/canopy:canopy-web-pat-mint`), a browser action that reveals or
changes a secret needs a passkey step-up a token can never hold
(`apps/passkeys/stepup.py`; each route says in `api-surface.md` whether a token is
refused or let through), and the OAuth consent page needs a person.

**No agent tools.** The server is canopy the product (board, sessions, turns,
runners, schedules, mail, page actions). It offers no per-agent tools that run an
agent remotely (no `<agent>__<profile>`, no `agent_reply`): such a call would run as
a turn on the agent OWNER's machine under their credentials. To use an agent, install
its plugin from your catalog (`plugin-marketplace.md`) and run it locally as yourself.
A call to an `<agent>__…` tool gets MCP's unknown-tool error.

**A plain write is never a hand-written tool**: it is a route, so it gets the route
gate, the settings audit and the acting cap (`share_session_to_slack`,
`audit_session_noise`, `purge_session_noise` are routes that keep their tool names).
Each hand-written tool declares its gate in `apps/api/route_gates.py::HAND_TOOL_GATES`
(`tests/test_every_route_declares_its_gate.py`).

## Scoping middleware (what a credential may list and call)

* `turn_scope.py::TurnScopeMiddleware`: a caller token (`cct_…`) sees only its
  profile's canopy tools. Minting and rules: [access-confinement-and-act-as.md § How confinement is enforced](access-confinement-and-act-as.md#how-confinement-is-enforced).
* `act_as_scope.py::ActAsScopeMiddleware`: an act-as-you token (`cact_…`) lists and
  calls only what `harness.act_as.route_refusal` allows; the in-process request
  re-checks the route ([access-confinement-and-act-as.md § Acting as the person on canopy-web](access-confinement-and-act-as.md#acting-as-the-person-on-canopy-web-act-as-you-in-your-own-turn)).
* `delegation.py::DelegatedScopeMiddleware`: a host-grant token gets only the tools
  its scopes map to (`apps/host_grants/self_host.py::SCOPE_TOOLS`); prompts are
  closed, and resources too except the View of a tool in its grant
  (`delegation._view_allowed`). Read-only except `WRITABLE_SCOPES`. Page → scope →
  tool table: [embedding-reference.md §11](embedding-reference.md#canopy-web-as-a-host-of-its-own-mcp-agents-act-as-you-on-canopys-pages).
* `chat_scope.py::current_chat_session`: the chat an `X-Canopy-Chat-Key` header
  names narrows page tools to that one chat.

## Auth model

`auth.py::CanopyPATVerifier` is the server's one `TokenVerifier`; every credential
resolves to a canopy user, and there is no shared server bearer (do not reintroduce a
`CANOPY_MCP_BEARER`-style token: every tool must run as a person). PATs, OAuth access
tokens and delegated tokens resolve through `apps.tokens.principal.resolve_bearer`,
the resolver REST and the WebSocket handshake use too.

* **PAT**: `Authorization: Bearer <raw>`; a miss is a 401.
* **OAuth sign-in** (what MCP clients do by default): the 401 carries
  `WWW-Authenticate: Bearer resource_metadata="…"` (RFC 9728), the client registers
  itself (public clients only; registering grants nothing), and the consent page
  needs a signed-in person (a session minted from a token cannot approve). **The
  access token is an ordinary `PersonalToken`** tied to an `OAuthGrant`, so every
  tool and route treats it like a hand-minted PAT. Lifetimes (`ACCESS_TOKEN_TTL`,
  `REFRESH_TOKEN_TTL`, `CODE_TTL`), refresh rotation and reuse revocation (a retired
  refresh token presented again, or a replayed code, revokes the grant):
  `apps/oauth_server/mcp_oauth.py`; shared PKCE/one-time-redemption rules:
  `apps/common/oauth_core.py`. Settings → Authorized apps disconnects a grant at once.
  On labs the RFC-located `/.well-known/…/canopy…` documents sit at the ROOT of the
  shared host, which the ALB routes to canopy.
* **Host-grant token** (canopy as a host of its own MCP): a DPoP-bound token
  (`Authorization: DPoP`) issued to the widget on a canopy page
  (`apps/host_grants/self_host.py`). The SDK's `DPoPGate` in front of the MCP app
  (`delegation.py::gate`, mounted in `config/asgi.py`) verifies the proof and acts
  only on `Authorization: DPoP`. `CanopyPATVerifier` resolves such a token ONLY when a
  key was proved (`delegation.access_token_for`); a bound token sent as a plain
  bearer is a 401, as is a DPoP request to a canopy not configured as a host. Tools
  run AS the visitor (`sub` = `delegated:<id>`), so their own ACL ∩ the page's scopes.
* **Caller token** (`cct_…`, confined or scoped turn) and **act-as-you token**
  (`cact_…`): see the scoping middleware above.

## Generated API tools (`api_tools.py`)

* **Coverage.** Every route minus `EXCLUDED` / `EXCLUDED_PREFIXES`, each with its
  reason: routes whose caller is not a person (runner protocol, a browser tab's
  plumbing, an embedding host's or contact's surface, anonymous reads, raw-bytes and
  multipart routes), `create_token`, and a person's own agent-memory switches and HCP
  grant/audit/client-registry routes (an agent must never turn those for itself).
  `tests/test_mcp_api_tools.py` fails on an exclusion naming a route that no longer
  exists.
* **Names.** The operationId, qualified by app only when two modules share it
  (`canopy_sessions_send`).
* **Tenant.** Every tool takes an optional `workspace` and calls `/api/w/{workspace}/…`
  (membership checked by `WorkspaceResolveMiddleware`). Omitted, the call goes to the
  flat route as a PAT caller's would, scoped by [roles.md § A read that names no workspace stays inside one organisation](roles.md#a-read-that-names-no-workspace-stays-inside-one-organisation).
  A route that names `{workspace}` in its path keeps its own argument.
* **Identity.** The caller from the access token; a token with no canopy user is
  refused. Every call writes `MCPAuditLog`; every non-GET counts against the per-user
  write limit (`rate_limit.py`).
* **Collisions.** None allowed: FastMCP resolves hand-written tools ahead of providers,
  so a route sharing a hand-written tool's name would be unreachable;
  `tests/test_mcp_api_tools.py` fails on one. A hand-written tool that duplicates a
  route is deleted and the route keeps the name.
* **Views are not tools.** The MCP Apps View routes refuse a PAT, so a View's commit
  token never reaches a model (`embedding-internals.md`).

## Hand-written tools (`apps/mcp/tools/`)

| Tool | Notes (the module docstring has the rest) |
|---|---|
| `who_is_asking` | `caller.py`: the caller envelope for a turn (same document the runner writes to `~/.canopy/caller/<turn_id>.json`), re-read mid-turn so a contact blocked since the claim shows as blocked. `verified` is about THIS message. Gated like `GET /api/harness/turns/{id}/caller-context`; unknown ⇒ "turn not found". |
| `site_tools`, `site_call`, `site_read` | `site.py`, page conversations only. The host's tools and resources AS THE VISITOR through `apps/host_grants/host_gateway.py`; refused, never falling back to the agent's own credential, when the visitor holds no unexpired grant. The turn is read from a credential (a caller token's own turn, else the turn of the chat the chat key names), never an argument. `site_call` re-checks site, grant freshness, resource, DPoP key and tool set on every call; the token never appears in a result, log or audit row, and never reaches a runner; it refuses a name this page declares as `page_<action>`. `site_read` reads only a link a site tool returned in THIS conversation (`host_gateway.remember_links` / `readable_link`), and its answer is never kept (the audit row records the URI only). A `data: sensitive` site may hold only `sensitive_data.SENSITIVE_CANOPY_TOOLS`. |
| `current_page` | `page.py`: the page(s) the caller is looking at, narrowed to one chat by the chat key. |
| `page_<action>` (dynamic) | `page_tools.py::PageActionProvider`: each open page's declared actions as per-caller tools, run by the browser tab (`apps/canopy_sessions/page_actions.py`; `wait_once` polls so no long call holds the shared sync thread). Static tools win, so a page cannot capture a canopy tool. Narrowest first: a chat key gives that chat's page; a confined caller token gets only its OWN conversation's page; otherwise the caller's own tabs. Added to a confined profile automatically (`published_profiles.PAGE_TOOLS`). |
| `propose_session_closes` / `close_proposed_sessions` | `close_review.py`: /ide close review. The close tool is app-only and refused to every credential but a person's host grant, so only a click closes; per session it re-checks the session is not running or waiting. |

## Mount + lifespan (`config/asgi.py`)

Streamable HTTP needs the MCP app's lifespan, which Django's bare ASGI app lacks, so
`config/asgi.py` builds a Starlette router that mounts the MCP apps and Django and
runs their lifespans for the life of the process; its module docstring is the
reference. Bare `/api/mcp` is rewritten to `/api/mcp/` (`_McpWithoutSlash`). The
projects connection at `/api/mcp/projects/` is `apps/mcp/project_gateway.py`.

## How to connect Claude Code

```bash
claude mcp add --transport http canopy https://<canopy-web>/api/mcp/
```

Claude opens canopy's sign-in in the browser the first time. A PAT still works for
scripts and headless clients: header `Authorization: Bearer cpat_<token>`, minted on
`/settings`, with `/canopy:canopy-web-pat-mint`, or `manage.py create_token --email <you> --label <name>`.
