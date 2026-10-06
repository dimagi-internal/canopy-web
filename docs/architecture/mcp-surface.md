# MCP Surface

canopy-web exposes its whole REST API as a
[FastMCP](https://github.com/jlowin/fastmcp) 4.x server, mounted at
`/api/mcp/` over **Streamable HTTP**. External MCP clients (Claude Code,
other agents) call these tools to inspect and mutate canopy-web state.

**Every REST route is a tool.** `apps/mcp/api_tools.py` generates one tool per
route of the single `NinjaAPI` from its OpenAPI schema — name = operationId,
parameters = the route's path/query/body, description = its docstring — and a
call is a request to that route, dispatched **in-process** (Django's ASGI
handler is the httpx transport; no socket) **as the caller** (handed to
`BearerTokenAuthMiddleware` in the ASGI scope, which no network request can
set). The web app calls the same API, so anything it can do an MCP client can
do, and a new route is on MCP without a step. See "Generated API tools" below.

A handful of **hand-written** tools remain for what is not a REST route
(caller-envelope, site gateway, per-agent capability tools, page tools, Slack
share, session-noise audit). The implementation lives in `apps/mcp/`.

## Module layout (`apps/mcp/`)

| File | Responsibility |
|---|---|
| `auth.py` | `CanopyPATVerifier` — a FastMCP `TokenVerifier` that resolves a Personal Access Token to a Django user (mirrors `apps.tokens.middleware`). |
| `delegation.py` | canopy-web as a host of its own MCP: `gate()` (the SDK's `DPoPGate`, mounted in `config/asgi.py`), `access_token_for()` (a host-grant token → the visitor's `AccessToken`), and `DelegatedScopeMiddleware` (only the grant's scopes' tools). |
| `api_tools.py` | The generated API tools: `CanopyAPIProvider` (one tool per REST route), the in-process transport, the exclusion list. |
| `server.py` | The `FastMCP("canopy-web")` instance with `MultiAuth` (PAT + optional OAuth), and `build_http_app()` for the ASGI mount. |
| `rate_limit.py` | Per-user write rate limit (mutating tools). |
| `audit.py` | `current_user_id()` + `write_audit()` (writes `MCPAuditLog`). |
| `models.py` | `MCPAuditLog` — one row per tool call. |

Tools reuse the SAME service functions as the REST views
(`apps.projects.services`), so the REST and MCP surfaces cannot drift.

## Auth model — dual auth (MultiAuth)

```python
MultiAuth(
    server=<GoogleProvider or None>,     # interactive OAuth (env-gated seam)
    verifiers=[CanopyPATVerifier()],     # per-user PAT (always on)
)
```

* **PAT (priority, always on).** A per-user Personal Access Token
  (`apps.tokens`) presented as `Authorization: Bearer <raw>`.
  `CanopyPATVerifier.verify_token()` calls `PersonalToken.lookup()`,
  stamps `last_used_at`, and returns a FastMCP `AccessToken` whose claims
  carry the user (`sub`/`user_id`/`email`). Tools read the user via
  `get_access_token()`. A miss returns `None` → 401.

* **OAuth sign-in (what MCP clients do by default, 2026-09-30).** Adding
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
  `apps/tokens/mcp_oauth.py` (the authorization server),
  `views_mcp_oauth.py` (register + consent), `views_oauth.token` (one token
  endpoint for both grants). On labs the RFC-located documents sit at the ROOT
  of the shared host (`/.well-known/…/canopy…`), which the ALB routes to canopy.

* **Host-grant token (canopy-web as a host of its own MCP, 2026-09-27).**
  An ADDITIONAL credential; PATs and caller tokens are unchanged. When the
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
  map to (`self_host.SCOPE_TOOLS`: `items:read` → `list_items`, `skills:read` → `skill_history` +
  `skill_revision_diff`); resources and prompts are closed to it. So a
  member's delegated token reaches at most their own ACL ∩ a read-only scope.
  A DPoP request to a canopy that is not configured as a host is a 401.

The legacy single shared `CANOPY_MCP_BEARER` and the hand-rolled ASGI
gate in `config/asgi.py` are GONE — auth is now enforced inside the MCP
app by MultiAuth.

## Generated API tools (`api_tools.py`)

* **Coverage.** Every route, minus `EXCLUDED` / `EXCLUDED_PREFIXES`: routes whose
  caller is not a person — the runner protocol (heartbeat, claim, streams, turn
  lifecycle…), a browser tab's plumbing (page state, attach/detach, push
  subscription), an embedding host's or a contact's surface, anonymous public
  reads, raw-bytes responses, multipart uploads — and `create_token` (a tool
  would put a fresh raw PAT into a chat transcript). **There are no web-app-only
  actions:** anything a person can do in canopy, their MCP client can do, under
  the same role checks (browser-only gates were added and removed the same day,
  2026-10-02 — they cost the MCP the actions it is most useful for). Leaving a route out is never a security decision: a tool
  is exactly as powerful as the caller's token already is against REST — the
  refusal is in the route.
* **Names.** The operationId, which `CanopyNinjaAPI` makes the view function's
  own name, qualified by app only when two modules share it
  (`canopy_sessions_send`, `session_sharing_list_sessions`).
* **Tenant.** Every tool takes an optional `workspace`; the call goes to
  `/api/w/{workspace}/…` (membership checked by `WorkspaceResolveMiddleware`).
  Omitted, the call goes to the flat route exactly as a PAT caller's would —
  reads span every workspace you belong to. A route that already names
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
  unreachable; `tests/test_mcp_api_tools.py` fails on one. The item,
  schedule and skill-history tools that predated this (and that the host-grant
  scopes and page contract name) are now their routes under the same names —
  `skill_history` / `skill_revision_diff` gained theirs
  (`/api/agents/{slug}/skill-history/{revisions,diff}`) rather than stay
  MCP-only.

