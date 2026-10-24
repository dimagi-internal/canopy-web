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
  (`host-grant-contract.md` is a pointer to it). Design:
  `docs/superpowers/specs/2026-09-26-embedded-caller-delegation-design.md`.
- **The MCP server itself** (generated tools, auth, `site_tools`/`site_call`) →
  [`mcp-surface.md`](mcp-surface.md).

## The SDK family

One **contract version** across several packages; they share the contract, not
code, because their consumers and dependencies differ (browser npm code vs. a
Python server library).

| Package | Where | What |
|---|---|---|
| `canopy-ui` | `frontend/packages/canopy-ui`, npm | Design-system kit + the chat kit (`canopy-ui/chat`: `ChatPanel`, `MenuPrompt`, `PlacementBanner`…), consumed by canopy-web and ace-web. |
| `canopy-client` | `frontend/packages/canopy-client`, npm | Layer 1: framework-free transport — token cache, REST, session WS URL, host bridge. |
| `canopy-widget` | `frontend/packages/canopy-widget`, **not published** | Layer 3: the drop-in loader, built and **served** by canopy at `/embed/widget.js` (`config/urls.py`). |
| `dimagi-canopy` (import `canopy_sdk`) | `sdk/python`, GitHub Releases | The server side of host grant contract v1. |

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
- ace-web tracks the newest versions through Dependabot (daily, canopy packages
  only) plus an auto-merge gated on its frontend build.

### `dimagi-canopy` (Python)

`canopy_sdk.contract` (the wire values, defined once), `.host` (what a site
runs: sign the assertion + ID-JAG, the page registry, the jwt-bearer
`GrantHandler`, the MCP-side DPoP `ResourceVerifier` / `DPoPGate`,
`ProbeHandler`), `.consumer` (canopy's half), `.django` (optional wiring, app
label `canopy_host`), `.conformance`.

- **canopy-web imports `.contract` / `.consumer`** in
  `apps/tokens/{assertions,host_grants,host_gateway,client_identity}.py` — never
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

`frontend/src/embed/EmbedApp.tsx` mounts the same `ChatPanel` (canopy-ui/chat)
over the same `ag-ui` protocol as `ChatPage`. When the widget looks or behaves
worse than canopy-web, the cause is a **seam** — a prop or call `ChatPage` makes
and the embed does not — not a component. Compare against `ChatPage.tsx`:

- `renderMarkdown` — without it `MessageItem` falls back to plain text and
  replies show literal `**bold**`.
- A `banner` carrying `MenuPrompt` — on `activity: blocked` the panel withdraws
  the working bubble, and without a menu a blocked agent looks like a finished
  conversation. (Contacts get the question as words: answer-menu is not on
  `/api/contact/`.)
- `disabledReason`, `emptyState`, `awaitingReply={socket.awaitingReply}`.
- **A REST send must call `socket.noteLocalSend(text, clientId)`.**
  `awaitingReply` is raised only by `sendChat()` or `noteLocalSend`, and the
  person's own line is not a server row until the agent's transcript ships it
  back — so a first message sent over HTTP (the session does not exist yet)
  otherwise vanishes into an empty box. Don't track the HTTP request as the
  "waiting" signal: it ends long before the reply.

### Styling: two halves, two doors

`frontend/packages/canopy-widget/src/theme.ts`:

- **The launcher** is host DOM inside a shadow root (a host's stray `button {}`
  cannot reach it). It is themable only through `--canopy-*` custom properties
  (the one thing that crosses a shadow boundary; `all: initial` does not reset
  them), `::part(launcher|dismiss|panel)`, or `canopy.init({ theme })`. Every
  launcher colour is `var(--canopy-*, <default>)`, so an unthemed host gets the
  default look.
- **The panel** is canopy's own document in an iframe, reachable only through
  `theme`, which rides the handshake and is **re-validated inside the frame**
  with the same `normalizeTheme` — the loader's check runs in the host's page,
  where a raw `postMessage` can skip it.
- `theme.mode` goes in the iframe URL (`?theme=`, allowlisted) and is rendered
  by the server (`apps/embedding/views_embed.py::_theme_mode`); applying it from JS
  flashes the wrong theme on load.
- The accent overrides canopy's own `--primary`. The font is set inline on
  `body`: `--font-sans` lives in a `@theme inline` block, so overriding the
  variable does nothing.
