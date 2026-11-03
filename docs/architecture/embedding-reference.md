# Embedding a canopy agent: reference

Known limits (9), the relationship to CopilotKit (10), canopy embedding its own pages (11, including canopy-web as a host of its own MCP) and building your own UI instead of the widget (12).

**See also:** [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md), [embedding-advanced.md](embedding-advanced.md), [embedding-actions-and-access.md](embedding-actions-and-access.md), [embedding-internals.md](embedding-internals.md).

## 9. Reference: known limits

- **Page actions need the page open** ([§7](embedding-actions-and-access.md#7-how-actions-reach-the-agent-and-what-they-cannot-do)). There is no server-side path for an
  agent to act on your *page* later — which is a reason to put data mutations in
  your MCP server, where they work regardless.
- **Invalidation reaches attached pages only.** Nothing is queued for a tab that
  is closed; it reads fresh data when it next opens, so this is correct rather
  than a gap.
- **Without a host grant, the agent's tools run as the agent, not your
  visitor** ([§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as)). Issue grants (`id_jag` at arrival) to run them as the
  visitor; until then this is the largest limit on what an embedded agent
  should be allowed to do. A grant lives only while the visitor is on the page:
  there is no unattended work on a visitor's behalf yet.
- **Visitors are contacts unless they already have a canopy account** ([§3](embedding-a-canopy-agent.md#3-add-one-backend-endpoint)).
  With `email` + `email_verified: true` in your assertion, at a domain your
  site is allowed to resolve, an EXISTING canopy user arrives as themselves
  (their own permissions; `kind: "user"` in the response). Nobody else does, and
  no account is ever created. Any visitor your server
  vouches for can chat — there is no domain allowlist on this path — but none of
  them acts as a canopy account.
- ~~**Context is a snapshot.**~~ Not a limit: page state is pushed on every
  change and re-read by the agent on demand via `current_page`, so changing the
  page mid-conversation is reflected. The page is also told when its data
  changes ([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes)). Only `provideContext` is a snapshot; use `setPageState`.
- **The agent does not remember previous conversations.** A user can see and
  resume their prior chats, but each is its own transcript; a new conversation
  starts cold.
- **Production hosts:** connect-labs and ace-web (cross-origin, each with
  host grants on), and canopy's own pages ([§11](#11-reference-canopy-embedding-its-own-pages)). `scripts/e2e_embed_widget.py`
  walks the live chain against a deployment.

---

## 10. Reference: relationship to CopilotKit

CopilotKit is the closest off-the-shelf equivalent, and knowing the difference
sets expectations.

| CopilotKit | Here | Difference |
| --- | --- | --- |
| `useCopilotReadable` | `setPageState` | Both push on change. Ours carries a selection (ids + the tool that resolves them) rather than data, and is capped at 8 KiB to keep it that way |
| `useCopilotAction` | `registerAction` | Both take a JSON-Schema. Theirs adds in-chat rendering and human-in-the-loop confirmation; ours surfaces as an MCP tool, so any MCP agent can call it |
| `CopilotPopup` / `CopilotSidebar` | `mode: 'overlay' / 'docked'` | Equivalent, but framework-free rather than React components |
| `CopilotRuntime` → an LLM | a canopy `Agent` on a runner | **The big one.** Theirs orchestrates a model call. Ours routes to a persistent agent with its own identity, mailbox, repo, credentials and tools |

If you want a page-scoped copilot in a React app, CopilotKit is more complete
and you should probably use it. The reason to use this instead is that the thing
on the other end is a *canopy agent* — the same one that runs scheduled work and
has its own inbox — not a chat completion bound to your page.

---

## 11. Reference: canopy embedding its own pages

canopy is also a host, and **not a special one**. The reason to talk to an agent
is usually about what is in front of you, and `/w/:ws/sessions` is not in front of
you — so the widget mounts on canopy's own authenticated pages, where two real
cases live: an agent inbox that has gone stale, and a feature set you decide to
deprecate while looking at it and have forgotten a minute later.

**Setup is the ordinary setup, plus one tick.** Connect a site as in step 1 and
tick **"Show this panel on canopy's own pages"**. canopy publishes its own signed
`site.json` like any host (`/oauth/host/site.json`, beside its host keys, and
`/.well-known/canopy-site.json`), so typing canopy's address and **Fill from the
site** fills the registration, and after a move to a new address **Fill from the
site's own documents** refreshes it — no URL is typed by hand. There is no separate flow, no
reserved name and no deployment setting: canopy's own panel is whichever
connected site an owner ticked that box on, and usually that site is canopy
itself. Only one app may have it at a time, and the refusal names the one that
already does.

Ticking it also adds canopy's own origin to that app's URL list, because the two
are not independent: `frame-ancestors` is built from that list, so a ticked app
without it would be on by every visible measure and dead in the browser. The
origin comes from the request rather than a field — it is the address you are
looking at, and the one value that cannot be typed wrong.

Steps 2 and 3 still do not apply when the site *is* canopy. A third-party host
signs an assertion because canopy cannot see who its visitor is; here the two
are one process, so `POST /api/embed/token` is session-authenticated and mints
for `request.user` directly. No secret, no signing key, no backend endpoint.

**It follows canopy's own light/dark toggle**, through the same `theme` option
any host uses (`frontend/src/widget/CanopyWidget.tsx`: `mode` at init,
`setTheme` on each toggle). No accent is passed: canopy's accent is the widget's default.

### canopy-web as a host of its own MCP (agents act as you on canopy's pages)

[§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as) applies to canopy too: on canopy's own pages the agent can call **canopy's
own MCP (`/api/mcp/`) as the visitor**, through the same host grant contract a
connected site implements — not a shortcut around it. canopy-web plays the host
with the SDK's own pieces (`apps/host_grants/self_host.py`: `HostConfig`,
`GrantHandler`, `ResourceVerifier`, `DPoPGate`, the `canopy_sdk.django` stores):

| Page (route) | Page key | Scope | Tools the grant unlocks |
|---|---|---|---|
| `/w/:ws/agents/:slug/tasks` | `agent.tasks` | `tasks:read` | `list_fleet_tasks` |
| `/w/:ws/agents/:slug/skills/history` | `agent.skill_history` | `skills:read` | `skill_history`, `skill_revision_diff` |
| `/ide`, `/ide/:id` | `ide` | `sessions:close` | `propose_session_closes`, `close_proposed_sessions` (app-only) |

Read-only except `sessions:close`, writable on purpose (`self_host.WRITABLE_SCOPES`):
its one write is app-only, so only a click in its View closes anything. Each
page declares its selection with `usePageState` and a `backing_tool`, so the
agent knows where to read the rows on screen — on /ide that is the page action
`page_reviewCloseCandidates`, answered by the person's own tab, because an
owner's turn runs on the agent's own login and reads none of their sessions.
Minting on a page with a View also records that View in the site's UI index
(`self_host._index_views`), since a turn on the agent's own login never lists
through the gateway.
What the grant reaches is its scope's tools, enforced by canopy's own MCP as the
visitor.

**How one call goes.** The widget mints at `POST /api/embed/token?page=<key>`
(the key comes from the route, `frontend/src/widget/grantPage.ts`, read on
every mint because the widget outlives navigation). For a registered key canopy
signs an ID-JAG with its host key and redeems it through the **normal consumer
path** — checked against the `canopy-web` site's JWKS URL, token endpoint
discovered from RFC 8414 metadata, jwt-bearer grant POSTed with canopy's real
client assertion and DPoP proof, verified by the SDK's `GrantHandler`. Those
requests to canopy's own URLs are answered in-process by the same functions the
public endpoints serve, without the round trip through the load balancer. In the visitor's turn the
agent calls `site_call`; canopy's gateway presents the token to its own
`/api/mcp/` over HTTP with a fresh proof; the DPoP gate verifies it; the tool
runs as the visitor, limited to the scope's tools.

**The visitor is usually a member, not a contact** — canopy's pages are visited
by canopy's users — so the grant's subject is their canopy user id and the
resulting `HostGrant` hangs off their user. Their delegated token reaches **no
more than their own access already does**: tools run with their own ACL, and
only the scope's tools exist for the token (`apps/mcp/delegation.py`).

**Why the browser names the page.** canopy's pages are a single-page app, so the
server never renders a route and cannot observe which one is on screen; a signed
page token would only sign whatever the browser asked for. So the browser names
a page KEY and the server decides everything else: an unknown key is no grant,
the scopes come from the server's registry (never from the request), every scope
is read-only, and the tools run as the visitor. A browser that names the wrong
page picks among read-only views of its own data.

**Turning it on** (everything is off until all of it is set):

1. `CANOPY_HOST_SIGNING_KEY` — an Ed25519 or P-256 private key PEM (Secrets
   Manager `canopy-web/host-signing-key` on labs; `PLACEHOLDER` = off). Plus
   canopy's client keys (`CANOPY_OAUTH_CLIENT_KEY` / `CANOPY_OAUTH_DPOP_KEY`),
   since the only client canopy grants to is itself. Dev and tests generate all
   three per process.
2. On the `canopy-web` Connected site (the one showing its panel on canopy's
   pages), in **Settings → Connected sites**: JWKS URL
   `{CANOPY_PUBLIC_BASE_URL}/oauth/host/jwks.json`, sign-in issuer
   `{CANOPY_PUBLIC_BASE_URL}`, MCP server `{CANOPY_PUBLIC_BASE_URL}/api/mcp/`.
   On labs (canopy.dimagi.com): `https://canopy.dimagi.com/oauth/host/jwks.json`,
   `https://canopy.dimagi.com`,
   `https://canopy.dimagi.com/api/mcp/`.
3. On each agent, a capability naming the site, e.g.
   `sites: [canopy-web]` (a `ceiling:` is optional and only
   narrows), and an access-list line giving the people who should use it that one
   capability (or a role that opens it), such as a workspace-wide default line for everyone in the workspace. `site_tools`/`site_call` are added to it automatically.
   (A full-profile turn — the agent's owner, a workspace owner, an admin — has
   no caller token and so no gateway; it already runs with its own canopy login.)
4. **Test connection** on that site should come back all green.

**What this does and does not prove.** The frame is same-origin here, so none of
the origin discipline is exercised — not `targetOrigin`, not `event.origin`
rejection, not storage partitioning. What it does exercise is everything above
that: the handshake, the agent picker, session creation and history, page
state, invalidation, page actions, and whether the panel is pleasant to use. A real
third-party host is still the only test of the boundary.

---

## 12. Reference: building your own UI instead of the widget

The widget is the supported path, and almost always the right one: it needs no
install, no build step and no framework, and it isolates canopy's UI in an
iframe. Build your own only if a chat panel in an iframe genuinely cannot work
for your page.

| package | what it is | needs |
| --- | --- | --- |
| [`canopy-client`](https://www.npmjs.com/package/canopy-client) | the transport: token cache, REST calls, the session socket URL, the host bridge | nothing — zero dependencies, ships JavaScript (use ≥ 0.2.0) |
| [`canopy-ui`](https://www.npmjs.com/package/canopy-ui) | React components: the chat panel, `useSessionSocket` (AG-UI on the wire), design tokens | React 19 as a peer, and a bundler that compiles TypeScript in `node_modules` (Vite does; webpack + babel with `exclude: /node_modules/` does not) |

Neither imports the other. A React 18 or non-React host uses `canopy-client`
and renders its own messages; a React 19 + Vite host can use both. Everything
in [§1](embedding-a-canopy-agent.md#1-register-your-app-in-canopy)–[§3](embedding-a-canopy-agent.md#3-add-one-backend-endpoint) still applies — the token endpoint, the registered origins and agents
— and sessions are still created by your backend, never by the browser.

---
