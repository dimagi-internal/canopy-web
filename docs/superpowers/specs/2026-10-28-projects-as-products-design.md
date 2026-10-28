# Projects as products: connections, bundled views, connection calls, a model gateway

**Status:** design, not built. Decided with Jonathan, 2026-10-28.
**Reference project:** chrome-sales (Gmail ↔ Salesforce), rebuilt from scratch on canopy at
100% feature and UI parity. Its own design lives in the new repo; this document is the
canopy half: what canopy must be able to do so that a project like that needs nothing else.

## Why

chrome-sales is what a canopy project should be able to be: a set of capabilities
(Salesforce and Google rules), tools for agents, screens people use every day, and agent
workflows. Built before canopy could hold any of it, it mushed four things together: a
Chrome extension, a library of sales rules, per-browser credentials and API keys, and
agentic workflows. Each person pastes Salesforce credentials into a browser, AI keys are
copied around in 1Password notes, shared state lives in Google Sheets, and agents read
`.sf-creds.json` off disk.

canopy already has most of the parts: projects that serve a toolbox (`work.md`), workspace
apps (`workspace-apps.md`), dynamic views in a sandbox (`dynamic-views.md`), agents on
runners, an OAuth server for clients, and an MCP Apps host. Four things are missing.

## What changes in canopy's rules

These are deliberate reversals or extensions of rules written elsewhere. Each must be
recorded in the architecture doc that states the old rule.

