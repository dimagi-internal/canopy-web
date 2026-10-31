# canopy-web architecture — the framework/product boundary

> Design record: `docs/superpowers/specs/2026-06-24-canopy-framework-harvest-design.md`.
> This doc makes the one boundary the whole framework thesis depends on **legible**;
> `tests/test_architecture_boundary.py` makes it **enforced**.

## The one invariant

> **The dependency arrow is one-way: FRAMEWORK code never imports PRODUCT code.
> PRODUCT freely imports FRAMEWORK.**

- **FRAMEWORK** = the generic, *agent-agnostic* substrate any agent could reuse:
  identity, board, task-action queue, transcripts, auth, timeline, MCP server, API
  plumbing. It knows nothing about what a particular agent *does*.
- **PRODUCT** = canopy's *own* domain features — the self-improvement / DDD /
  portfolio surface. Another agent would not reuse these; they
  *are* canopy.

The payoff: the blend stays **cuttable**. We never physically split the repo, but
because framework never depends on product, the framework apps could be lifted out
onto a standalone host (or another agent) without dragging canopy's product along.
This is a **direction, not a wall** — we do not move apps into `framework/` vs
`product/` folders (that's the decomposition the design doc forbids); we just hold
the arrow's direction and enforce it in CI.

## The tiers

| App | Tier | What it is |
|---|---|---|
| `agents` | **framework** | Agent workspace: projects and tasks (an ask is a property of a task; *Waiting on you* is a filter), the five-action drain queue, syncs, skills. Agent-agnostic. |
| `agent_runs` | **framework** | Unified agent run-lifecycle: run → step → artifact → verdict/QA → decision → gate → fork, as a storage-agnostic read model behind a `RunStore` Protocol (DB adapter persists rows; Drive adapter reads ACE's YAML). FK's `agents.Agent`; imports no product app. |
| `harness` | **framework** | Agent-execution harness: runner registry, turn lifecycle + lease/claim, turn-event ledger (/api/harness). |
| `huddles` | **framework** | A team of agents syncing in rounds, led by one of them (canopy `huddle`). A huddle itself **stores nothing**: a read-only, DERIVED view (`/api/huddles`) over `harness` turns tagged `origin_ref.huddle` and `agents` tasks whose `source_url` is the huddle page. Agent-agnostic (any team, any huddle type); reuses `harness.api_turns.visible_turns_qs` + `turn_access` so it can never show more than the turn routes do. What the app does store is **project check-ins**: `ProjectCheckin`, a recurring meeting on one project (cadence, lead, the agents taking part, optionally a Slack channel the lead confirms each read of), and `CheckinRun`, a record of each time one came due (`checkins.py`, `/api/checkins`). |
| `threads` | **framework** | Agent threads — a bounded, moderated conversation between named agents (canopy `thread`). Stores only the frame and its limits (`AgentThread`: participants, moderator, parent, context, `max_messages`, `deadline_at`, status, outcome); each MESSAGE is a `harness` turn tagged `origin_ref.kind == "thread_message"`, its words DERIVED from the speaker's reply block like `huddles`. The limits are enforced where a message turn is created (`threads/guard.py`, called from `harness.services.enqueue_turn` — a framework→framework import). Reads reuse `visible_turns_qs` + `turn_access`. |
| `workspaces` | **framework** | Multi-tenancy: `Workspace` + members (owner/admin/editor/viewer) + email invites (domain-agnostic — no Drive coupling). The tenant that owns agents + runs. |
| `api` | **framework** (composition root) | The single NinjaAPI that wires every app's router (`api.py`): the one module allowed to import all apps. It also holds the shared API kit every app imports — `auth` (route auth), `errors` (RFC 7807 problems), `pagination` — plus `route_gates` and the `/api/w/{ws}/` resolver (`tenancy`). |
| `common` | **framework** | Shared infra: auth flow (allauth adapter, allowed domains), middleware, CSRF, request context/timing, email (SES), field encryption, Redis client. |
| `timeline` | **framework** | Generic activity-log aggregation; reads other apps' events via a string registry (no hard product imports). |
| `tokens` | **framework** | How anything that is not a browser session reaches canopy: Personal Access Tokens and bearer auth (`principal.resolve_bearer`, the middleware) and the CLI loopback mint (`/auth/cli/authorize/`, `create_token`); WebSocket tickets; and the credential models — `PersonalToken`, `DelegatedToken`, `ContactToken`, `AppCredential` (+ `AppCredentialAgent`), `HostGrant`, `EmbedAuditLog`, `HcpFrameProof` — kept together so no table moved. Everything that USES them lives elsewhere: connected sites and embedding in `embedding`, the contact surface's routes in `contacts`, the MCP sign-in in `oauth_server`, MCP Apps hosting in `mcp_apps_host`, host grants and self-host in `host_grants`, a person's GitHub connection in `github_connect`. |
| `oauth_server` | **framework** | The OAuth authorization server MCP clients sign in with (`/oauth/register`, `/oauth/authorize`; `mcp_oauth.py`, protocol rules in `common/oauth_core.py`): `OAuthClient`, `OAuthAuthorizationCode`, `OAuthGrant` (tables kept from when they lived in `tokens`), and a person's connected MCP clients (`/api/tokens/connected-apps`, operationIds keep their `tokens_` label). An access token it mints is a `tokens.PersonalToken`. |
| `mcp_apps_host` | **framework** | canopy as an MCP Apps host (SEP-1865): which of a site's tools carry a View and fetching it (`mcp_apps.py`), a rendered View's plumbing and who may see it (`mcp_apps_views.py`, used by the session, contact and Slack surfaces), and the View sandbox proxy (`views_mcp_apps.py`, `/mcp-apps/`). No models: reads `tokens`' `AppCredential` and `HostGrant`, and calls the site through `host_grants.host_gateway`. |
| `host_grants` | **framework** | canopy reaching a Connected site's MCP as the visitor, and hosting its own: redeeming a host-issued grant (`host_grants.py`; the `HostGrant` rows stay in `tokens`), canopy as the site's OAuth client (`client_identity.py`, `/oauth/client.json`, `/oauth/jwks.json`), the only door out to an operator-typed URL (`outbound.py`), the gateway that calls the site's tools (`host_gateway.py`, `site_tools`/`site_call`) and what it vouches for on each call (`execution.py`, `Canopy-Execution`), canopy as a host of its own MCP (`self_host.py`), the live probe of a site's grant chain (`live_probe.py`), and the issuer's shared front door (`views_oauth.py`: `/oauth/token` for both its jwt-bearer grant and `oauth_server`'s grants, the RFC 8414/9728 documents, `/oauth/probe`), and a person seeing and stopping their own act-as-you permissions (`grants_api.py`: `/api/me/host-grants/`, `/api/embed/host-grant`, `/api/contact/host-grant`). No models. |
| `embedding` | **framework** | Connected sites and the embedded widget: registering a site and what it may do (`embed_apps.py`, `/api/workspaces/<ws>/connected-apps`, operationIds keep their `tokens_connected_apps_` label), the widget's routes (`embed_api.py`, `/api/embed/…`) and shell (`views_embed.py`, `/embed/…`), signed visitor assertions and the site's keys (`assertions.py`, `jwks.py`), CORS for a site's origin (`cors.py`), "Test connection" (`connection_test.py`), `site ∩ user` for a delegated token (`delegation.py`), and the embed audit log and mint rate limit. Its models (`AppCredential`, `DelegatedToken`, `EmbedAuditLog`) stay in `tokens`. |
| `github_connect` | **framework** | A person's GitHub connection: `GitHubConnection` (the grant canopy holds, table `tokens_githubconnection` kept from when it lived in `tokens`), the GitHub App (`github_app.py`: user tokens, installations), the browser connect flow (`/auth/github/…`) and its JSON routes, still under `/api/tokens/github…`. Read by `agents` to create an agent's repo and sync skill history. |
| `connections` | **framework** | Server-held OAuth connections to third-party providers (Salesforce, Google): `ConnectionApp` (a workspace's OAuth client for a provider, set up in Settings) and `Connection` (through one app; owned by exactly one person or one agent; tokens Fernet-encrypted, refreshed only by canopy-web under a row lock) and `ConnectionGrant` (an agent's connection used by another agent). A provider is one module in `providers/` (`base.Provider` is the OAuth 2.0 code flow; `salesforce.py`, `google.py` the examples); no deploy settings, nothing else names one. Sits above `agents`/`harness`, which never import it. |
| `session_sharing` | **framework** | Shared Claude transcript storage + the public `/w/:workspace/share/:token` viewer (not `sessions`: that name is the live-session harness's). |
| `issues` | **framework** | GitHub issue provenance / evidence capture. |
| `mcp` | **framework** | MCP server infra + audit + rate-limit; every REST route is generated as a tool (`api_tools.py`), so no product tool module lives here. |
| `system` | **framework** | The `/system` capability catalog (read live from the canopy plugin) + the anonymous aggregate counts for the public site (`/api/system/public-stats`). |
| `push` | **framework** | Web Push subscription registry (VAPID keypair + `PushSubscription` rows). Agent-agnostic — any agent's board could trigger a send; observes `agents` (never the reverse), same direction `harness` takes. |
| `realtime` | **framework** | WebSocket transport (Django Channels + Redis): live-tails the `harness` `TurnEvent` ledger (`turn.{id}`), pushes `/supervisor` runner-status + waiting-count deltas, carries the runner control channel (`ws/runner/{id}/`), and hosts cross-app viewer presence (`ws/presence/` — one socket per tab, Redis roster with 60s field TTL in `presence_store.py`, `PresencePreference` opt-out re-checked on every enter; badge UI shared via `canopy-ui/presence`). Fan-out mirrors `push` (signal/`post_save` → `on_commit` → `group_send`); observes `harness`/`push`/`agents`, never the reverse. See `docs/superpowers/specs/2026-07-16-realtime-chat-cloud-runner-program-design.md`. |
| `canopy_sessions` | **framework** | Live, multiplayer chat sessions — the interactive front-door to a durable `harness` Turn. A `Session` "send" enqueues a session-target Turn; the assistant stream lands in the `TurnEvent` ledger and is projected into `Message` rows. It also holds co-edited `Draft` + `SessionParticipant` + cache-backed presence and a per-session `SessionConsumer` (`ws/canopy-sessions/{id}/`) over the `realtime` transport. Agent-agnostic (opaque `metadata` for product linkage). Named `canopy_sessions` — plain `sessions` collides with the `django.contrib.sessions` label (and `session_sharing` already owns the shared-transcript name). The route is mounted at `/api/canopy-sessions`, matching the app label; the WS protocol strings (`chat.<verb>`) and the Channels group name (`chat.{id}`) keep the `chat` prefix. |
| `feedback` | **framework** | Feedback on a thing, from a person, via a channel (`web`/`email`/`gdoc`/`manual`/`api`). Generic over its target (`target_kind` + `target_ref` strings, never an FK) so it imports no product app. Idempotent per `(channel, source_ref)` so re-reading a mailbox is safe. Deliberately emits NO signal: feedback is input to a judgement, not work — a turn reads the pool when the owner is ready. canopy-web runs no pollers and holds no third-party credentials; agents ingest. |
| `events` | **framework** | A durable, fleet-wide log of actions and errors — the place an operational fault lives when it belongs to no `Turn`. Generic over its producer (`source` + `kind` strings, never FKs), so it imports nothing and any app may write to it. Repeats of `(workspace, source, key)` coalesce onto one row with a `count` — the runner's in-memory `failure_log` streak idea made durable and server-visible. `workspace` is NOT NULL (the nullable-tenant-FK fail-open class). Deliberately emits NO signal, like `feedback`: an event is a record, not work — promoting faults into task asks would both misuse approve/decline and turn a flapping runner into a badge storm. Read-only after write; there is no mutation route, because a log you can edit is not a record. |
| `slack` | **framework** | The Slack front door: a Slack thread becomes a chat session with an agent whose owner turned Slack on, sent as `origin="slack"` by the linked canopy user. Holds the bot token (encrypted); agents never do. A member acts as their own account; anyone else (a Slack guest, a non-member) is recorded as a `Contact` and answered as one, like an email sender — granted nothing. See `docs/superpowers/specs/2026-09-18-slack-front-door-design.md`. |
| `retention` | **framework** | How long the CONTENT of AI conversations and turns is kept. `RetentionRule`s (workspace / kind / source / principal / agent → `keep_days`; nearest workspace, then most specific, then shortest wins) drive a sweep that scrubs a turn's prompt, ledger and raw transcript while keeping its row, drops a chat's expired message prefix behind a `retention_floor_index` that stops runner re-ships writing it back, and deletes shared transcripts. Off until `CANOPY_RETENTION_ENFORCE`; `manage.py retention_sweep` dry-runs. See `docs/superpowers/specs/2026-10-05-content-retention-design.md`. |
| `admission` | **framework** | Which turns a workspace refuses (or lets run `auto`) before they run: admin-edited `AdmissionRule` rows, evaluated at enqueue and at claim; harness calls it at those two seams. See `docs/architecture/access.md` → "Admission rules". |
| `inbound` | **framework** | The push doorbell: receives a workspace's Gmail Pub/Sub notification (verified against that workspace's own `InboundPushConfig`) and rings the runners that hold the mailbox. **Never reads mail** — only the runner holds mailbox credentials. Routes and verification: `docs/architecture/api-surface.md` § Inbound push; provisioning: `docs/runbooks/gmail-push-provisioning.md`. |
| `plugins` | **framework** | A person's agents across their workspaces served as one Claude Code plugin catalog, `canopy-agents` (`/plugins/marketplace.json`; each entry a `github` source on the agent's repo, so canopy holds no plugin bytes). Agent-agnostic: reads `agents` + `workspaces`, owns no models. See `docs/architecture/plugin-marketplace.md`. |
| `capabilities` | **framework** | Cloud capabilities: machines an agent leases for as long as it needs them. The agent DECLARES each one in its repo (`capability.yaml`, published by `canopy-runtime publish-capabilities`) and owns its stack, image and CLI; a workspace admin GRANTS it (`CapabilityGrant`); canopy-web brokers `CapabilityLease`s over the AWS permissions the agent's stack grants it — one fresh EC2 instance per lease, exec over SSM, artifacts in S3, released explicitly, when its turn ends, when idle, or by the off-request sweep (a job on `apps/common/ticks.py`, registered by `apps/capabilities/scheduler.py`). Reads `agents`, `harness`, `events`, `workspaces`. See `docs/architecture/capabilities.md`. |
| `agent_tasks` | **framework** | Calling an agent INLINE: a caller hands an agent a task, its input and the JSON Schema of the answer, and gets a schema-validated result back (`AgentTask`, `/api/agent-tasks/`). Each task queues ordinary `harness` turns tagged `origin_ref.agent_task`; it is done only when the agent submits a valid result, never because a turn ended — a turn's end (`turn_events_appended`) nudges it or fails it, and a task past its deadline fails (a job on `apps/common/ticks.py`). Tenancy derives from its agent. Core, not a feature: the caller envelope (`harness.caller_context`) names the task a turn holds. Reads `agents`, `harness`, `workspaces`. See `docs/architecture/agent-tasks.md`. |
| `approvals` | **framework** | Approval requests: an explicit, typed ask for one person's yes (`ApprovalRequest`, a closed registry of kinds). The server renders what approving will do from the typed params; approving runs the kind's hard-coded handler as the approver (no model); delivered as a Slack DM with Approve/Decline buttons, a Web Push, or an email, and routed like an alert (`alerts_approval`). See `docs/architecture/approvals.md`. |
| `attention` | **framework** | What is waiting on a person, as ONE list (`/api/me/attention/`, a user-level view): each owning app supplies its own items (`<app>/attention.py`, by its own rule — the session feed's `feed.status`, the board's waiting-on-you filter, `members.manage` for access requests) through a string registry like `timeline`'s, so a product app can add a source without being imported. Stores only `Hidden` — a person taking something off their own list without deciding or closing it, until something new happens on it. Reads `agents`, `canopy_sessions`, `workspaces`. |
| `passkeys` | **framework** | A person's WebAuthn passkeys (`Passkey`, several per user) and the five-minute **step-up** a passkey assertion opens on a browser session. Never a way to sign in — the second factor before an action that hands over or changes a secret. Other apps call `apps.passkeys.stepup.require_recent_step_up(request)`, which raises a 403 `{"error": "step_up_required"}` the SPA answers with Touch ID and a retry. Also holds a person's zero-knowledge vault key (`UserVaultKeyset`, `/api/vault/keyset`). Reads `api`, `common`, `agents`. |
| `contacts` | **framework** | People canopy knows about who are not members: a `Contact` (an email or Slack sender, recorded and granted nothing), the people brain (`Person`, `PersonFact`, who may read it — `/people/me`), the Human Context Protocol server and its client registry (`HcpClient`), and the contact surface's routes (`/api/contact/…`, `contact_api.py`, a contact's own HCP in `contact_hcp_api.py`; operationIds keep their `tokens_contact_` label). Reads `agents`, `harness`, `canopy_sessions`, `tokens`, `workspaces`. |
| `projects` | **product** | **A migrations-only stub.** No models, routes or code: it stays installed only so the migration chain replays (shareouts/0001 and workspaces/0008 depend on its nodes) and `projects/0013` drops its tables. Agent projects/tasks are `agents.AgentProject`/`AgentTask`; repo-turn GitHub identity is `agents.RepoIdentity`. |
| `walkthroughs` | **product** | DDD walkthrough artifacts (HTML/video demos). |
| `reviews` | **product** | DDD narrative review surface. |
| `shareouts` | **product** | Team shareout briefings. |
| `dynamic_views` | **product** | Dynamic views: AI-generated screens (JSX as data) owned by a workspace or one of its projects, versioned, and drawn in a sandboxed opaque-origin frame (`/dynamic-views/frame/`) that reads canopy only through a catalog of existing REST reads, as the viewer. Reads `agents`, `workspaces`, `embedding`, `mcp_apps_host`. See `docs/architecture/dynamic-views.md`. |
| `workspace_apps` | **product** | Workspace apps: self-contained applications a workspace installs, switches on and off, makes public, or writes itself — DATA, never server code. A manifest declares roles (each a workspace capability, via `permissions.can`), typed record collections (with optional public projections), the declared actions that are their only writes, and dynamic-view pages drawn in the same sandbox. The bundled Hiring app (an applicant tracker) is the first. Reads `workspaces`, `dynamic_views`, `embedding`. See `docs/architecture/workspace-apps.md`. |
| `runs` | **product** | DDD run aggregation/versioning. |
| `beta_requests` | **product** | The public site's request-access form (`POST /api/beta-requests`, anonymous; access is closed to Dimagi and its partners). Stores the request and emails the person who grants access; grants nothing and creates no user. Product because the public site is canopy's own, not substrate an agent host would reuse. |
| `storyboards` | **product** | An ordered arc of DDD narratives — acts over entries — shareable as ONE capability-bearing link (`read`/`comment`/`suggest`). Curates narratives, so product. An entry FOLLOWS its narrative's current release rather than freezing a run id, which is safe only because every `feedback.Feedback` row records the version it was left against. Anonymous read mirrors `runs`' release endpoint: `auth=None`, self-enforcing, 404 (never 403) on a wrong token so existence never leaks. |
| `mail` | **product** | A workspace's own email domain run through Amazon SES: `MailDomain` (DKIM-verified, globally unique), `MailRoute`s (agent mailbox / forward to a member / drop), threads, messages (deduped per mailbox), forward log and SES events. Removable: nothing in a framework app imports it. Off until `MAIL_ENABLED`. See `docs/superpowers/specs/2026-10-12-workspace-email-domains-design.md`. |