- `tests/test_embedding_doc_is_true.py` fails if the host guide documents a
  variable, part or field the code does not read.

## canopy as an MCP Apps host

canopy hosts MCP Apps (SEP-1865, `io.modelcontextprotocol/ui`). Spec:
`docs/superpowers/specs/2026-10-08-mcp-apps-host-design.md`. Code:
`apps/mcp_apps_host/mcp_apps.py`, `mcp_apps_views.py`, `views_mcp_apps.py`;
routes in `apps/canopy_sessions/app_views_api.py` and `apps/contacts/contact_api.py`.

- A Connected site's tool may carry a **View** (`_meta.ui.resourceUri`, a `ui://`
  HTML resource). canopy renders it sandboxed in `ChatPage` **and** the embed
  panel.
- **A View's clicks call the site as the person looking at it**
  (`mcp_apps_views.resolve_for_viewer`), never as the turn's initiator or the
  agent.
- Which rows get a View is decided server-side (`mcp_apps_views.annotate`, and
  the live `chat.tool_result` frame → `app` on the row) from the site's UI tool
  index, `AppCredential.mcp_apps_index`, refreshed by every listing.
- **App-only tools** (`_meta.ui.visibility: ["app"]`) are hidden from
  `site_tools` and refused by `site_call`. The View routes
  (`/api/canopy-sessions/{id}/apps/{tool_call_id}/…`; contacts under
  `/api/contact/sessions/…`) are not MCP tools and refuse a PAT, so a View's
  commit token can never reach the model.
- **The sandbox proxy is a canopy sub-path** (`MCP_APPS_SANDBOX_URL`, default
  `/mcp-apps/sandbox/`) made opaque by `sandbox="allow-scripts"` with no
  `allow-same-origin` — a deliberate deviation from the standard's
  separate-origin rule, recorded in the spec. Views therefore cannot use
  storage. It is answered by `McpAppsSandboxMiddleware`, which runs before
  sessions and auth, so it can never set a cookie. Don't add `allow-same-origin`.
- **Size and display modes come from the `AppHost`** (`canopy-ui` `mcpApps/context.ts`):
  `maxWidth` / `maxHeight` (default 900, floor 80) bound an inline View, and
  `fullscreen: true` advertises `availableDisplayModes: ["inline","fullscreen"]`
  and grants `ui/request-display-mode` → the same frame restyled `fixed inset-0`
  with a close control and Esc (never remounted; `ui/notifications/host-context-changed`
  tells the View).
- **A View draws in canopy's look**: `hostContext.styles.variables` carries canopy's
  tokens, resolved, under the standard MCP Apps names (`mcpApps/hostStyles.ts`; the
  accent goes as the `info` colours), and again on a theme switch. Without the flag the answer is always `inline`. ChatPage and
  the embed panel set it; the embed lifts `maxWidth: 420` and raises `maxHeight`
  while the panel is expanded.
- **A View's header names it and collapses it** (a button with `aria-expanded`).
  The title is its result link's `name` ("Query over …"), else the tool humanised;
  the raw tool name is the tooltip. `MessageList` names the newest View in the
  conversation (`LatestAppViewContext`): it opens expanded, every earlier one
  collapsed to one line (rows × columns, time, a row-cap cut-off, from the tool
  result's `structuredContent`; the link's `description` when only the link is kept;
  MCP Apps has no title or size notification). A person's toggle is kept per `tool_call_id` in
  `sessionStorage`. A collapsed View gets no frame until first expanded; collapsing
  hides the frame and never unmounts it.

Tests: `tests/test_mcp_apps_{gateway,sandbox,views}.py`; frontend
`mcpApps/{bridge,AppView}.test.ts(x)`.

### The expanded panel

Overlay and docked panels can expand to `inset: 16px` (the whole viewport under
640px). It is a STATE over the mode (`data-expanded` on the panel, `chrome.ts`),
so collapsing returns to the same overlay or rail; it uses `!important` because
a dragged position and a frame-requested height are inline styles that must
survive untouched. The frame asks (`expand`, frame→host) and the host answers
(`display`, host→frame) — the frame's button follows the answer. The visitor's
choice is remembered per `app` (`canopy.widget.display.<app>`) through the
injected `storage`; a host's `expand()` / `initialDisplay` is not. Esc collapses
from either document (a full-screen View takes the first Esc). Tests:
`canopy-widget/src/widget.test.ts` § expanding the panel, `src/embed/hostLink.test.ts`.

