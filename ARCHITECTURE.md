# canopy-web architecture — the framework/product boundary

> Wave 0 of the framework harvest (`docs/superpowers/specs/2026-06-24-canopy-framework-harvest-design.md`).
> This doc makes the one boundary the whole framework thesis depends on **legible**;
> `tests/test_architecture_boundary.py` makes it **enforced**.

## The one invariant

> **The dependency arrow is one-way: FRAMEWORK code never imports PRODUCT code.
> PRODUCT freely imports FRAMEWORK.**

- **FRAMEWORK** = the generic, *agent-agnostic* substrate any agent could reuse:
  identity, board, command queue, transcripts, auth, timeline, MCP server, API
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
| `agents` | **framework** | Agent workspace: board, command-drain queue, work-products, tasks, the *inbox* (open Items). Agent-agnostic. |
| `agent_runs` | **framework** | Unified agent run-lifecycle: run → step → artifact → verdict/QA → decision → gate → fork, as a storage-agnostic read model behind a `RunStore` Protocol (DB adapter persists rows; Drive adapter reads ACE's YAML). FK's `agents.Agent`; imports no product app. |
| `harness` | **framework** | Agent-execution harness: runner registry, turn lifecycle + lease/claim, turn-event ledger (/api/harness). |
| `workspaces` | **framework** | Multi-tenancy: `Workspace` + members (owner/editor/viewer) + email invites (ported from ace-web, domain-agnostic — no Drive coupling). The tenant that owns agents + runs. Distinct from the retired co-authoring app that used to be `apps/workspace` (singular). |
| `api` | **framework** (composition root) | The single NinjaAPI that wires every app's router. The one seam allowed to import all apps. |
| `common` | **framework** | Shared infra: anthropic client, auth flow, middleware, auth-domains. |
| `timeline` | **framework** | Generic activity-log aggregation; reads other apps' events via a string registry (no hard product imports). |
| `tokens` | **framework** | Personal Access Token management + bearer auth. |
| `session_sharing` | **framework** | Shared Claude transcript storage + the public `/share/:token` viewer (renamed from `sessions` to free that name for the live-session harness). |
| `issues` | **framework** | GitHub issue provenance / evidence capture. |
| `mcp` | **framework** | MCP server infra + audit + rate-limit. (Individual *tools* may be product — see carve-outs.) |
| `system` | **framework** | System metadata / AI-backend status. |
| `push` | **framework** | Web Push subscription registry (VAPID keypair + `PushSubscription` rows). Agent-agnostic — any agent's board could trigger a send; observes `agents` (never the reverse), same direction `harness` takes. |
| `realtime` | **framework** | WebSocket transport (Django Channels + Redis): live-tails the `harness` `TurnEvent` ledger (`turn.{id}`) and pushes `/supervisor` runner-status + waiting-count deltas. Fan-out mirrors `push` (signal/`post_save` → `on_commit` → `group_send`); observes `harness`/`push`/`agents`, never the reverse. Wave 4 (`docs/superpowers/specs/2026-07-16-realtime-chat-cloud-runner-program-design.md`). |
| `canopy_sessions` | **framework** | Live, multiplayer chat sessions — the interactive front-door to a durable `harness` Turn. A `Session` "send" enqueues a session-target Turn; the assistant stream lands in the `TurnEvent` ledger and is projected into `Message` rows. SP3 adds co-edited `Draft` + `SessionParticipant` + cache-backed presence and a per-session `SessionConsumer` (`ws/canopy-sessions/{id}/`) over the `realtime` transport. Agent-agnostic (opaque `metadata` for product linkage). Named `canopy_sessions` — plain `sessions` collides with the `django.contrib.sessions` label (and `session_sharing` already owns the shared-transcript name). The route is mounted at `/api/canopy-sessions` (renamed from the historical `/api/chat`, to match the app label); the WS protocol strings (`chat.<verb>`) and the Channels group name (`chat.{id}`) are unchanged. Wave 4 SP2–SP3. |
| `feedback` | **framework** | Feedback on a thing, from a person, via a channel (`web`/`email`/`gdoc`/`manual`/`api`). Generic over its target (`target_kind` + `target_ref` strings, never an FK) so it imports no product app — the same discipline `Item` follows. Idempotent per `(channel, source_ref)` so re-reading a mailbox is safe. Deliberately emits NO signal: feedback is input to a decision, not work — a turn reads the pool when the owner is ready. canopy-web runs no pollers and holds no third-party credentials; agents ingest. |
| `projects` | **product** | Canopy's portfolio/insights feature: repos + which canopy skills ran (`skills[]`, `skill_name`, hygiene-skill frontend). Not a generic registry today — promote to framework only when a real second consumer needs one. |
| `walkthroughs` | **product** | DDD walkthrough artifacts (HTML/video demos). |
| `reviews` | **product** | DDD narrative review surface. |
| `shareouts` | **product** | Team shareout briefings. |
| `runs` | **product** | DDD run aggregation/versioning. |
| `storyboards` | **product** | An ordered arc of DDD narratives — acts over entries — shareable as ONE capability-bearing link (`read`/`comment`/`suggest`). Curates narratives, so product. An entry FOLLOWS its narrative's current release rather than freezing a run id, which is safe only because every `feedback.Feedback` row records the version it was left against. Anonymous read mirrors `runs`' release endpoint: `auth=None`, self-enforcing, 404 (never 403) on a wrong token so existence never leaks. |

## Accepted seams (carve-outs)

The boundary holds everywhere except these documented, intentional places:

1. **`apps/api` — the composition root.** One NinjaAPI imports every router; a
   framework needs exactly one such wiring seam. Exempt by design.
2. **`apps/mcp/tools/insights.py` — a product MCP tool on the framework server.**
   `apps/mcp/server.py` registers tools by importing `apps.mcp.tools` as a side
   effect; the `insights` tool exposes the `projects` (portfolio) product. Same
   composition-root shape as the api hub. **Candidate for inversion** later
   (let `projects` register its own tool via `AppConfig.ready`), which would
   remove this carve-out.
3. **`apps/timeline/sources.py` — a string registry of product event-sources.**
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
  to — e.g. the DDD run-id grammar that had been living in `apps/common/ddd.py`
  and now sits in `apps/runs`. Seam #3 is the one allowlisted exception.

It also fails if a **new** app isn't classified into a tier here.

Adding a new app? Put it in `FRAMEWORK` or `PRODUCT` in both this doc and that
test. Need framework code to touch product? Don't — move the code to a product
app, or (rarely) add a justified carve-out here and in the test.
