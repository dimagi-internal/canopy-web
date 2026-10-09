# canopy-web as an MCP Apps host: a site's tool can show a View, and a click acts as the viewer

**Status:** design, 2026-10-08. Decision (owner, Jonathan, 2026-10-08): adopt the
standard, **MCP Apps** (SEP-1865, `io.modelcontextprotocol/ui`, spec status Stable
2026-01-26; `modelcontextprotocol/ext-apps`, latest v2.0.3). We render a host's UI
resources sandboxed in the chat, including the embed panel. We do not build a
canopy-specific widget protocol. **Owner decisions recorded below (2026-10-08) override
the open questions and the sandbox-origin design in §7**; implementation lands in the
staged PRs that follow this doc.

Section references (§) are to `specification/2026-01-26/apps.mdx` in
`modelcontextprotocol/ext-apps`. Code references are to `origin/main` of this
repo, and of `connect-labs` for the Labs half.

## The problem, in the flow that raised it

A person is on a Labs workflow run page with the canopy panel open
(`frontend/src/embed/EmbedApp.tsx`). They ask ACE to coach a worker. ACE calls
Labs' `workflow_run_action` without `confirm` and with `include_image`. Labs
returns a **preview**: the worker, the opening message, the briefing topics, a
coaching PNG, and a single-use `confirm` token
(`connect_labs/workflow/actions.py:620-724`). A second call carrying that token
does the send (`actions.py:726-765`).

