# TODOS

## Canopy-Web Platform

### Coaching facts on a dashboard

**What:** a coaching panel that reads `coaching.note_sent`, `coaching.reply` and `coaching.finding_routed`.

**Why not yet:** the facts are recorded (`docs/architecture/analytics.md`), but nothing reads them yet: `analytics.build` counts other kinds. A panel has to decide who may see a per-person reply, since the facts carry `person_id`. They also outlive the coach's `forget`, which the consent text says.

**Effort:** S.

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

**What:** Items deferred from the V1 walkthrough sharing slice:
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

**What:** Items deferred from the `apps/harness` / `runner/canopy_runner` Phase 0 slice:
- Per-runner PAT binding + workspace FK scoping on the harness `Runner`/`Turn` models — any authenticated PAT can currently act as any runner.
- A board-command → automatic `Turn` enqueue hook — Phase 0's only trigger is a manual `POST /api/harness/turns/`.

**Effort:** S-M each.
**Priority:** P2
**Depends on:** Phase 0 (laptop/emdash runner) in real use beyond a single agent/laptop.

### Agents on Codex — remaining plans and deferred notes

**What:** canopy-web can route a turn to Codex (the executor axis is in place), but no runner runs one yet. Left:
- **Plan 2, cloud runner Codex executor.** Starts with the live probe (a ChatGPT-signed-in `codex` on a box). Also covers Codex logins on `RunnerLogin`, an "add OpenAI key" UI, and runner copy that still says "Claude is not signed in".
- **Plan 3, canopy plugin:** guards and caller context read Codex hook payloads; the factory's `skills/turn` and persona fallback (`AGENTS.md`).
- **Plan 4, laptop headless runner on Codex.**
- **An agent's model in emdash sessions.** emdash starts Claude Code itself, so a session it opens ignores `Agent.cli_models`; every other path honours it.
- **Executors receive `invocation`.** The claim carries a turn's parsed plugin command; the Codex executor renders it as a skill invocation (Claude Code types `prompt` as is).

**Priority:** P2
**Depends on:** a Codex sign-in on a test box for Plan 2's probe

### Claude Code: use the CLI's first-party mechanisms

**What:** Use what the `claude` CLI offers first-party instead of canopy's own parsing. Open items, in suggested order:
- Replace the paid "Reply with OK" credential probe with zero-cost signals (the `rate_limit_event` on every real turn keeps a credential's state current).
- Background tasks in one-shot turns: drop the deny-hook.
- Use `claude agents --json` to list live sessions and their state.
- Load each agent's plugin from the turn's own checkout (`--plugin-dir`) instead of the auto-updated cache, and on cloud boxes put guards and denies in `managed-settings.json` (decide the plugin-locking split together: a managed `enabledPlugins` lock blocks a `--plugin-dir` copy of the same name).
- Use Claude Code's OS sandbox for confined turns that grant Bash; ask a human without screen-scraping or keystrokes.
- Spikes: one Agent SDK executor instead of `claude -p` parsing plus an ACP adapter; on the laptop, `claude --bg` + `claude attach` versus driving emdash over CDP.
- Still open from structured-error reading: route away from a credential near its cap using `rate_limit_event` utilization, delete the text matchers' duplicate copies in `packages/canopy_transcript`, and check whether the laptop's transcript records carry `error`.

**Priority:** P2
**Depends on:** None