Overlay and docked panels have a resize grip (`part=resize`). Overlay: the
top-left corner, away from the bubble; the drag keeps the bottom-right fixed.
Docked: the same corner, with the rail on the window's bottom edge — shorter
than the window it gets `top: auto` and an inline height (`data-short`); at the
window's height the inline height is removed, so it is the 100vh CSS rail again. Sizes
stay within `MIN_PANEL` and the window (`dragging.resizePanel`), are remembered
per `app` (`canopy.widget.size.<app>`), and are reported to the host's
`onResize` (a docked rail overlays the page, so a host that reserved a column
follows it). A visitor-chosen size makes the frame's `resize` request a no-op.
Inline (host-sized) has no grip; expanded hides it.

A docked rail can be SNAPPED into the page (`chrome.ts` `syncReserve`): it pads
the host's `snapTarget` (default `document.documentElement`) by its width while
it is snapped AND open AND not expanded, restoring the element's own inline
`padding-right` exactly when any of those stops (or on `destroy`). Every path
that changes the rail calls the one `syncReserve`, which reads state rather than
being told. The frame asks (`snap`, frame→host); the host answers with
`display` carrying `snapped` (absent from an older loader = unchanged), and
`init` carries `snappable`/`snapped`. Remembered per `app`
(`canopy.widget.snap.<app>`). Tests: `widget.test.ts` § snapping,
`src/embed/{hostLink,EmbedApp}.test.ts(x)`. Tests: `dragging.test.ts` / `widget.test.ts` § resizing.

## Live host-grant probe

Test connection's conformance stops where a visitor starts, because a real grant
needs an ID-JAG signed by the **host's** key. The live probe walks the rest.

- A host with a probe identity (`CANOPY_HOST["PROBE"]`,
  `canopy_sdk.host.ProbeHandler`) signs a real ID-JAG for one dedicated,
  low-privilege principal and one read-only scope, for canopy's client only
  (`private_key_jwt` + DPoP; any other principal, tool or scope is refused;
  marked `canopy_probe: true`).
- `apps/host_grants/live_probe.py` runs the chain through the real code —
  `host_grants.redeem`, then `host_gateway.context_for_grant` +
  `host_gateway.probe_call` (the client `site_call` uses) — and asserts: the
  probe tool succeeds; an out-of-scope tool is refused **by the host** (canopy's
  own ceiling is opened to exactly the two tools); no proof, a stranger's proof
  and a plain bearer are refused. It then deletes the probe's grant and records
  **no** `host_grant.*` Event, so real-traffic health is never flattered.
- The verdict is stored on the site (`AppCredential.last_probe_*`). A failure
  records a `host_probe.failed` error Event coalesced per site, recovery a
  `host_probe.recovered` Event, and the workspace's owners (ancestors included)
  are pushed on the transition only — a site is not an agent, so there is no
  task to ask on.
- **On demand only**, from Test connection
  (`apps/embedding/connected_apps_api.py`): each probe mints a real grant and makes
  real calls at a host. A host with no probe is "not set up"
  (`last_probe_ok` null), never a failure.
- **canopy-web probes itself the same way**: `/oauth/probe`, the `canopy-probe`
  user from migration `tokens/0026_probe_user` (no password, no email, no
  membership), named by `CANOPY_HOST_PROBE_USERNAME` (set in
  `config/settings/connectlabs.py`); probe tool `list_fleet_tasks` (page
  `agent.tasks`, scope `tasks:read`), denied tool `skill_history`.
- **`self_host.config()` must not read the database by default**: the DPoP gate
  builds its verifier in async context. Only the probe endpoint and the
  metadata call `config(with_probe=True)`.
- Not built: an agent-turn leg (a drill making a real confined `site_call`).

## Live end-to-end check: `scripts/e2e_embed_widget.py`

The embed seam's failures (cookie names, the API base path, the tenant a session
is created in, a provider no module imports, a session cookie hiding the
frame's bearer, a session created on page load) cannot be caught by a test that
constructs both ends, so this script drives the real deployment the way a
browser does: the loader, `/api/embed/self`, the framable shell and its
`frame-ancestors`, a minted token, the **same** token listing agents — then
checks the count of empty sessions did not move (no create-on-load). Read-only
by default; `--send "text"` starts a real conversation and costs real tokens.
Its sibling for the chat page is `scripts/e2e_session_chat.py`.