Today the confirmation is a stopgap. ACE's skill `skills/coach-from-labs-page`
(ace PR #2831) has the agent ask with `AskUserQuestion`. That has three problems:

1. **It only renders as buttons on interactive runners.** Canopy turns a parsed
   dialog into `MenuPrompt` (`frontend/packages/canopy-ui/src/chat/MenuPrompt.tsx`,
   wired at `EmbedApp.tsx:885`). A headless turn has no dialog to parse.
2. **The picture is not shown.** The person says yes to a description of an image
   they never saw.
3. **The agent holds the token, so the human's yes is advisory.** Nothing
   stops a model that has `confirm` from sending it without the click.
   Worse, the token is bound to whoever previewed (`_digest(user.pk, …)`,
   `actions.py:608-612`). When the agent previews on its own Labs login, the
   token is the agent's, not the person's.

With MCP Apps, Labs returns a View for the preview. Canopy renders it. The
"Send" click is a `tools/call` that goes from canopy to Labs **as the viewer**,
with a token the View obtained as the viewer. The model never needs it.

## Design principles

- **The standard, whole, before anything canopy-specific.** Any departure is
  a MAY the spec allows, and is listed here.
- **The View acts as whoever is looking at it.** That is the viewer, not the turn's initiator and not
  the agent. In a shared session these can be three different people.
- **The host is the witness.** Canopy records each View-initiated call itself, whatever
  the View chooses to tell the model.
- **Rendering is canopy's job, not the runner's.** So headless turns get Views too. A runner
  only produces the tool call and its result.

## Actors and origins

| Actor | Origin | Role |
|---|---|---|
| Host page | `https://canopy.dimagi.com` (ChatPage), or the embed panel at `/embed/chat` framed by `labs.connect.dimagi.com` (`apps/tokens/views_embed.py:220`) | Runs the host bridge, renders the chat |
| Sandbox proxy | `/mcp-apps/sandbox/` on canopy, **opaque origin** (owner decision 1; `MCP_APPS_SANDBOX_URL`) | Outer iframe; §Sandbox proxy |
| View | inner iframe of the proxy, `srcdoc` | The host app's HTML (`ui://…`) |
| MCP server | the Connected site's `host_mcp_resource` (Labs: `…/mcp/`) | Serves `tools/list`, `resources/read`, `tools/call` |

Canopy is already the MCP **client** of the host in one place:
`apps/tokens/host_gateway.py`. It resolves a turn to a site and a visitor's
DPoP-bound delegated grant (`resolve`, :133; `context_for_grant`, :209). It
lists the host's tools (:335) and calls them as that person (:352). The agent
reaches it through `site_tools` / `site_call` (`apps/mcp/tools/site.py:51-106`).
MCP Apps attaches there: canopy sees `_meta.ui` on `tools/list`, reads the
`ui://` resource, and forwards the View's `tools/call`, all through the same
client and the same DPoP machinery.

## Design

### 1. Negotiate the extension (§Client<>Server Capability Negotiation)

The gateway client (`host_gateway._client`, :294) advertises
`capabilities.extensions["io.modelcontextprotocol/ui"] = {"mimeTypes": ["text/html;profile=mcp-app"]}`
in `initialize`. An implementation task is to confirm that FastMCP's `Client`
can set `extensions`. If it cannot, set it in the initialize hook; do not fork
the client. A server that does not declare the extension gets nothing new: its
tools stay text-only.

### 2. Discover UI tools and enforce visibility (§Resource Discovery, §Visibility)

`list_tools` (:347) currently keeps only `name/description/input_schema` and
drops `_meta`. Changes:

- **Keep `_meta.ui`.** Store `{resourceUri, visibility}` per tool in a per-site
  **UI tool index**, refreshed whenever the gateway lists. Only `_meta.ui.resourceUri`
  counts: the deprecated flat `_meta["ui/resourceUri"]` is not read.
- **Hide app-only tools from the agent.** `site_tools` MUST NOT return a tool
  whose `visibility` lacks `"model"` (§Visibility, "tools/list behavior").
- **Refuse the agent an app-only tool.** `site_call` refuses (`not_allowed`,
  audited like :94-97) a tool whose visibility lacks `"model"`. The spec
  only mandates hiding; refusing is the matching rule, since a hidden tool should
  not be callable by name.
- Visibility is **not** a security boundary against a direct MCP client (below).
  Every app-only tool still authorizes the caller itself.

### 3. Which tool calls get a View

There are two ways a host tool call reaches a transcript, and the View must work for both:

- **G (gateway):** the agent calls `site_call` (a confined visitor's session). The
  transcript row is `site_call`, and the host tool is its `input.tool`.
- **D (direct):** the agent calls the site on its own MCP connection, e.g. ACE's
  own `connect_labs` login in the owner's session. Canopy sees only the
  `tool_use`/`tool_result` blocks (`apps/canopy_sessions/stream_map.py:76-85`).
  The host tool is `host_tool_name(name)` (`host_gateway.py:98`).

**Rule:** a `tool_result` gets a View when (a) the session was held on a
Connected site (`session.metadata.embed_app`, the same field `resolve` reads at
:161), and (b) that site's UI tool index has a `resourceUri` for the host tool
name. Canopy decides this server-side. When projecting `chat.tool_result`
(`apps/canopy_sessions/agui.py:315-330`), it adds `metadata.canopy.app = {site,
tool, resource_uri}` through `_meta` (:93). The REST history path gets the same
field. The client never guesses which tools have Views. The View receives
`tool-input` (the call's arguments) and `tool-result` (`content`, plus
`structuredContent` when the row has it). The Labs View is designed to need only
the arguments, because a path-D transcript may not carry `structuredContent`.

Matching on the host tool name within the session's one site is enough for P1.
A name collision would render a View that then fails, because the View acts with
the viewer's own grant. Owner decision 3 covers whether path D gets Views at all.

### 4. The viewer, not the initiator

`resolve` picks the grant of the turn's initiator (:177-187). A View's call
instead needs the grant of the person who clicked. Add
`host_gateway.resolve_for_viewer(session, principal)`:

1. The site comes from `session.metadata.embed_app`. The live `AppCredential`
   comes from the agent's tenant, with the `issues_host_grants()` check (:166-170).
2. The grant is the **principal's own** `HostGrant`: `user_id` for a canopy user,
   `contact_id` for a contact (the same two keys as :179-182).
3. `context_for_grant(app, grant, …)` (:209) applies the same expiry, resource
   and DPoP-key checks. The ceiling is the session's capability ceiling, if it
   has one. It can narrow and never widen.
4. **Runner requirements (ZDR)** do not apply. No runner touches a View call;
   canopy-web makes it directly.

**Gate order for a View `tools/call`.** Each refusal is a JSON-RPC error to the
View and one audit row:

| # | Check | Source |
|---|---|---|
| 1 | Principal may write in this session: owner/editor; a contact in their own session | `access.can_write` (`apps/canopy_sessions/access.py:239`); contact surface |
| 2 | `tool_call_id` is a real View-bearing result in this session | §3 metadata |
| 3 | Target tool is on the **same server** as the View's tool | §Visibility, "app from the same server connection only" |
| 4 | Target tool's visibility includes `"app"` | §Visibility, "tools/call behavior" (host MUST reject) |
| 5 | `tool_allowed(tool, ceiling)` | `host_gateway.py:115` |
| 6 | Principal holds a live grant for the site | `resolve_for_viewer` |
| 7 | The host's own ACL, as that person | Labs, per call |

A **viewer-role participant** fails gate 1. They still see the View. Canopy
tells them they cannot act by **omitting `serverTools` from `hostCapabilities`**
in the `ui/initialize` result (§Host Capabilities). That is the standard's
"cannot proxy tool calls" signal, so the View renders its buttons disabled. Gate 1
refuses anyway if the View ignores the signal.

A viewer **without a live grant** fails gate 6, for example someone reading the
session later on `canopy.dimagi.com`, after the page grant expired. They get the
same refusal message as `BACK_ON_THE_PAGE` (:62), and the View offers
`ui/open-link` to the run page. Owner decision 4 covers whether canopy should
prompt for a grant in place instead.

### 5. Endpoints (browser plumbing, never MCP tools)

New session-scoped REST routes for the host bridge:

| Route | Does |
|---|---|
| `GET  /api/sessions/{id}/apps/{tool_call_id}/resource` | `resources/read` of the `resource_uri`, as the viewer. Falls back to a cached copy keyed by `(site, uri, sha256)` when the viewer has no grant, since a View can render read-only |
| `POST /api/sessions/{id}/apps/{tool_call_id}/call` | One `tools/call` through gates 1-7 |
| `POST /api/sessions/{id}/apps/{tool_call_id}/read` | A View's own `resources/read`, same server only |
| `PUT  /api/sessions/{id}/apps/{tool_call_id}/context` | `ui/update-model-context` (§6) |

Contacts get the same routes under `/api/contact/`, which is already excluded
from MCP (`apps/mcp/api_tools.py:68`). The user routes go in `EXCLUDED` with
reason `_BROWSER` (:62, :76). Every REST route becomes an MCP tool unless
excluded, so without that exclusion the agent could call app-only tools
as its own user. That is exactly what visibility forbids.

### 6. How the agent learns what was clicked (§ui/message, §ui/update-model-context)

- **`ui/update-model-context`:** canopy stores the latest payload per
  `(session, tool_call_id)`, overwriting as §ui/update-model-context says. It is a
  bounded packet (8 KiB, like page state), a cache and never the authority,
  exactly as `apps/canopy_sessions/page_state.py` frames page state. It is handed
  to the **next** turn with the caller context, and only the last update before
  that turn counts.
- **`ui/message`:** posted through `services.send_message`
  (`apps/canopy_sessions/services.py:1638`) as a user message **authored by the
  viewer**, with `origin="app_view"`, gated by `can_write`. It starts a turn,
  which is the spec's "trigger follow-ups". Owner decision 5 covers whether a commit
  should do this.
- **The host's own receipt.** Whatever the View says, each View `tools/call`
  that reaches the host writes a canopy-authored session event (`app_call`: who,
  tool, ok/error, `tool_call_id`). It renders as a one-line receipt under the
  View, for example "Sent by Jon at 14:02", and the next turn sees it. A View
  cannot forge or suppress it. This is canopy's answer to §Security
  "View performs … social engineering": the model learns of a send from the
  host, not from the View's claim.

### 7. The sandbox (§Sandbox proxy, §Security Implications)

> **Amended by owner decision 1:** no separate origin. The proxy is a canopy
> sub-path made opaque (`sandbox="allow-scripts"`, no `allow-same-origin`). The
> first two bullets below are the design this replaced; see § Owner decisions.

- **The proxy runs on a different origin** (§Sandbox proxy 1). Its outer iframe
  is `sandbox="allow-scripts allow-same-origin"` (2). It sends
  `ui/notifications/sandbox-proxy-ready` (3). The host answers
  `ui/notifications/sandbox-resource-ready` with `{html, csp, permissions,
  sandbox}` (4). The proxy then relays everything that is not
  `ui/notifications/sandbox-*` (6) and synthesizes no requests (7). The host sends
  nothing to the View before `ui/notifications/initialized` (6).
- **Why the origin must differ, in canopy's own terms.** Canopy's existing
  rule for uploaded HTML is *no* `allow-same-origin`, because
  `allow-scripts` plus same-origin is no sandbox at all
  (`frontend/src/lib/uploadedContentSandbox.ts:1-19`;
  `apps/walkthroughs/streaming.py:57`). MCP Apps requires `allow-same-origin` on
  the proxy, so the proxy cannot share canopy's origin. It must be another
  origin that holds no canopy cookies.
- **Inner View iframe:** canopy sets the `sandbox` override to
  `allow-scripts allow-forms`, **without** `allow-same-origin`. Each View
  therefore gets an opaque origin, and Views from different servers do not share
  storage on the proxy origin. Canopy's uploaded-content rule holds one level
  down.
- **CSP** (§UI Resource Format "Host Behavior", §Security 4): built from
  `_meta.ui.csp`. When it is omitted, canopy uses the spec's restrictive default
  (`default-src 'none'; … img-src 'self' data:; connect-src 'none'`), plus
  `frame-src 'none'; object-src 'none'; base-uri 'self'`. Canopy **restricts
  further**, as the spec allows: a declared domain is honoured only if it is one
  of the Connected site's own registered origins. Others are dropped and logged.
  Canopy never loosens a declared policy. `permissions` are not granted in P1.
- **Proxy page response headers:**
  `frame-ancestors https://canopy.dimagi.com https://labs.connect.dimagi.com`,
  no cookies, and no session middleware. The sandbox host serves **only** the
  proxy page and its script; every other path is a 404.
- **Resource review** (§Security 3): canopy logs the sha256 and the effective CSP
  of every fetched `ui://` resource once per change. A hash change shows on
  the Connected sites table. Pinning by hash is deferred.
- **Display:** inline only. `ui/request-display-mode` returns `inline`. Height
  follows `ui/notifications/size-changed`, capped. `hostContext` carries the
  theme and `--color-*` variables from canopy-ui, plus `platform: "web"`.
  `ui/open-link` accepts `https:` only and opens with `noopener`. The View
  is drawn inside a labelled frame ("from Labs") (§Other risks, "clearly
  indicate sandboxed UI boundaries").

Embed nesting is four levels: Labs page, canopy embed (canopy origin), sandbox
proxy (sandbox origin), View (opaque). The proxy's `frame-ancestors` names the
embed's origin. Nothing in the chain needs Labs' cookies.

### 8. Frontend

- A new `AppView.tsx` in `frontend/packages/canopy-ui/src/chat/` is rendered by
  `ToolCallPair`/`MessageItem` when a result carries `metadata.canopy.app`. The
  tool row stays collapsed below it. Both `ChatPage` and `EmbedApp` mount the same
  `ChatPanel`, so they get it together. EmbedApp's narrow panel only needs
  `containerDimensions.maxWidth`.
- **Host bridge:** use `@modelcontextprotocol/ext-apps`' host-side bridge pinned
  at 2.0.3 if it fits canopy's transport. Otherwise, write a small bridge that
  follows §Communication Protocol. It checks `event.source` and `event.origin`
  on every message and drops malformed ones (§Security 2).
- **Remount on reload:** the View re-initializes from the transcript row. The Labs
  View previews again as the viewer on every mount (Labs half, below), so a
  reload never replays an old token.
- `MenuPrompt` stays the precedent for "a thing in the chat that a click
  answers". It is not reused: a menu answers the runner's dialog, and a View
  calls the host.

### 9. Slack and headless turns

- **Slack** has no iframe. `apps/slack/relay.py` posts the agent's prose and not tool
  calls (:9-14), and that stays. A View-bearing result adds **one** line to the
  thread: the tool result's text `content` (§Server Behavior: tools "MUST return
  meaningful content array"). It is followed by "Open it to act:" and a deep link to the
  session on canopy. Slack never gets a send button, because Slack cannot present the viewer's
  grant.
- **Headless turns** need nothing. The runner produces the tool result and canopy-web
  renders the View for whoever opens the session.

## The host-app half: Labs (connect-labs, `connect_labs/mcp`)

Labs' MCP server is FastMCP 3.x with a bridged registry (`connect_labs/mcp/server.py`).

1. **Declare the extension.** Read the client's `io.modelcontextprotocol/ui`
   capability, and declare it in Labs' own `capabilities.extensions` (SEP-1724).
2. **One resource:** `ui://labs/workflow-action-preview`, `mimeType:
   text/html;profile=mcp-app`, one self-contained HTML5 document with inline JS
   and CSS. It sets **no `_meta.ui.csp`**, so the restrictive default applies,
   and `prefersBorder: true`. Labs MAY omit it from `resources/list` (§Behavior).
3. **`workflow_run_action`** (`connect_labs/mcp/tools/workflow_run.py:385-490`)
   gets `_meta.ui = {resourceUri: "ui://labs/workflow-action-preview",
   visibility: ["model", "app"]}`. Its text `content` stays a complete
   description, which is the fallback for every non-UI client.
4. **A new app-only tool, `workflow_action_preview_view`**
   (`visibility: ["app"]`). Its arguments are the same scope as
   `workflow_run_action`. It runs `preview()` **as the caller**, which is the
   viewer, and forces `include_image`. It returns `structuredContent`:
   `{label, summary, workers, needs, qa_redirect, image: {data_uri, caption},
   confirm, confirm_expires_in}`.
   - **The picture is inline:** a `data:image/png;base64,…` from `coach_image`,
     bounded (≤ 1 MB). The signed URL from `coach_image.attachment`
     (`coach_image.py:153-169`) cannot be used: the default CSP is
     `img-src 'self' data:`, and the URL opens only with a Labs session cookie, which
     the opaque View origin cannot hold.
   - **The token is the viewer's,** because `_digest` binds `user.pk`
     (`actions.py:608-612`). The agent's preview token is useless to the View,
     and the View's token never reaches the model. This keeps the current model:
     one preview, one token, bound to exactly what was shown.
   - **Scope:** add it to the `workflow:act` scope beside `workflow_run_action`
     (`connect_labs/mcp/tests/test_delegation.py:310` pins that map). A visitor's grant can then
     reach it, and nothing else can.
5. **The View:** on `tool-input`, it calls `workflow_action_preview_view` with the
   agent's `{run_id, action, arguments}`. It shows the picture, the opening message,
   the topics and three buttons:
   - **Send to <worker>** calls `workflow_run_action` with the preview's `arguments`
     and its own `confirm`. On success it calls `ui/update-model-context` with
     `{outcome: "sent", execution_id}` and then `ui/message` ("Sent — follow it with
     workflow_action_status").
   - **Send to QA user** is shown only when `qa_redirect` (Dimagi staff, one worker,
     `actions.py:578-597`). Labs holds no PersonalID username for a web user, and
     its own dialog asks for one (`components/workflow/ActionDialog.tsx:75-86,184`).
     The View asks the same way: an input, then a re-preview with `deliver_to`, so
     the token binds to it, then commit. Remembering the last value per user is a
     possible Labs follow-up.
   - **Not yet** makes no call. It sends `ui/update-model-context`
     `{outcome: "declined"}`.

   On mount, the View also shows recent executions for this run and action
   (`workflow_action_status`). A reload after a send then never looks unsent.
6. **Withhold the model's token when the UI is negotiated.** On a connection that
   negotiated the extension, a preview's model-facing result omits `confirm` and
   says the person confirms in the preview shown to them. Other connections
   (Claude Code without MCP Apps, Slack-driven turns) keep today's two-call flow,
   so the ACE stopgap keeps working there. Owner decision 2 covers whether to go
   further.

## Rollout

| Phase | Ships | Done when |
|---|---|---|
| **P1: host renders, click acts** | Extension negotiation; UI tool index; visibility in `site_tools`/`site_call`; the §3 matching rule; `resolve_for_viewer` and gates 1-7; the four routes; sandbox origin and proxy; `AppView` in ChatPage; `app_call` receipts and audit. Labs ships items 1-5 at the same time. | On canopy.dimagi.com, a session held on a Labs page shows the coaching picture. An editor with a live grant clicks Send to QA user and the conversation arrives. A viewer-role participant sees disabled buttons and gets a 403 if they force it. |
| **P2: embed panel** | `AppView` in `EmbedApp`, contact routes, narrow layout, four-level frame-ancestors | The motivating flow end-to-end in the Labs panel, for a user and for a contact. |
| **P3: Slack fallback** | One text line with a deep link in `relay.py` | A Slack-born coaching ask posts the preview text and a link, and no button. |

The ACE stopgap (`skills/coach-from-labs-page`, ace PR #2831) stays until P2 is
live. After that it keeps only the non-UI branch.

## Test plan

**Backend (pytest):**
- `site_tools` hides `["app"]` tools, and `site_call` refuses them.
- `_meta.ui` survives `list_tools`; the deprecated flat key is ignored.
- Each of gates 1-7 refuses with its own audit `error` code.
- Gate 3 refuses a call to another server.
- The viewer's grant is used, not the initiator's. The test uses a session where the
  two differ and asserts which grant's token was sent.
- A contact path and a viewer-role path.
- `agui.project` adds `metadata.canopy.app` only for indexed tools, on paths G and D.
- `api_tools` excludes the new routes. Extend the existing exclusion test.
- CSP builder: the default when omitted; no loosening; foreign domains dropped.
- The sandbox host serves the proxy only, sets no `Set-Cookie`, and 404s everything else.
- One `app_call` receipt per call; `ui/update-model-context` overwrite and size cap.
- `tests/test_architecture_boundary.py` stays green: everything lives in framework apps
  (`tokens`, `mcp`, `canopy_sessions`).

**Frontend (vitest):**
- The bridge handshake: proxy-ready, then resource-ready, then nothing to the View
  before `initialized`, then `tool-input` before `tool-result`.
- Messages with the wrong `source` or `origin` are dropped; `sandbox-*` messages are
  not forwarded.
- `hostCapabilities` omits `serverTools` for a viewer.
- `ui/open-link` rejects non-https.
- `AppView` remounts cleanly from history.

**Live (staging, a synthetic opportunity):**
- The motivating flow with `deliver_to` to a QA PersonalID. The model's transcript must
  never contain the View's `confirm`.
- A reload shows the receipt and no live Send.
- A Slack thread shows the fallback line.

## Security summary

| Threat (§Threat Model) | Mitigation here |
|---|---|
| Harmful HTML | Different-origin proxy; opaque inner origin; restrictive CSP narrowed to the site's own origins; sha256 logged |
| Sandbox escape to canopy | The proxy origin holds no cookies and serves one page; the inner frame has no `allow-same-origin` |
| Unauthorized tool execution | Gates 1-7: visibility, same server, ceiling, the viewer's own grant, the host's ACL; app-only tools also authorize themselves |
| Exfiltrating host data | `connect-src 'none'` by default; canopy passes the View only that call's input and result |
| Phishing | A labelled frame; https-only `open-link`; the click result is recorded by the host, not reported by the View |
| Audit (§Security 2) | Every View RPC goes through canopy REST. Every `tools/call`, `resources/read` and refusal is a `write_audit` row (`apps/mcp/audit.py:75`) naming the session, tool call, site, tool and viewer. Never arguments, never a token, matching `site_call` (`site.py:89-106`) |

## Owner decisions (decided, Jonathan, 2026-10-08)

These override the open questions this section used to list.

1. **Sandbox origin: no new DNS.** The proxy is served at a canopy sub-path,
   `/mcp-apps/sandbox/` (`MCP_APPS_SANDBOX_URL`, a setting, so a dedicated origin
   later is config only). It is isolated by framing it `sandbox="allow-scripts"`
   **without** `allow-same-origin`, which makes its origin opaque: no canopy
   cookies, storage or DOM. The response also carries
   `Content-Security-Policy: sandbox allow-scripts`, so the document is opaque even
   opened top-level (the walkthrough-content precedent,
   `apps/walkthroughs/streaming.py::SANDBOX_CSP`). **This deviates from §Sandbox
   proxy 1-2** ("different origins", proxy "MUST have `allow-same-origin`"). It is
   equivalent for isolation: what the standard buys with a separate origin is that
   the proxy and the View cannot reach the host's cookies, storage or DOM and are
   cross-origin to it, and an opaque origin is cross-origin to *every* origin,
   canopy's included. The cost: a View cannot use storage (`localStorage`,
   IndexedDB, cookies all throw or are empty under an opaque origin). No CORS or
   credentials are ever sent to the opaque origin (`Origin: null` is not a
   Connected site origin), and the host's postMessage check accepts the literal
   `"null"` origin ONLY together with `event.source === <that iframe>.contentWindow`
   — the source, not the origin, is the check.
2. **Clicks are the only way to send.** A model-visible preview never carries the
   commit token; the View holds its own, from its own app-only preview run as the
   viewer. Labs implements the withholding. canopy guarantees the View's app-only
   calls never reach the model: `site_tools` hides app-only tools, `site_call`
   refuses them (`not_allowed`, audited), the View routes are not MCP tools
   (`api_tools.EXCLUDED`, `_BROWSER`), and they refuse a PAT (what an agent holds).
3. **Views on both paths.** Path G (`site_call`/host_gateway) and path D (a tool
   result canopy only sees in the transcript, e.g. the owner's own `connect_labs`
   login in an emdash session). Rendered wherever canopy's chat shows the session:
   ChatPage and the embed panel (EmbedApp). Terminals and Slack get the text
   fallback plus a link to the session in canopy.
4. **No live grant: a read-only View** with a "Sign in to Labs" link. canopy does
   not ask the host for a grant in place.
5. **A Send click starts a short agent turn** (the View's `ui/message`, posted as
   the viewer), so the agent knows what was picked and Labs' result — and canopy's
   own receipt of the call rides the next turn's caller context. "Not yet" /
   decline only records a note (`ui/update-model-context`) the next turn sees.

## Implementation notes (as shipped)

- **`tool_call_id` is the tool call's own correlation id** (the `tool_use` block's
  `id`, which the result row carries as `tool_use_id`) — the one id that is the
  same in a live frame and after a reload. canopy's `Message` pk is not: a live
  row can carry a synthetic id before it is projected.
- **The UI tool index** is `AppCredential.mcp_apps_index`, refreshed by every
  listing canopy makes as somebody: the gateway (`site_tools`, `site_call` when the
  index is older than ten minutes) and every View call (which lists as the viewer).
  It also caches each fetched View (sha256, effective CSP) for the read-only case.
- **Receipts** are written for View calls to tools the MODEL could also call (a
  commit such as `workflow_run_action`), not for app-only helpers (a preview runs
  on every mount and would bury the one receipt that matters). Every call, receipt
  or not, is an audit row.
- **The inner View frame is `sandbox="allow-scripts"`** (no `allow-forms`): a
  nested frame cannot hold more than the proxy's flags, and a View submits through
  JS. CSP adds `form-action 'none'` and drops `'self'`, which under an opaque origin
  could only ever name canopy's own URLs.
