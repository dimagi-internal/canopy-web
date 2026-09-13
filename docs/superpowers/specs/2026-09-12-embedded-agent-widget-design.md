# Embedded agent widget — canopy as a cross-product chat substrate

**Status:** SUPERSEDED by
[`2026-09-12-embedded-agent-widget-v2-design.md`](2026-09-12-embedded-agent-widget-v2-design.md)
· **Date:** 2026-09-12 · **Author:** Jonathan + Claude

> **Superseded the same day it merged.** Reading the target codebase invalidated
> §2 (one agent per host), §3 (`npm install canopy-ui` — labs is React 18, and
> is not a React SPA) and §5 (deferring a generic embed). Kept as the record of
> how the design got there; v2 is the current one.

> Generalizes the ace-web ↔ canopy-web chat cutover
> (`2026-07-25-ace-web-canopy-chat-cutover-design.md`) into a repeatable pattern
> for *any* host product, and proposes connect-labs as tenant #2 — with its
> dynamic Workflow engine as the first "agent acts on live page state" use case.

## Where this comes from

ace-web already proved most of the hard part of "embed a canopy agent in a
third-party product": a host app's backend holds a registered canopy
`AppCredential`, trades it (+ the signed-in user's email) for a short-lived
per-user `DelegatedToken` via `POST /api/auth/token-exchange`, and the host's
browser then talks to canopy-web **directly** — REST + WebSocket — using that
token. The host never touches session/message/turn state; canopy owns all of
it. Session scoping per host is a `metadata.origin_key` convention
(`{host}:{workspace}`) that canopy's session-list filters on. The interactive
chat UI itself ships as `canopy-ui/chat` (`ChatPanel`, `useSessionSocket`,
`PlacementBanner`), so a host with a React frontend gets a drop-in widget, not
a from-scratch build.

None of that is ace-specific. It's a general "embed a canopy agent in your
product" recipe:

1. Register an `AppCredential` for the host (allowed email domains,
   `provision_workspace`/`provision_role` for JIT membership).
2. Host backend: one identity-brokering endpoint (token-exchange passthrough)
   + one session-create convenience that stamps host-derived metadata
   server-side (never client-supplied, so one tenant can't stamp another's
   `origin_key`).
3. Host frontend: mount `canopy-ui/chat`, wired to the delegated token.

This is the "SDK" half of the original vision. What ace-web's integration does
**not** yet cover — because ace-web's Workbench doesn't need it — is the other
half: an agent that can *read live, possibly-unsaved page state* and *take
actions back on the host page*, not just receive a static title/metadata
string at session-create time.

## Why connect-labs, and why now

connect-labs' Workflow engine is close to an ideal first host for that second
half. Workflow templates are Python-defined (`connect_labs/workflow/templates/*.py`)
exporting `RENDER_CODE` — a React JSX string, transpiled in-browser by Babel —
which receives `{definition, instance, workers, pipelines, links, actions,
onUpdateState, view}` as props at render time. That means, for any workflow
instance a user has open:

- **Read access to live state already exists as props**, not just persisted
  DB rows — `instance`, `workers`, `pipelines`, `view.state` reflect exactly
  what's on screen, including in-progress state a saved-runs template hasn't
  frozen yet.
- **Write access already exists as callback props** — `onUpdateState` and
  `actions` are host-provided functions a component calls to mutate the
  workflow. Exposing one of these to a canopy agent as a callable tool is a
  much smaller step than inventing a new action-dispatch mechanism.
- Workflows are already edited live by AI today (the `workflow-author`
  MCP flow: pull → edit JSX → push) — so "an agent reasoning about a workflow
  instance" is an established mental model here, not a new one.

This is a materially better fit for "agent looks at a dynamic workflow" than
connect-labs' existing `workflow_*` MCP tools (`workflow_get`,
`workflow_preview_as_of`, `pipeline_sql`, …) alone: those read *persisted*
workflow definitions/snapshots, but the render props are the only place that
sees a user's current, possibly-unsaved on-screen state.

## Decision: v1 scope is single-host, read+one-action; cross-system bridging is deferred

Two things the original conversation raised — live page-context injection and
bridging two external systems in one conversation — are not equally mature to
build:

- **Page-context injection** has a concrete host and a concrete prop surface
  (above) to build against today.
