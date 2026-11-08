# Embedding a canopy agent: reference

Known limits (9), the relationship to CopilotKit (10), canopy embedding its own pages (11, including canopy-web as a host of its own MCP) and building your own UI instead of the widget (12).

**See also:** [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md), [embedding-advanced.md](embedding-advanced.md), [embedding-actions-and-access.md](embedding-actions-and-access.md), [embedding-internals.md](embedding-internals.md).

## 9. Reference: known limits

- **Page actions need the page open** ([§7](embedding-actions-and-access.md#7-how-actions-reach-the-agent-and-what-they-cannot-do)). There is no server-side path for an
  agent to act on your *page* later, which is a reason to put data mutations in
  your MCP server, where they work regardless.
- **Invalidation reaches attached pages only.** Nothing is queued for a closed
  tab; it reads fresh data when it next opens.
- **Without a host grant, the agent's tools run as the agent, not your
  visitor** ([§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as)). Issue grants (`id_jag` at arrival) to run them as the
  visitor; until then this is the largest limit on what an embedded agent
  should be allowed to do. A grant lives only while the visitor is on the page:
  there is no unattended work on a visitor's behalf.
- **Visitors are contacts unless they already have a canopy account** ([§3](embedding-a-canopy-agent.md#3-add-one-backend-endpoint)).
  With `email` + `email_verified: true` in your assertion, an EXISTING canopy
  user who is a member of the site's workspace arrives as themselves (their own
  permissions; `kind: "user"` in the response). Nobody else does, and no account
  is ever created (`contacts.services.resolve_arrival`).
- **Page state is live, `provideContext` is a snapshot.** Page state is pushed on every
  change and re-read by the agent on demand via `current_page`, and the page is
  told when its data changes ([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes)). Use `setPageState` for anything that changes.
- **A new conversation starts cold.** A user can see and resume their prior
  chats, but each is its own transcript; carrying anything across is the agent
  memory a person opts into (`apps/contacts/contact_hcp_api.py`, gated for
  contacts by the `HCP_CONTACT_OPT_IN` setting).

---

## 10. Reference: relationship to CopilotKit

CopilotKit is the closest off-the-shelf equivalent. What differs here: `setPageState` carries a
selection (ids plus the tool that resolves them), not data, and is capped at 8 KiB to keep it that way;
`registerAction` surfaces as an MCP tool, so any MCP agent can call it; and the other end is a
persistent canopy `Agent` on a runner, with its own identity, mailbox, repo, credentials and tools,
not a model call bound to your page.

---

## 11. Reference: canopy embedding its own pages

canopy is also a host, and **not a special one**: the widget mounts on canopy's own
authenticated pages through the ordinary registration.

**Setup is the ordinary setup, plus one tick.** Connect a site as in step 1 and
tick **"Also show the agent panel on canopy's own pages"** (`show_on_canopy_pages`
on the host-sites routes; `frontend/src/pages/HostSitesPage.tsx`). canopy publishes its own signed
`site.json` like any host (`/oauth/host/site.json` and
`/.well-known/canopy-site.json`, `apps/oauth_server/views_oauth.py::host_site_document`),
so typing canopy's address and **Fill from the site** fills the registration. There is no
separate flow, no reserved name and no deployment setting: canopy's own panel is whichever
host site an owner ticked that box on. Only one app may have it at a time
(`already_shown`, 409, names the one that does).

Ticking it also adds canopy's own origin, taken from the request, to that app's URL
list (`embed_apps.set_show_on_canopy_pages`): `frame-ancestors` is built from that list, so a
ticked app without it would be on by every visible measure and dead in the browser.

Steps 2 and 3 do not apply when the site *is* canopy: `POST /api/embed/token` is
session-authenticated and mints for `request.user` directly
(`apps/embedding/embed_api.py::embed_self_token`). No secret, no signing key, no backend
endpoint. The panel follows canopy's own light/dark toggle through the ordinary `theme`
option (`frontend/src/widget/CanopyWidget.tsx`); no accent is passed.

### canopy-web as a host of its own MCP (agents act as you on canopy's pages)

[§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as) applies to canopy too: on canopy's
own pages the agent can call **canopy's own MCP (`/api/mcp/`) as the visitor**, through the same
host grant contract a host site implements, not a shortcut around it. Which pages, scopes and
tools: `PAGE_SCOPES`, `SCOPE_TOOLS` and `WRITABLE_SCOPES` in `apps/host_grants/self_host.py`, whose
module docstring is the design. What holds:

- **The browser names a page KEY and the server decides everything else**
  (`frontend/src/widget/grantPage.ts`, read on every mint because the widget outlives
  navigation). canopy's pages are a single-page app, so the server cannot observe which route is
  on screen and a signed page token would only sign what the browser asked for. An unknown key is
  no grant, scopes come from the server's registry and never from the request, and a browser that
  names the wrong page picks among read-only views of its own data.
- **Read-only, except `sessions:close`** (`WRITABLE_SCOPES`), whose one write is app-only: only a
  click in its View closes anything. Do not add a write scope without declaring it there.
- **The visitor is usually a member**, so the grant's subject is their canopy user id. The
  delegated token reaches no more than their own access already does: tools run with their own
  ACL and only the scope's tools exist for it (`apps/mcp/delegation.py`).
- **Minting redeems through the normal consumer path** (`POST /api/embed/token?page=<key>`:
  an ID-JAG signed with the host key, redeemed against the `canopy-web` site's JWKS URL, DPoP
  verified by the SDK's `GrantHandler`); requests to canopy's own URLs are answered in-process
  (`self_host.loopback_get` / `loopback_post`). In the visitor's turn the agent calls `site_call`
  and the DPoP gate verifies the token at `/api/mcp/`.
- **Pages declare their selection** with `usePageState` and a `backing_tool`. On `/ide` that is the
  page action `page_reviewCloseCandidates`, answered by the person's own tab, because an owner's turn
  runs on the agent's own login and reads none of their sessions. Minting on a page with a View records
  that View in the site's UI index (`self_host._index_views`).

**Turning it on** (everything is off until all of it is set; `self_host.py` "Off until configured"):

1. `CANOPY_HOST_SIGNING_KEY` (an Ed25519 or P-256 private key PEM; Secrets Manager
   `canopy-web/host-signing-key`, see `deploy/aws/canopy-web.cfn.yaml`; `PLACEHOLDER` reads as off),
   plus canopy's client keys `CANOPY_OAUTH_CLIENT_KEY` / `CANOPY_OAUTH_DPOP_KEY`, since the only client
   canopy grants to is itself. Dev and tests generate all three per process.
2. On the `canopy-web` host site (the one showing its panel on canopy's pages), in **Settings → Host
   sites**: JWKS URL `{CANOPY_PUBLIC_BASE_URL}/oauth/host/jwks.json`, sign-in issuer
   `{CANOPY_PUBLIC_BASE_URL}`, MCP server `{CANOPY_PUBLIC_BASE_URL}/api/mcp/`.
3. On each agent, a host site naming the site (`sites: [canopy-web]`; a `ceiling:` is optional and
   only narrows), and an access-list line giving the people who should use it that one host site (or
   a role that opens it). `site_tools`/`site_call` are added automatically. A full-profile turn (the
   agent's owner, a workspace owner, an admin) has no caller token and so no gateway; it already runs
   with its own canopy login.
4. **Test connection** on that site should come back all green (`apps/embedding/connection_test.py`).

**What this does and does not prove.** The frame is same-origin here, so none of the origin
discipline is exercised: not `targetOrigin`, not `event.origin` rejection, not storage
partitioning. Everything above that is: the handshake, the agent picker, session creation and
history, page state, invalidation, page actions. A real third-party host is still the only test
of the boundary.

---

## 12. Reference: building your own UI instead of the widget

The widget is the supported path: it needs no install, no build step and no framework, and it
isolates canopy's UI in an iframe. Build your own only if a chat panel in an iframe genuinely cannot
work for your page. Then use [`canopy-client`](https://www.npmjs.com/package/canopy-client) (the
transport, zero dependencies) and optionally [`canopy-ui`](https://www.npmjs.com/package/canopy-ui)
(React components and `useSessionSocket`); what each needs and how to choose is in their READMEs
(`frontend/packages/canopy-client/README.md`, `frontend/packages/canopy-ui/`). Neither imports the
other. Everything in [§1](embedding-a-canopy-agent.md#1-register-your-app-in-canopy)–[§3](embedding-a-canopy-agent.md#3-add-one-backend-endpoint)
still applies: the token endpoint, the registered origins and agents.

---
