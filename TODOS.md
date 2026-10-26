# TODOS

## Canopy-Web Platform

### Proactive Conversation Detection

**What:** Monitor Slack channels and open claw sessions for conversations that contain reusable approaches. Surface as suggestions in the web UI.

**Why:** Transforms the product from "tool you visit" to "system that works for you." Solves the behavior change problem — non-technical users won't paste conversations, but they'll click a suggestion.

**Context:** Deferred from CEO review Expansion 1. Requires Slack API OAuth, background analysis job, notification system. This is the V2 flywheel that creates network effects. Should ship after V1 workspace is validated with real users (Neal test passes). False positive filtering is critical — annoying suggestions erode trust faster than missing suggestions.

**Effort:** M (human) → S with CC
**Priority:** P1
**Depends on:** V1 workspace validated with real users

### Prompt Injection Hardening

**What:** Add input sanitization and prompt separation for source content before LLM calls.

**Why:** Source content (Slack threads, transcripts) goes directly into LLM prompts. Currently trusted (internal tool), but must be hardened before any external use.

**Context:** Identified in CEO review Section 3 (Security). V1 accepts risk (single-tenant, trusted users). For V2 or external use: separate user-provided content from system instructions using Claude's message role separation, validate/sanitize inputs, add content length limits.

**Effort:** S (human) → S with CC
**Priority:** P2
**Depends on:** None — can do anytime before external exposure

### Slack API OAuth Integration

**What:** Auto-fetch Slack thread content by URL instead of requiring manual paste.

**Why:** Reduces friction for the most common source type. Currently V1 requires copy-pasting thread text.

**Context:** Deferred from V1 scope. Requires Slack workspace OAuth flow, thread content fetching, handling private channels/permissions. The existing Slack agents can already call the API — this is about the web UI being able to fetch threads directly.

**Effort:** S-M (human) → S with CC
**Priority:** P2
**Depends on:** V1 workspace validated

### Google Docs API Integration

**What:** Auto-fetch Google Doc content by URL instead of requiring manual paste.

**Why:** Google Docs are a common source type (e.g., Eva's CRISPR analysis document).

**Context:** Deferred from V1 scope. Requires Google OAuth flow or service account, Docs API content fetching, handling permissions.

**Effort:** S (human) → S with CC
**Priority:** P3
**Depends on:** V1 workspace validated

### Multi-Tenant Auth

**What:** Add user authentication and multi-tenant support to the platform.

**Why:** Required if the tool is shared beyond the immediate team or if multiple teams use it independently.

**Context:** V1 is single-tenant, no auth (internal tool). When adoption grows, need: user accounts, team/org scoping, skill visibility controls (private/team/org), audit logging of who created/modified skills.

**Effort:** M (human) → S with CC
**Priority:** P3
**Depends on:** V1 adoption growing beyond immediate team

### Cowork Runtime Adapter

**What:** Build a runtime adapter for the cowork environment.

**Why:** Extends skill portability to the cowork platform.

**Context:** Deferred pending cowork API spec/documentation. The skill schema already has a placeholder for cowork (type: task_sequence). Implementation depends on understanding cowork's execution model.

**Effort:** S (human) → S with CC
**Priority:** P3
**Depends on:** Cowork API documentation available

### Walkthrough sharing — deferred (V2)

**What:** Items deferred from the V1 walkthrough sharing slice (spec `docs/superpowers/specs/2026-05-26-walkthrough-sharing-design.md`):
- View analytics (who viewed, when, where from)
- Multi-link / per-audience tokens (promote `share_token` to its own table)
- Comments / reactions on walkthroughs
- Embed support (oEmbed-style)
- Video poster frames, chapter markers, thumbnails
- Signed Drive URLs for video (approach B in the spec) — only if Cloud Run egress becomes a measurable cost
- Auto-upload mode from `/canopy:walkthrough`
- Browser drag-drop upload UI at `/walkthroughs` (currently CLI-only)
- Multi-tenant scoping of walkthrough list (today: any dimagi user sees all walkthroughs)

**Effort:** Each item is S-M independently.
**Priority:** P2 (analytics, video signed URLs) / P3 (rest)
**Depends on:** V1 backend in production, real usage feedback

### Agent execution control plane — deferred from Phase 0

**What:** Items deferred from the `apps/harness` / `runner/canopy_runner` Phase 0 slice (spec `docs/superpowers/specs/2026-07-05-agent-execution-control-plane-design.md`):
- Per-runner PAT binding + workspace FK scoping on the harness `Runner`/`Turn` models (spec §5.1/§9) — any authenticated PAT can currently act as any runner.
- A board-command → automatic `Turn` enqueue hook — Phase 0's only trigger is a manual `POST /api/harness/turns/`.

**Effort:** S-M each.
**Priority:** P2
**Depends on:** Phase 0 (laptop/emdash runner) in real use beyond a single agent/laptop.

### Agents on Codex — remaining plans and deferred notes

**What:** canopy-web can route a turn to Codex (spec `docs/superpowers/specs/2026-10-26-multi-cli-agents-design.md`, Plan 1 shipped), but no runner runs one yet. Left:
- **Plan 2, cloud runner Codex executor.** Starts with the live probe (a ChatGPT-signed-in `codex` on a box). Also covers Codex logins on `RunnerLogin`, an "add OpenAI key" UI, and runner copy that still says "Claude is not signed in".
- **Plan 3, canopy plugin:** guards and caller context read Codex hook payloads; the factory's `skills/turn` and persona fallback (`AGENTS.md`).
- **Plan 4, laptop headless runner on Codex.**
- **Deferred minors from the Plan 1 review** (none affect a box that reports only Claude Code):
  1. The signed-out reason uses `allowed()[0]`, so it can name the wrong CLI (Codex signed in but not confining, Claude confining but signed out).
  2. "Sign X in" is the wrong advice when X is signed in but cannot confine, or for a sensitive conversation on a Codex-only agent.
  3. The sensitive-data check still runs inside the `ignore_requirements` counterfactual, so that case is never diagnosed as a requirements problem.
  4. A heartbeat whose `clis` names only unknown CLIs stores `{}` and the box takes no turns (fails closed; surprising for a newer runner on an older server).
  5. The unclaimable report runs an `ignore_cli` coverage pass even when every agent in scope is Claude-only.
  6. The frontend (`lib/clis.ts`, `RunnerOverview.tsx`) hardcodes the profile/envelope versions and the signed-out note; the server should compute them on `RunnerOut`.
  7. Local turns record `cli=""`; they are always Claude Code.

**Priority:** P2
**Depends on:** a Codex sign-in on a test box for Plan 2's probe

### Claude Code: use the CLI's first-party mechanisms

**What:** The audit in `docs/superpowers/specs/2026-10-26-claude-code-audit.md`. First: the cloud runner classifies failures by message text, so an invalid or revoked setup-token's 401 ("OAuth access token is invalid") neither fails over to another login nor raises an alert; classify on the structured error and rate-limit fields instead. Then stop `claude -p` with SIGINT, replace the paid "Reply with OK" probe, and the rest in the audit's suggested order.

**Priority:** P1 for the 401 fix (a live failover bug), P2 for the rest
**Depends on:** None

## Completed

### MCP Layer — shipped (PR #71)
Exposed canopy-web as a FastMCP 3.x Streamable-HTTP server at `/api/mcp/` (`apps/mcp/`), authenticated per-user via Personal Access Tokens, with audit logging and write rate-limiting. Tools reuse the REST service layer. Tools today: `list_insights`, `clear_insights`. See `docs/architecture/mcp-surface.md`.