- **Cross-system bridging** (one canopy session reasoning across, say,
  connect-labs *and* ace-web) has no concrete use case yet, and the harder
  design problem (a credential resolver keyed by tenant+system, since each
  external system has its own auth boundary) has no standard to lean on — the
  research into A2A / multi-tenant MCP patterns found this is still largely
  ad hoc industry-wide. Building it speculatively now, with nothing real to
  validate it against, risks designing the wrong abstraction.

**v1 ships single-host context + one host action. Cross-system bridging is an
explicit non-goal for this spec**, revisited once a second concrete
consumer needs it.

## Shape of the work

### 1. canopy-web: register connect-labs as a tenant (no new code)
Same as ace-web: a new `AppCredential` (name `connect-labs`, allowed domains,
`provision_workspace`/`provision_role`), its raw value in connect-labs' AWS
Secrets Manager. Confirms the *tenanting* mechanism is truly host-agnostic —
if this needs canopy-side code changes, that's a finding, not expected.

### 2. connect-labs: identity-brokering surface (mirrors `ace-web/apps/canopy/`)
- `client.py` — `exchange_token(email)`, `create_session(user_token, *,
  title, metadata)`. Same two calls, same stdlib-urllib shape.
- API router — `GET /labs/canopy/status`, `POST /labs/canopy/token`,
  `POST /labs/canopy/sessions` (stamps `origin_key = f"connect-labs:{workspace}"`
  or equivalent server-derived scope — connect-labs' tenancy unit, not the
  request body).
- Settings: `CANOPY_BASE_URL`, `CANOPY_APP_CREDENTIAL`, `CANOPY_PUBLIC_BASE_URL`,
  `CANOPY_WORKSPACE`, `CANOPY_AGENT_SLUG` (a labs-specific agent, not `ace`).

### 3. connect-labs: mount `canopy-ui/chat` in the Workflow view
A `CanopyChatPanel` (ace-web's is the reference implementation) docked in the
workflow instance page. `createCanopySession` is seeded with:
- `metadata`: `{workflow_id, template, opportunity_id}` (mirrors ace-web's
  `opp_slug`/`opp_run_id` convention).
- **A context snapshot as the session's opening turn**, not just metadata:
  serialize the subset of `{definition, instance, workers, view.state}` the
  template's author opts into (a `context_manifest` similar in spirit to
  saved-runs' `snapshot_inputs`) so the agent starts the conversation already
  knowing what's on screen. This is intentionally the *simple* version of
  "page-context injection" — a snapshot at session-open — not a live,
  continuously-synced readable-state channel. Continuous sync is real future
  work (the CopilotKit `useCopilotReadable` pattern is the reference shape)
  but isn't needed to prove the concept.

### 4. connect-labs: one agent-callable action
Expose `onUpdateState` (or a narrow, template-declared subset of `actions`)
as a tool the labs-side canopy `Agent` can invoke, gated the same way
`workflow_update_definition` already gates writes today (permissions, audit).
Start with **one** template (a low-stakes one, not a prod-critical workflow)
to prove the round-trip: agent reads instance state → proposes a change →
calls the action → the live page updates.

### 5. Skip: cross-system bridging, continuous state sync, non-React embed
Explicitly out of scope for v1 — see Decision above. A generic
script/iframe embed for non-React third parties is also deferred; both
current hosts (ace-web, connect-labs) are React apps that can `npm install
canopy-ui`.

## Non-goals / risks

- **Not** replacing connect-labs' `workflow_*` MCP tools — those remain the
  right surface for persisted/historical workflow data. This spec adds a
  *live, in-session* surface for the instance currently open in the browser.
- **Security**: an agent with a write action into a live workflow is a new
  blast radius on shared, real infrastructure (labs). v1 deliberately starts
  on one low-stakes template and one narrow action, gated behind the same
  review/audit path `workflow_update_definition` already uses — not a
  general "agent can call any action" surface.
- **Deploy gate**: connect-labs enforces PR + merge-to-`main`-only deploys
  (no branch deploys) — this spec's implementation lands as reviewed PRs;
  testing on labs happens only after merge + an explicit deploy trigger.
- Canopy's own tenancy isolation residual (noted in the ace-web integration:
  session-list is origin-scoped, but direct-by-id `GET` is not) applies here
  too and is not re-solved by this spec.