## Hand-written tools

| Tool | Kind | Purpose |
|---|---|---|
| *(caller tokens)* | auth | A CONFINED session authenticates with a `cct_…` caller token (`apps/harness/caller_tokens.py`), minted per confined turn at claim and bound to its conversation — never the runner owner's PAT. The canopy plugin's `headersHelper` sends it from the session's profile. It resolves to the conversation's CURRENT turn: tools run as that turn's asker (their own ACL; **no canopy user** for an outside contact) and `TurnScopeMiddleware` (`apps/mcp/turn_scope.py`) lists and allows only the canopy tools the capability names (`mcp__*canopy-web__<tool>` → `<tool>`) — `agent ∩ caller`. `who_is_asking` answers only about the token's own conversation. Expired, long-finished, or a conversation whose current turn is not confined → the token authenticates nothing. |
| `who_is_asking` | read | The caller envelope for a turn (`apps/harness/caller_context.py`): who asked, `verified` (about THIS message — a spoof of a once-verified address is not verified), `relationship` to the agent (owner/admin/member/caller/system), and the workspace's contact profile (`notes`, `attributes`, this-message vs best grade, `is_blocked`). The same document the claiming runner writes to `~/.canopy/caller/<turn_id>.json`; this is the mid-turn re-read, so a contact blocked or re-annotated since the claim shows as such. Gated like the REST twin `GET /api/harness/turns/{id}/caller-context`: tenant membership, unknown ⇒ "turn not found". |
| `site_tools` | read, **caller-token only** | The tools of the Connected site the visitor is on that this turn may use (host grant contract v1): what the host lists AS THE VISITOR for their grant, narrowed by the capability's `ceiling` only when the owner set one (the page's `backing_tool` is a hint to the agent, not a filter). The turn is read from the caller token, never an argument. Refused — never a fallback to the agent's own credential — when the visitor holds no unexpired grant ("I need you back on the page"). |
| `site_call` | write, **caller-token only** | Call one host tool as the visitor, through canopy (`apps/tokens/host_gateway.py`): canopy attaches the visitor's host-issued token as `Authorization: DPoP` with a fresh proof per request (`htm`/`htu`/`ath`/`iat`/`jti`) and `Canopy-Actor: <agent>`. Re-checks site, capability `sites:`, grant freshness, resource, DPoP key and the effective tool set on every call. Audited per call; the token never appears in a result, error, log or audit row, and never reaches a runner. Added to a confined profile automatically for any capability with `sites:`. |
| `<agent>__<capability>` | write (rate-limited), **dynamic** | Each agent's DECLARED INTERFACE served as tools, computed per caller by `AgentInterfaceProvider` (`apps/mcp/agent_tools.py`), the sibling of `PageActionProvider`: `ace__ask`, `ace__summarise_opportunity`. Listed for agents in the caller's workspaces that have a declared interface (held on canopy-web) — every capability for the agent's owner and admins and for members matched by a `full:` rule, the ones naming their member class for other members, nothing for anyone else. A call is a TURN asked by the caller (initiator = their token's user, `via=mcp:<capability>`) in a conversation they own: full profile for owner/admins, confined to the capability for a member. Capability `input` fields become typed required parameters. Re-authorized on every call (listing is not permission). Waits up to `wait_seconds` (≤110) and returns `{conversation_id, turn_id, status, reply}`; `status: running` → call `agent_reply`; `waiting_on_you` carries the agent's `question`, answered by calling again with the `conversation_id`. |
| `agent_reply` | read | The latest turn of a conversation the CALLER started with an agent tool: status, reply so far, pending question. Nobody else's conversations. |
| `share_session_to_slack` | write (rate-limited) | Post a session-written summary into a Slack channel: `broadcast` (one post) or `bind` (the thread becomes the session's own, exactly as if Slack had started it). Sharing an already-bound session posts an update into its thread instead; `channel` is then optional. Resolves "this session" by `claude_session_id` → `RunnerBinding.transcript_id`, else emdash task + project, else `session_id`, always inside what the caller can see. Backs the canopy plugin's `/canopy:share-to-slack`; rules live in `apps/slack/share.py`. |

## Mount + lifespan (`config/asgi.py`)

Streamable HTTP requires the MCP app's lifespan to run (session
management). The Django bare ASGI app has no lifespan, so the app is a
Starlette router that mounts the MCP app under `/api/mcp` and the Django
app at `/`, with `lifespan=mcp_app.lifespan`:

```python
mcp_app = build_http_app()  # mcp.http_app(path="/", transport="streamable-http")
application = Starlette(
    routes=[Mount("/api/mcp", app=dpop_gate(mcp_app)), Mount("/", app=django_asgi_app)],
    lifespan=mcp_app.lifespan,
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
    "headers": { "Authorization": "Bearer <your-PAT>" }
  }
}
```

Mint a PAT on `/settings`, with `/canopy:canopy-web-pat-mint`, or
`manage.py create_token --email <you> --label <name>`.

## History

The first server (fastmcp 0.4) also derived tools from OpenAPI, but executed
them over an HTTP loopback to localhost with ONE shared `CANOPY_MCP_BEARER`, so
every tool ran as the same identity; it was replaced in May 2026 by explicit
per-user tools, and its `x-mcp-expose` markers were deleted on 2026-09-18
because nothing read them. The generated surface (2026-09-29) keeps the
per-user model and drops the loopback: the request is dispatched in-process,
as the caller.
