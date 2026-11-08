# Embedding, the SDK family and host grants — canopy's side

What canopy-web maintains so a host product can embed an agent and the agent can
act on that host as the visitor: the published SDK packages and how they ship,
the embedded widget, the MCP Apps host, and the checks that prove the chain live.

Not this doc:
- **What a host builds** (registering an app, the token endpoint, the script tag,
  the page contract, the ACL hop by hop) → [`embedding-a-canopy-agent.md`](embedding-a-canopy-agent.md).
  It is pinned by `tests/test_embedding_doc_is_true.py`; change it only together
  with the code it describes.
- **The host grant wire contract** (ID-JAG → jwt-bearer + `private_key_jwt` +
  DPoP → `site_call`) → [`sdk/python/README.md`](../../sdk/python/README.md#host-grant-contract-v1)
  (`host-grant-contract.md` is a pointer to it).
- **The MCP server itself** (generated tools, auth, `site_tools`/`site_call`) →
  [`mcp-surface.md`](mcp-surface.md).

## The SDK family

One **contract version** across several packages; they share the contract, not
code, because their consumers and dependencies differ (browser npm code vs. a
Python server library). Where each lives: `frontend/packages/{canopy-ui,canopy-client,canopy-widget}`
and `sdk/python`. `canopy-widget` is **not published**: canopy builds and serves it at
`/embed/widget.js` (`config/urls.py`).

### npm packages

- **Never scope them.** The `@canopy` npm scope belongs to someone else, so a
  `@canopy/*` name can never be published. `canopy-ui`, `canopy-client` and
  `canopy-widget` are unscoped deliberately.
- **Touching `frontend/packages/{canopy-ui,canopy-client}/src` requires a version
  bump in that package's `package.json`.** The `Frontend build` job (a required
  check, `.github/workflows/ci.yml` → "Published packages need a version bump
  when their source changes") diffs `src` against the PR base and fails if the
  version did not move — otherwise npm keeps serving the old code under the old
  number and every pinned consumer silently misses the change. `canopy-widget`
  is exempt: it is served, never installed.
- **Merging the bump is the release.** `publish-canopy-ui.yml` /
  `publish-canopy-client.yml` run on a version change reaching `main`, publish
  if npm does not already have that version (OIDC Trusted Publishing, no stored
  token — a new package needs a one-time Trusted Publisher setup on npmjs, see
  the workflow header), and write the `<pkg>-v<version>` tag. Don't push a tag
  by hand as the release step.

### `dimagi-canopy` (Python)

Import name `canopy_sdk`; modules `.contract`, `.host`, `.consumer`, `.django`
(app label `canopy_host`), `.conformance` — each module's docstring says what it is for.

- **canopy-web imports `.contract` / `.consumer`** in
  `apps/tokens/{assertions,client_identity}.py` and `apps/host_grants/{host_grants,host_gateway}.py` — never
  keep a copy of a contract value in canopy-web. `tests/test_sdk_round_trip.py`
  runs the SDK's real host half against canopy's real arrival, redemption and
  `site_call`, so the two halves cannot drift.
- `.host` assumes nothing about host ≠ canopy (issuer, resource and client id
  are configuration); canopy-web mounts it to host its own MCP
  (`apps/host_grants/self_host.py`, `apps/mcp/delegation.py`).
- **The import name `canopy_sdk` is fixed; the distribution name may change**
  (`canopy-sdk` on PyPI is an unrelated project). A rename must only ever touch
  a host's requirements line.
- **Not on PyPI.** A version bump in `sdk/python/pyproject.toml` reaching `main`
  makes `release-dimagi-canopy.yml` tag `dimagi-canopy-v<version>` and attach
  the sdist + wheel to a GitHub Release; hosts pin
  `dimagi-canopy @ git+https://github.com/dimagi-internal/canopy-web@dimagi-canopy-v<version>#subdirectory=sdk/python`.
- **Touching `sdk/python/src` requires that bump** (and `canopy_sdk.__version__`
  + `CHANGELOG.md`) — guarded in the `Runner packages` job (which also runs the
  SDK's own suite), required through the `Backend tests` gate.

## The embedded widget is canopy's chat page

Which agent it opens on is ranked in one place, `apps/embedding/defaults.py`
(its docstring has the order); the frame's own fallbacks are `frontend/src/embed/pickAgent.ts`.

`frontend/src/embed/EmbedApp.tsx` mounts the same `ChatPanel` (canopy-ui/chat)
over the same `ag-ui` protocol as `ChatPage`. When the widget looks or behaves
worse than canopy-web, the cause is a **seam** — a prop or call `ChatPage` makes
and the embed does not — not a component. Compare against `ChatPage.tsx` for
`renderMarkdown`, `disabledReason`, `emptyState`, `awaitingReply`, and:

- A `banner` carrying `MenuPrompt` — on `activity: blocked` the panel withdraws
  the working bubble, and without a menu a blocked agent looks like a finished
  conversation. (Contacts get the question as words: answer-menu is not on
  `/api/contact/`.)
- **A REST send must call `socket.noteLocalSend(text, clientId)`.**
  `awaitingReply` is raised only by `sendChat()` or `noteLocalSend`, and the
  person's own line is not a server row until the agent's transcript ships it
  back — so a first message sent over HTTP (the session does not exist yet)
  otherwise vanishes into an empty box. Don't track the HTTP request as the
  "waiting" signal: it ends long before the reply.

### Opening a conversation

- **Every way into a conversation is read as the visitor first** — the page's
  `widget.openSession`, a row of the start screen's list, and a chat whose socket
  was refused all go through one check in `EmbedApp` (`openRequest`): an agent the
  site does not offer, or one canopy will not give, is "isn't available here"; a
  private one they are not in is canopy's ask-to-join interstitial. Never hand an
  id straight to the socket.
- **A refused socket is an answer.** `useSessionSocket` stops reconnecting on close
  `4003`/`4004` (`REFUSAL_CLOSE_CODES`) and reports `refused`; the panel then says
  so instead of a chat that reads "Reconnecting…" forever.
- **The list is your conversations**: rows with `you_are_in_it === false` (a
  co-tenant's private conversation, listed so they could ask to join) are left out,
  and rows are told apart by **metadata only** (`conversationLabels.ts`) — never
  their words, which canopy cannot read.

### Styling and chrome

`frontend/packages/canopy-widget/src/theme.ts` and `chrome.ts` (comments there give the rules):

- The launcher is host DOM in a shadow root, themable only through `--canopy-*`
  properties, `::part(...)` or `canopy.init({ theme })`. The panel is canopy's own
  document in an iframe, reachable only through `theme`, which is **re-validated
  inside the frame** with the same `normalizeTheme`: the loader's check runs in the
  host's page, where a raw `postMessage` can skip it.
- `theme.mode` goes in the iframe URL and is rendered by the server
  (`apps/embedding/views_embed.py::_theme_mode`); applying it from JS flashes the wrong theme.
- Panel size, expanded state, snap and resize are chrome state kept per `app`
  through the injected `storage` (`chrome.ts`, `dragging.ts`: `MIN_PANEL`, `syncReserve`).
  A host's own `expand()` / `initialDisplay` is not remembered.
- `tests/test_embedding_doc_is_true.py` fails if the host guide documents a
  variable, part or field the code does not read.

## A site sign-off in the frame (`canopy.signOff`)

The widget's second entry point, beside `init`: `canopy.signOff(...)`
(`frontend/packages/canopy-widget/src/signOff.ts`) opens a modal holding
`/embed/sign-off` (`views_embed.embed_sign_off`, same framing policy as the chat),
rendering `SignOffFrame` instead of `EmbedApp`. No launcher, no chat, no act-as-you
permission: the frame mints a plain token (`{}`), shows the request (only the frame
of the site that asked), and approves with a passkey assertion made in the frame. It
posts `sign-off {id, state}` to the page and nothing else: the approval stays in
canopy for the site's server to read. Tests: `signOff.test.ts`,
`SignOffFrame.test.tsx`, `tests/test_site_sign_offs.py`.

## canopy as an MCP Apps host

canopy hosts MCP Apps (SEP-1865, `io.modelcontextprotocol/ui`). Start at the module
docstrings of `apps/mcp_apps_host/{mcp_apps,mcp_apps_views,views_mcp_apps}.py`; routes
are in `apps/canopy_sessions/app_views_api.py` and `apps/contacts/contact_api.py`.

- A Host site's tool may carry a **View** (`_meta.ui.resourceUri`, a `ui://`
  HTML resource), rendered sandboxed in `ChatPage` **and** the embed panel.
- **A View's clicks call the site as the person looking at it**
  (`mcp_apps_views.resolve_for_viewer`), never as the turn's initiator or the agent.
- Which rows get a View is decided server-side (`mcp_apps_views.annotate`) from the
  site's UI tool index, `AppCredential.mcp_apps_index`; a client never guesses.
- **App-only tools** (`_meta.ui.visibility: ["app"]`) are hidden from
  `site_tools` and refused by `site_call` (`host_gateway`, `mcp_apps.MODEL`). The View
  routes are not MCP tools and refuse a PAT (`app_views_api.py`), so a View's
  commit token can never reach the model.
- **The sandbox proxy is a canopy sub-path** (`MCP_APPS_SANDBOX_URL`) made opaque by
  `sandbox="allow-scripts"` with no `allow-same-origin` — a deliberate deviation from
  the standard's separate-origin rule (`views_mcp_apps.py` docstring). Views therefore
  cannot use storage. `McpAppsSandboxMiddleware` answers before sessions and auth, so
  it can never set a cookie. Don't add `allow-same-origin`.
- Sizing, display modes, host styles and the collapsed-header behaviour are
  `frontend/packages/canopy-ui/src/chat/mcpApps/` (`context.ts`, `hostStyles.ts`, `AppView.tsx`).
  In a sensitive conversation a View's header always names the tool, since the kept
  link's name is only its URI scheme (`sensitivity.strip_result`).

Tests: `tests/test_mcp_apps_{gateway,sandbox,views}.py`; frontend `mcpApps/{bridge,AppView}.test.ts(x)`.

## Act-as-you permissions (host grants): when, which, and Stop

A host grant (`tokens.HostGrant`) is, to the person, an **act-as-you permission**: a
time-limited permission they give in the moment, so the agent acts on the host's
page within their own access. The lifecycle is the docstring of
`apps/host_grants/host_grants.py`; the rules:

- **Minted only when the person acts, never on load.** The widget's load mint is
  plain (`{}` to the host's token endpoint; `apps/embedding/embed_api.py` for
  canopy's own pages). The frame (`frontend/src/embed/pageGrant.ts`) asks for one —
  `token-request` with `grant: true`, which the loader sends to the host as
  `{"grant": true}` — when the person sends, when a View sends a `ui/message`, and
  when a card in a conversation they open loads read-only or is refused `no_grant`.
  From then on every mint of that tab asks again (`HOST_GRANT_REMINT_SECONDS`,
  `apps/contacts/contact_api.py`) until they press Stop.
- **Unusable until confirmed with the person's passkey.** A redeemed grant has no
  `confirmed_at`; `host_grants.live`, `lookup` (→ `UNCONFIRMED`) and
  `host_gateway.context_for_grant` refuse it. **The passkey is asked for only when
  something needs the grant — a send never waits for it.** When the agent's turn
  calls the site and `lookup` says `UNCONFIRMED`, the gateway marks the page's
  waiting rows `requested_at` / `requested_reason` (`host_grants.ask`, carried across
  re-mints by `_inherit`), tells the agent to retry shortly
  (`host_gateway.page_grant_asked`) and **pushes** `host_grant.requested` (the grant id
  only) down the conversation's socket (`_push_ask` →
  `SessionConsumer.host_grant_requested`, which passes it only to the grant's own
  person); the panel opens `PageGrantConfirm` (`EmbedApp.tsx`). **Confirm with
  passkey** runs a WebAuthn assertion in canopy's frame against
  `POST /api/embed/host-grant/confirm/begin|finish` (`apps/host_grants/grants_api.py`),
  which `host_grants.confirm`s it. The host's token proves nothing about the person;
  the assertion is signed over canopy's origin, which the framing page cannot
  produce. The loader sets the frame's `allow="publickey-credentials-get"`.
- **30 minutes from the passkey, never extended.** `host_grants.confirm` caps the
  grant at `CONFIRM_SECONDS` after the touch (and never past the host token's own
  `canopy_sdk.contract.ACCESS_TOKEN_MAX_LIFETIME`). Each re-mint of the same
  page (`same_page`) inherits the newest live confirmation AND its end
  (`host_grants._inherit`), so renewing the token never prompts and never extends.
  **Testing the end of the window in production:** add `?canopy_debug_window=30` to the
  host page's URL; the widget copies it to the frame, the panel sends it as
  `window_seconds` with the assertion, and `host_grants.confirm` stores it
  (`HostGrant.window_seconds`). Only a SHORTER window is honoured (`window_for`), so the
  param widens nothing and needs no gate.
- **Never a contact's.** A contact has no passkey: `redeem` refuses a grant with no
  user (only the live probe's synthetic principal, `probe=True`, is exempt and is
  deleted with the probe), `contact_api` sends no ID-JAG on to it, and `lookup`
  finds none for a contact. An agent that serves the site on pages
  (`published_profiles.page_sites`) is left off a contact's list and cannot be started
  by one (`contact_api._for_contacts`); when that leaves no agent, the frame posts
  `unavailable` and the loader removes the launcher.
- **A conversation reads only through its own page's grant.** `host_grants.lookup`
  (the gateway's and Views' one lookup): with a session, only the grant
  `note_host_grant` (`canopy_sessions/services.py`) stamped for that person — by a
  send, a page-state/actions declaration, the create, or a View call — while it lives.
  Expired/pruned → `EXPIRED`, stopped → `REVOKED`, not confirmed → `UNCONFIRMED`,
  none → `NONE`; never another page's grant. The agent says
  `host_gateway.no_grant_message`. Outside a conversation, the latest LIVE grant
  (bound to the agent first, then unbound). A turn never reads through a grant the
  person did not give by acting in that conversation's page.
- **See and stop.** `host_grants.live_for` lists one row per page
  (`same_page`: same site, person, agent, scope); `host_grants.revoke` sets
  `revoked_at` on every live row of that page and wipes their tokens. Routes in
  `apps/host_grants/grants_api.py`: `/api/me/host-grants/` (Settings → Act-as-you
  permissions; by person, like a token — a site in a workspace you are not in can hold
  one for you) and `/api/embed/host-grant` (the panel's line, its prompt, and Stop). A
  Stop pressed on Settings can race one re-mint of an open tab, which then shows in
  both places to stop again.
- **A View's card button says it is the person's.** `host_gateway.view_call` carries
  `Canopy-View` on the View's own `tools/call` (when the listed tool's
  `_meta.ui.resourceUri` is that View), as a View's `resources/read` does. An agent's
  call never does.

## Live host-grant probe

Test connection's conformance stops where a visitor starts, because a real grant
needs an ID-JAG signed by the **host's** key; the live probe walks the rest.
The steps, and what it asserts, are the docstring of `apps/host_grants/live_probe.py`.

- It runs through the real code (`host_grants.redeem`, `host_gateway.context_for_grant`,
  `host_gateway.probe_call`), against a host's probe identity
  (`CANOPY_HOST["PROBE"]`, `canopy_sdk.host.ProbeHandler`) for one dedicated,
  low-privilege principal. It deletes the probe's grant and records **no**
  `host_grant.*` Event, so real-traffic health is never flattered.
- **On demand only**, from Test connection (`apps/embedding/host_sites_api.py`): each
  probe mints a real grant and makes real calls at a host. A host with no probe is
  "not set up" (`last_probe_ok` null), never a failure. A failure records a
  `host_probe.failed` Event and recovery `host_probe.recovered`, and the workspace's
  owners are pushed on the transition only.
- **canopy-web probes itself the same way**: `/oauth/probe`, the probe user from
  migration `tokens/0026_probe_user` (no password, no email, no membership), named by
  `CANOPY_HOST_PROBE_USERNAME` (`config/settings/connectlabs.py`); see `self_host.py`.
- **`self_host.config()` must not read the database by default**: the DPoP gate
  builds its verifier in async context. Only the probe endpoint and the
  metadata call `config(with_probe=True)`.

## Live end-to-end check: `scripts/e2e_embed_widget.py`

Drives a real deployment the way a browser does (loader, `/api/embed/self`, the
framable shell, a minted token, the same token listing agents, no session created
on load), because the embed seam's failures cannot be caught by a test that
constructs both ends. Read-only by default; `--send "text"` starts a real
conversation and costs real tokens. Its sibling for the chat page is
`scripts/e2e_session_chat.py`.