## Accepted seams (carve-outs)

The boundary holds everywhere except these documented, intentional places:

1. **`apps/api` — the composition root.** One NinjaAPI imports every router; a
   framework needs exactly one such wiring seam. Exempt by design.
   Apps import `apps/api`'s kit modules (`auth`, `errors`, `pagination`) in the
   other direction; those import no app.
2. **`apps/timeline/sources.py` — a string registry of product event-sources.**
   Timeline resolves each source by dotted path via `import_module` precisely to
   AVOID a hard framework→product import; a missing product app degrades
   gracefully. The string indirection is the seam. Allowlisted in the content
   guard (below), not the import guard — it holds no product import.

## Enforcement

`tests/test_architecture_boundary.py` (pure stdlib `ast`, runs in the normal
`uv run pytest` CI job) enforces the arrow two ways, so the boundary can't rot:

- **Imports** — parses every framework app and fails if any non-test file
  *imports* a product app, except the carve-outs above.
- **Content** — fails if a framework file merely *names* a product module in a
  string literal (a lazy `import_module`, a registry path, product logic parked in
  a framework file). This is the gate that catches what the import-check is blind
  to — e.g. the DDD run-id grammar belongs in `apps/runs`, not a framework app. Seam #2 is the one allowlisted exception.

It also fails if a **new** app isn't classified into a tier here, and if a
carve-out names a file that no longer exists (a dead allowlist entry is a hole
waiting for a new file of that name).