1. **Third-party OAuth is first class and server-held; zero-knowledge is for pasted
   secrets.** A token canopy obtains from its own OAuth flow with a provider (GitHub
   today; Salesforce and Google next) is stored server-side, encrypted under the field key
   (`apps/common/encryption.py`), and refreshed only by canopy-web. A secret a person or
   agent *pastes* (API key, password, fine-grained token, CLI login) stays in the
   zero-knowledge vault. This is already the 2026-09-05 credentials rule ("anything
   canopy-web itself mints") and `GitHubConnection`'s shape; it becomes the stated rule in
   `vault.md`. Accepted cost: a compromised canopy-web can use every connected account.
   The delegated Google lending of #2239/#2259 is NOT changed by this document.
2. **canopy-web makes model calls for pages and clients** (the model gateway, below).
   Agent turns, chat replies and reviews still run on runners. `CLAUDE.md` § AI and
   `workspace-apps.md` ("canopy-web holds no model key") change accordingly. Precedent: HCP
   embeddings already call Vertex/Bedrock from canopy-web with no stored key and a checked
   zero-retention setting (`hcp-service.md`).
3. **A view may call a person's connections, as that person.** Reads freely within what
   the view declares; writes only as declared and only from a click. Mail and calendar
   reads need the viewer's consent to the view's publisher (see "What keeps it safe").
4. **A view may be a built bundle**, not only one JSX string, published from a project's
   repository. Same sandbox, same rules.

## Goals

- A project repo can ship: a library, agent tools, skills, agents, screens with their own
  UI (any framework, charts, drag and drop), a workspace app, and a Chrome extension — and
  canopy holds identity, credentials, data, model access and the published screens.
- No Salesforce or Google credential, and no model key, in any browser, extension or laptop
  file.
- One implementation of each domain rule, in the project's repo, running wherever the
  code runs (view sandbox, extension, runner).

## Non-goals

- canopy running project code on its servers. Domain code runs in the viewer's browser
  sandbox, in the extension, or on runners. (Revisit only if a server-side write path
  proves necessary; the escape hatch is a project-deployed tool server, not canopy
  hosting.)
- A Salesforce or Google client library in canopy-web beyond the generic request path.
- Changing how agents' own Google accounts (`gog-token`) or GitHub delegations work.

## 1. Connections

A **connection** is one person's OAuth grant to one provider, held by canopy-web.
Generalize `apps/github_connect` into `apps/connections`:

- `Connection(user, provider, account_id, instance_url?, scopes, refresh_token_enc,
  access_token_enc, access_token_expires_at, refresh_failed_at, connected_at)`, unique on
  (user, provider, account_id). `GitHubConnection` may migrate onto it later; not required.
- A **provider registry** in code (`providers/salesforce.py`, `providers/google.py`):
  authorize/token URLs, scopes, how to read the account identity, whether refresh tokens
  rotate, and the **request allowlist** (§3).
- Flows: `GET /auth/connections/<provider>/start` → provider consent (PKCE + client secret,
  `state` in session, single use) → `/auth/connections/<provider>/callback` → store →
  back to where the person started. Disconnect revokes at the provider, then deletes.
  Settings page lists a person's connections with last use and scopes.
- **Refresh**: canopy-web only, under `select_for_update` on the row, access token cached
  until 5 minutes before expiry — exactly `github_app.access_token_for`, because Salesforce
  rotates refresh tokens like GitHub. A failed refresh stamps `refresh_failed_at` and
  surfaces "reconnect Salesforce".
- **Salesforce**: one Connected App per deployment (`SALESFORCE_CLIENT_ID/SECRET`, login
  host configurable for sandboxes). Scopes `api refresh_token`. Connected App policy:
  refresh-token rotation on, session timeout short. **No IP restriction** (my call): canopy-web
  runs on Fargate with public IPs and no NAT (`deploy/aws/canopy-web.cfn.yaml`), so it has no
  stable egress, and runner/CLI tokens are used from other networks anyway. Adding a NAT with
  an Elastic IP later would allow restricting refresh to canopy-web only. Identity from the id URL (`/id/<org>/<user>`); `instance_url` stored.
- **Google** (for people's own Gmail and Calendar): canopy's existing Web OAuth client;
  scopes are what the reference project uses today: `gmail.readonly`, `gmail.compose`,
  `calendar.readonly`, `spreadsheets`, `drive.file`, `documents`. A provider connection may
  be granted incrementally (Google's `include_granted_scopes`) so a person adds Sheets only
  when a screen first needs it. Google refresh tokens do not
  rotate. This is a *person's* connection used by their own screens and their own CLI; it
  is unrelated to lending an agent a Google account.
- Every issue of an access token, and every connection call (§3), writes an audit event
  (who, provider, path class, view/client, status) — never a body or a token.

**Agents have their own connections** (decided by Jonathan). An agent that uses Salesforce
signs in as a dedicated Salesforce user — its own login, its own permissions, its writes
attributed to it. `Connection` therefore belongs to a person **or an agent**
(`user` / `agent`, exactly one). An admin of the agent starts the connect flow from the
agent's page and completes the provider's consent signed in as the agent's Salesforce user.
An agent's turn always uses the agent's connection, whoever started the turn; a person's
own CLI uses the person's. There is no fallback from one to the other.

**Project identities** (decided by Jonathan). Some work acts as the project, not as a
person: the reference project's shared Drive folder is owned through its service account
(`chrome-sales-sa@dimagi-chrome-extension`). canopy holds **no key** for it: canopy-web's
task role is exchanged through workload identity federation (as for Vertex in
`hcp-service.md`) and impersonates the service account (`iam.serviceAccountTokenCreator`
granted to the federated principal), minting a one-hour access token for the scopes the
identity declares. A project registers an identity (`ProjectIdentity(project, name,
provider="google-sa", principal, scopes)`, workspace admin approves); a project member's
CLI and a runner turn on the project may get its token; every issue is audited.

**Where an access token may go** — the only places one leaves canopy-web:

| To | Route | Rule |
|---|---|---|
| A runner turn | `POST /api/harness/runners/{rid}/turns/{tid}/connection-token {provider}` | New behaviour, modelled on `turn_github_token`'s checks (a turn this runner claimed and is executing, runner may hold the agent, refused for a caller-confined turn) but unlike it the token is server-held and handed out per call. The connection is **the turn's agent's own** (below). |
| A project member's CLI or a runner turn, for a project identity | `POST /api/projects/{ws}/{handle}/identities/{name}/token` | See "Project identities". |
| The person's own CLI | `POST /api/me/connections/{provider}/token` (PAT) | Their own token only. `canopy cred <provider>-token` writes it 0600 for stdio MCP servers. |
| Nobody else | — | Views, apps and the extension never receive a token; they go through §3. |

## 2. Bundled views

A **bundled view** is a dynamic view whose code is a built bundle: an `index.html` entry
plus hashed JS/CSS/font/image assets, produced by the project's build and published from
its repo.

- Model: `DynamicView.kind = "jsx" | "bundle"`; a bundle revision stores a manifest
  (`entry`, asset list with sha256, total size ≤ 10 MB) and its assets in canopy's object
  store. Revisions stay append-only; rollback as today.
- Served at `/dynamic-views/bundles/<view>/<rev>/…` by a middleware that, like
  `DynamicViewFrameMiddleware`, answers before sessions and auth and never sets a cookie.
  Frame: `sandbox="allow-scripts"`, never `allow-same-origin`. CSP: `default-src 'none'`,
  `connect-src 'none'`, `script-src`/`style-src`/`font-src`/`img-src` only that revision's
  asset path (+ `data:`/`blob:` images), `frame-ancestors` canopy's origins. No
  `unsafe-eval` needed (the bundle is precompiled).
- Runtime: the same postMessage protocol as JSX views (`protocol.ts`), exposed to bundles
  by a small published client, `@dimagi/canopy-view-client` (read, refresh, navigate,
  runAgentTask, and the §3 and §4 calls). No modals and no `<form>` submission in the
  sandbox; the client documents the replacements.
- Theming: the frame receives canopy's resolved tokens like an MCP Apps View; a bundle may
  use its own design system.
- **Publishing** (dynamic-view bundles): `POST /api/dynamic-views/{id}/bundles`, by a person
  or agent with edit rights on the view's project — normally the project's CI on merge to
  main. The view records the source repo and commit. App-page bundles (below) and view
  bundles share one asset store and one serving path; only how a revision is published
  differs.
- **Opening links, copying, downloading** without loosening the sandbox (my call): the
  frame gets no `allow-popups`, `clipboard-write` or download rights. Instead the bridge
  gains `canopy.openExternal(url)` (opens a new tab from the page, without the outside-link
  confirm, for origins the view or app declares, e.g. its Salesforce instance, Gmail, Docs),
  `canopy.copy(text)` and `canopy.download(name, mime, data)`, each needing transient user
  activation, done by the parent page.
- Where it shows: a project's Views section, its own page `/w/:ws/views/:id` (full width,
  with routes inside the bundle by hash), and inside the extension's side panel (§5).
- **A workspace app's page may be a bundle too.** `pages` entries in an app manifest take
  `bundle: <asset manifest>` as an alternative to JSX, published with the app version.
  **New:** today a workspace-owned template has only version 1 (made at install,
  `services.install`) and no route releases another. Add `POST
  /api/workspace-apps/{ws}/{key}/template-versions` (manifest + bundles), callable by the
  app's maintainers and by a project-scoped CI token; it creates a new template version and a
  **draft** on it, which goes live only when a maintainer publishes it (the existing draft →
  checks → publish path). CI never publishes. Manifests store structured settings (layouts,
  board configs) as `longtext` JSON; there is no json field type.
  An app page reads and writes the app's collections (as today) and, with this spec,
  declares connection calls and models like a view. The reference project's screens are
  pages of its workspace app, so its state and its screens ship together.

## 3. Connection calls from views and clients

A view (JSX or bundle) and a signed-in client (the extension) call a person's connection
through canopy-web, which attaches the token server-side:

`POST /api/connections/{provider}/request {method, path, query?, body?}` → the provider's
status and JSON body.

- **As the caller only.** The viewer's own connection; there is no way to name another
  person's. No connection → 409 `not_connected` with the connect URL.
- **Allowlisted paths per provider**, matched on method + path pattern:
  - Salesforce: `GET query`, `GET search`, `GET sobjects/*/describe`,
    `GET|POST|PATCH|DELETE sobjects/{type}/{id?}`, `GET|POST analytics/reports…`,
    `GET analytics/dashboards…`, `POST actions/custom/flow/{name}`,
    `POST apexrest/{name}` (an org's own Apex REST, e.g. Dimagi's `convert-lead`; each name
    is declared by the view like a flow), `GET chatter/users/me`, `GET limits`,
    `GET ui-api/…` (picklists, layouts). Composite is out until needed.
  - Google: Gmail `users/me` (messages list/get, threads get, profile, drafts create, send
    for replies) and the Gmail batch endpoint, Calendar `calendars/primary[/events]`,
    Sheets v4 values/batchUpdate, Drive v3 files (create, get), Docs v1 documents
    get/batchUpdate — nothing else.
- **Declared by the view.** A view declares `connections: [{provider, reads: [...],
  writes: [...]}]` — writes named as `{method, path pattern}` (for Salesforce, object +
  operation: `create Event`, `update Lead`, `flow Create_Lead_From_Contact`). Checked
  when the view is saved; the bridge refuses anything undeclared before it reaches the
  server, and the server checks again against the revision.
- **Writes need a click — one click opens one operation** (my call). Real flows are several
  writes after awaits (logging one email is anchor Task → EmailMessage → relations → Lead
  Task; bulk logging repeats that per person; create waits on a duplicate check). So a click
  (transient user activation in the frame, forwarded with the message) asks the page for
  an **operation lease**: `{operation_id, declared write set, expires 120 s}`; writes carry
  the operation id and are accepted while it is live, for that view revision, up to a cap
  (e.g. 500 writes). Writes the model proposes (a copilot's tool call) open no lease by
  themselves: the page shows the proposed write as a confirm chip and the person's click
  opens it. Every write is audited with the view, revision and operation id.
- **Rate-limited** per person per provider; large responses capped (e.g. 5 MB).
- The extension uses the same route with its canopy OAuth token (§5); its scope set is the
  extension's declared list, approved once by the person at sign-in.

## 4. The model gateway

Prior art to port, not invent: chrome-sales's `server/` ("dimagi-ai-proxy", Cloud Run behind
IAP) already does this for one project — verifies the person, per-user rate limit, daily USD
spend cap, request-size cap, model allowlist, request-shape sanitising, content-free logs,
Vertex with its own identity. The gateway is that, owned by canopy and shared by every
project.

`POST /api/models/complete` — streamed — for views and clients:
`{model, system?, messages, tools?, max_tokens, temperature?}`.

- **Providers through canopy's cloud identity**, no stored keys where avoidable: Vertex via
  the task role's workload-identity exchange (as HCP embeddings), Bedrock via the task
  role. An Anthropic API key in the field-encrypted settings is the fallback. Which models
  a workspace may use is a workspace setting (`models.allowed`); default off.
- **Zero retention checked, not assumed** — reuse `embeddings.retention_ok` per provider.
  Exception (decided by Jonathan): **grounded calls** (Gemini Google Search grounding),
  which Vertex's zero-retention terms exclude, are allowed when the workspace turns
  `models.grounding` on; they are marked not-zero-retention in the audit.
- **Who may call**: a member of the view's workspace (editor+ by default; configurable),
  or a signed-in client for the person. Per-person and per-view daily token budgets; a
  budget hit is 429 with the reset time.
- **Metered and audited**: tokens in/out, model, person, view or client, latency. Shown in
  workspace analytics. Prompts and outputs are not stored.
- A view declares `models: ["claude-sonnet-…"]`; undeclared models are refused.
- **Tool use** is the caller's loop: the gateway returns tool calls, the view runs them
  (through §3 or its own code) and calls again. The gateway runs no tools — except
  provider-hosted ones it allowlists, such as Gemini's Google Search grounding, which the
  reference project's auto-fill uses by default.
- Initial models: what the reference project runs today (Gemini on Vertex, default
  `gemini-2.5-flash`); Claude through Bedrock or Vertex where the account allows it.

## 5. Clients: the extension

A Chrome extension is a **client of canopy**, not a holder of credentials:

- Signs in with canopy's existing OAuth server (`/oauth/register`, PKCE public client,
  `chrome.identity.launchWebAuthFlow` redirect). Token stored in `chrome.storage.session`;
  refresh rotates as for MCP clients. **New:** today that server grants only the scope
  `canopy` (a full personal token; requested scopes are ignored, `mcp_oauth.USER_SCOPE`). A
  registered client gets a narrower scope, `client:<project>` — §3, §4, the project's app
  reads/actions and `/api/me/` only — enforced in token auth.
- Calls §3 and §4 and canopy's own REST API with that token. Holds no Salesforce or Google
  token and no model key.
- May frame bundled views (the side panel embeds the project's views) — the frame
  ancestors must then include the extension's origin, registered on the project
  (`ProjectService` kind `client`, with the extension id), approved by a workspace admin.

## 6. Projects list what they ship

New `ProjectService` kinds next to `kb`, `mcp`, `skill`, `plugin`:

- `app` — a workspace app installation (`AppInstallation`) the project ships; the project
  page links it, and the app's template can name its source repo.
- `client` — a registered client (Chrome extension id, its store URL), as in §5.
- Bundled views already belong to a project via `DynamicView.project`.

`open_project` lists these with how to reach them.

## What keeps it safe

- **Domain code never runs on canopy-web.** It runs in an opaque sandbox with no network,
  in the extension, or on runners. canopy-web runs only generic provider requests against
  an allowlist.
- **A view can do only what its viewer can, and only what it declared.** Writes need a
  click. The viewer's own connection is the ceiling — the same exposure the extension
  has today, minus stored credentials.
- **What a view is shown, its publisher can see** (`dynamic-views.md` § What is left). For
  CRM data inside one workspace that is acceptable. For **mail and calendar**, the viewer
  consents per publisher the first time a view asks ("Sales screens, published from
  dimagi-internal/canopy-salesforce, may read your Gmail"), revocable in settings. Bundles from a registered
  repo record their commit, so what ran is reviewable.
- **Tokens leave canopy-web only to runner turns and the person's own CLI.**
- **Model spend** is bounded per person and per view and visible.
- **Compromise of canopy-web** exposes every connection and the model budget; accepted for
  OAuth connections under rule 1, mitigated by IP restriction (Salesforce), scopes,
  provider-side revocation and the audit trail.

## Build order

1. Connections: model, Salesforce provider, connect/disconnect, refresh, settings page,
   runner-turn and CLI token routes, `canopy cred salesforce-token`.
2. Connection calls (§3) for Salesforce, with declarations and click-gated writes, in
   JSX views first.
3. Bundled views (§2) and `@dimagi/canopy-view-client`.
4. Google provider + consent per publisher.
5. Model gateway (§4).
6. Extension as client (§5), `ProjectService` `app` and `client` (§6).

Each step is usable on its own: after 1, agents use Salesforce through stdio tools; after
3, the reference project's screens can move.

Per-person records need nothing new: a collection's `scope` (`apps/workspace_apps/scope.py`)
already gives record-level access through a `private_field` naming the owner.

## Open questions (Jonathan's)

1. Where the reference project lives in canopy (workspace and project handle).
## Left to the builder

- Storage backend for bundle assets (S3 bucket already used by canopy vs a new prefix).
- Exact budget defaults and rate limits.
- Whether `GitHubConnection` migrates onto `Connection` now or later.
- The `@dimagi/canopy-view-client` package's home (canopy-web `frontend/packages/` vs
  `sdk/`).