Adding a new app? Put it in `FRAMEWORK` or `PRODUCT` in both this doc and that
test. Need framework code to touch product? Don't — move the code to a product
app, or (rarely) add a justified carve-out here and in the test.

## Layers inside the framework

The tiers say framework never imports product. Inside the framework the apps are
layered too, and an import may only point down (or sideways, within a layer):

| Layer | Apps | What belongs here |
|---|---|---|
| L0 kit | `common`, `events`, `workspaces`, `api` (the kit: `auth`, `errors`, `pagination`, `renderers`, `route_gates`, `tenancy`) | Infrastructure every app may use. Knows no agent, turn or session. |
| L1 core | `agents`, `harness`, `canopy_sessions`, `tokens`, `contacts`, `admission`, `retention`, `realtime`, `push`, `passkeys`, `embedding`, `mcp_apps_host`, `attention`, `agent_tasks` | The agent/turn/session substrate and who may reach it. |
| L2 features | `agent_runs`, `capabilities`, `connections`, `feedback`, `github_connect`, `host_grants`, `huddles`, `inbound`, `issues`, `mcp`, `oauth_server`, `plugins`, `session_sharing`, `slack`, `system`, `timeline`, `threads`, `approvals` | Framework surfaces built on the core. Nothing below names them. |
| L3 product | the product tier above | canopy's own features. |
| L4 root | `apps/api/api.py` | The composition root, wired in by `config/urls.py`; an app importing it is an upward import like any other. |

`tests/test_layered_app_imports.py` enforces it (pure `ast`, test files and
migrations excluded). An import inside a function counts the same as one at the
top of the module: a lazy import hides a cycle from Python, not the dependency.

The upward imports that exist today are frozen in its `FROZEN` table as
`(src, dst): count`, one count per import statement. It is a **ratchet**:

- **A new upward edge, or a higher count, fails.** Remove the import — move the
  code down a layer, pass a callback, emit a signal, or register by string (as
  `timeline` and `attention` do). If the map itself is wrong, move the app between
  layers in the test and in this table, in a reviewed PR.
- **A lower count, or an edge gone, also fails** — with the line to change or
  delete, so the table shrinks as imports are fixed and never hides slack.
- **After a rebase**, other PRs may have moved imports; regenerate the table with
  `uv run python -m tests.test_layered_app_imports --write` and check the diff raises
  nothing your PR did not mean to.

A new app gets a layer in both places; the test fails until it has one.
