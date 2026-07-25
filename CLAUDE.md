# Canopy Web

Collaborative web workspace for the canopy agent ecosystem — portfolio insights,
first-class AI agents, demo-driven development (DDD), walkthroughs, and shareouts.

## Architecture

- **Backend:** Django 5 ASGI + uvicorn, Django Ninja 1.x + Pydantic v2, PostgreSQL.
  OpenAPI 3.1 schema auto-generated at `/api/openapi.json`; Scalar UI at
  `/api/docs/`; Redoc at `/api/redoc/`. All errors return RFC 7807
  `application/problem+json`. Frontend TypeScript types are generated from the
  schema (`frontend/src/api/generated.ts`) and consumed via `openapi-fetch`.
- **Frontend:** React 19 + Vite + Tailwind CSS 4 + shadcn/ui
- **AI:** Anthropic Claude API via SSE streaming. Dual backend — direct API key (`AI_BACKEND=api`) or Claude Code CLI subscription (`AI_BACKEND=cli`), switchable at runtime via `/api/ai/switch/`.
- **MCP server:** `apps/mcp/` is a FastMCP 3.x Streamable-HTTP server mounted into the ASGI app at `/api/mcp/` (wired in `config/asgi.py`). Tools run **as the authenticated user** via per-user PAT (`CanopyPATVerifier`) and reuse the same service functions as the REST views, so the two surfaces can't drift. See `docs/architecture/mcp-surface.md`.
- **Deployment:** AWS ECS Fargate on the shared labs platform (account `858923557655`, `us-east-1`), served at `https://labs.connect.dimagi.com/canopy/` behind the shared ALB. One container (Django serves the built SPA + API + MCP). Deploys run from GitHub via the **Deploy to Labs (AWS)** workflow (`.github/workflows/deploy-labs.yml`): build → push to ECR (`labs-jj-canopy-web`) → register task-def revision → auto-migrate (idempotent; skippable only via `skip_migrations`) → roll the ECS service. Infra is provisioned by `deploy/aws/canopy-web.cfn.yaml` (CloudFormation). Runtime settings in `config/settings/connectlabs.py` (extends `production.py`; `FORCE_SCRIPT_NAME=/canopy`, shared RDS `canopy_web` DB).
- **Framework/product boundary (the one invariant):** apps split into **framework** (generic, agent-agnostic substrate — `agents`, `agent_runs`, `workspaces`, `api`, `common`, `timeline`, `tokens`, `session_sharing`, `issues`, `mcp`, `system`, `realtime` (Channels WS transport), `canopy_sessions` (multiplayer chat sessions)) and **product** (canopy's own features — `projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`). **Framework code must never import product code; product freely imports framework.** This keeps the blend cuttable (the framework apps could lift onto a standalone host without dragging canopy's product). It's a *direction, not a wall* — we don't move apps into `framework/`/`product/` folders. Enforced by `tests/test_architecture_boundary.py` (fails CI on a framework→product import, or on a new app left untiered). Full rationale, the per-app tier table, and the accepted carve-outs (the `api` composition root, the `mcp` insights tool): **`ARCHITECTURE.md`**. The framework apps are being harvested as the generic layer out of ACE — see `docs/superpowers/specs/2026-06-24-canopy-framework-harvest-design.md`.

## Development

Backend uses [`uv`](https://docs.astral.sh/uv/) for dependency management (uv.lock is committed). Install uv first if you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```bash
# Backend
cp .env.example .env  # Set AI_BACKEND=api + ANTHROPIC_API_KEY, or AI_BACKEND=cli
uv sync --extra dev
uv run python manage.py migrate
uv run python manage.py seed_projects   # optional: seed the initial 13 portfolio projects
uv run python manage.py runserver

# Frontend
cd frontend && npm install && npm run dev

# Both (via honcho)
uv run honcho start -f Procfile.dev

# Docker (backend + frontend + Postgres)
docker compose up

# Deploy to AWS labs (https://labs.connect.dimagi.com/canopy/). Deploys run from
# GitHub only — trigger the "Deploy to Labs (AWS)" workflow from the Actions tab
# (workflow_dispatch), or:
gh workflow run "Deploy to Labs (AWS)" --ref main                          # migrations run automatically
gh workflow run "Deploy to Labs (AWS)" --ref main -f skip_migrations=true  # EMERGENCY ONLY: skip migrations
# Production ships from `main` ONLY — the workflow hard-fails on any other ref.
# It builds+pushes the image to ECR, registers a task-def revision (image swap
# only), ALWAYS migrates on a one-off Fargate task before cutover (migrate --noinput
# is idempotent — a no-op when nothing is pending, so it can't be forgotten), then
# rolls the ECS service. See .github/workflows/deploy-labs.yml + deploy/aws/canopy-web.cfn.yaml.
```

When `AI_BACKEND=cli`, the `claude` binary must be on PATH and authenticated. In Docker, use the headless auth flow at `/settings` (drives `claude setup-token` via PTY; token persists in `CLAUDE_CODE_OAUTH_TOKEN`).

## Testing

```bash
uv run pytest                                    # All backend tests
uv run pytest tests/test_agents.py -v            # Specific
cd frontend && npm run build                     # Frontend type check + build
cd frontend && npm run gen:api                   # Regenerate TypeScript types from OpenAPI schema
```

CI (`.github/workflows/ci.yml`) runs both on every PR and on push to main. Deploy is a separate manual job in the same workflow — trigger it from the Actions tab via "Run workflow"; the deploy step waits for the test jobs to pass before shipping. Walkthrough QA spec at `docs/walkthroughs/project-workbench.yaml` (run via `/walkthrough project-workbench`).

## Key URLs

The app is **workspace-tenant-scoped** (PR #183). Every surface that owns tenant
data lives under `/w/:workspace/`; personal/global surfaces and public viewers
stay at root. A header workspace switcher appears when you belong to >1 workspace.
Bare `/` redirects to the active workspace's workbench, and the legacy flat paths
(`/timeline`, `/shareouts`, `/walkthroughs`, `/agents/*`, `/ddd/*`) redirect into
the active workspace. `/ddd-plans` and `/reviews` now redirect to `/`.

**Tenant-scoped (under `/w/:workspace/`):**
- `/w/:workspace` — Project workbench. Tile grid dashboard with a "Today's top 3" insight hero, freshness chip, inline insights triage, and self-prioritizing tile order by insight count.
- `/w/:workspace/timeline` — Team activity timeline (cross-app activity feed; link-out only)
- `/w/:workspace/shareouts` (+ `/shareouts/:period`) — Dated, teammate-facing work briefings (what shipped, why, how to leverage) posted by `/canopy:shareout`; `:period` is a copy-linkable permalink to one briefing
- `/w/:workspace/walkthroughs` — Sharable demos uploaded from `/canopy:walkthrough`
- `/w/:workspace/ddd` (+ `/ddd/:narrative`, `/ddd/:narrative/:runId`) — Demo-driven-development (DDD) views: narrative → version → run → package (video + deck + narrative + links)
- `/w/:workspace/agents` — First-class AI agents list (e.g. "Echo")
- `/w/:workspace/members` — Members + invites admin surface (owner-only actions): list members, remove a member, invite by email, list/revoke pending invites. Canopy sends no email — invite by copying the generated `/invite/:token` link and sending it yourself.
- `/w/:workspace/agents/:slug` — Agent workspace: a full-bleed rail + scrolling main built on `canopy-ui`. Sub-routes (rail): **Inbox** (the default landing — the agent's OPEN `Item`s, ranked review→question, decidable in place; legacy `needs-you` path 302s here), Overview, Tasks (the "who has the ball" board), **Items** (the full item ledger incl. decided/dismissed; `?batch=<key>` renders one sitting, e.g. a fleet audit), Turns (packaged units of work + optional transcript), Schedules, Syncs, Work products, Skills
- `/w/:workspace/schedules` — Weekly calendar of that workspace's recurring schedules
- `/w/:workspace/chat` — Session-centric chat home: a findable list of your chat sessions to continue from any device + "New chat with `<agent>`". Reusable `ChatSessionsPanel` (cross-workspace); supervisor's Sessions tab embeds it (with the grouped-by-project `OpenSessions` view). "Chats" nav entry.
- `/w/:workspace/chat/:id` — Live multiplayer chat with an agent, built on the **`canopy-ui/chat`** kit (ported from ace-web; `ChatPanel` + `useSessionSocket` over `ws/canopy-sessions/{id}/`, co-edited draft + presence + streamed reply). A send enqueues a session `Turn`; a session-capable runner drives the agent's emdash session and bridges the reply back live. See `docs/superpowers/specs/2026-07-22-reusable-chat-kit-design.md`.

**Root / personal / global:**
- `/system` — Capability catalog + Workflows view (how canopy's plugin capabilities compose; read live from the canopy plugin)
- `/insights` — Cross-portfolio AI insights feed (user-scoped; deliberately not tenant-scoped)
- `/supervisor` — Cross-fleet "waiting on you" inbox, agent KPI cards, and runner status. Loaded by three consumers (phone PWA, the menubar's WKWebView, desktop browser); deliberately root, not `/w/:workspace/` — the fleet spans workspaces, like `/insights`. Installable as an Android PWA (manifest + service worker) and pushes a notification when any owned agent's `waiting_count` increases — see Push below
- `/schedules` — Personal weekly calendar of every recurring schedule across all workspaces you belong to (client-filterable by workspace/agent); the per-workspace view is `/w/:workspace/schedules`. Same component (`ScheduleCalendar`) mounts both routes and reuses the per-agent rail's `ScheduleEditor` for edits
- `/sessions` — My shared Claude Code sessions (transcripts uploaded via `/canopy:share-session`)
- `/settings` — AI backend status, switch backends, headless Claude CLI auth, theme toggle, and debug-session minting (consolidated under the user menu)
- `/walkthrough/:id` — Single walkthrough viewer (HTML iframe or video player). Reclaimed from `/w/:id` when `/w/` became the tenant prefix; a legacy `/w/<uuid>/content` link 302-redirects here
- `/review/:id` — Editable narrative review surface for DDD (approve / redraft a story before build); public (link-visibility) reviews are readable by anyone with the URL, but submitting a decision requires a Dimagi login
- `/invite/:token` — Accept-invite page (deliberately outside `/w/:workspace/`: an invitee has no membership yet, so there's no tenant to scope into). Pre-auth, previews the invite (masked email, workspace name, role) via `GET /api/workspaces/invites/{token}/preview`; accepting requires a Dimagi (or invite-admitted) login
- `/share/:token` — Public, chrome-less read-only viewer for a shared session (no login; mounted outside the app shell)
- `/api/` — REST API
- `/admin/` — Django admin
- `/health/` — Health check

## API Endpoints

All endpoints are served by Django Ninja (Pydantic v2 typed) under `/api/`. Errors use RFC 7807 `application/problem+json`. The machine-readable schema lives at `/api/openapi.json`; browse at `/api/docs/` (Scalar) or `/api/redoc/`.

**Tenant routing:** the canonical tenant URL is `/api/w/{ws}/…`. `apps/api/tenancy.py::WorkspaceResolveMiddleware` gates membership (non-member → 404), pins `request.workspace_slug`, then strips the prefix and reroutes to the flat mount — so the OpenAPI schema stays single/clean (no double-mount, no colliding operation IDs). The flat `/api/…` routes below remain a **non-breaking compat shim**: `workspace_slug` resolves to the caller's default workspace, keeping the PAT/plugin fleet (e.g. Echo, the canopy plugin) working unchanged. Handlers read `getattr(request, "workspace_slug", None)` — truthy pins the workspace, `None` applies the handler's default.

### Auth + session (root)
- `GET /api/me/` — Current authenticated user
- `GET /api/csrf/` — CSRF token bootstrap
- `GET|POST /auth/cli/authorize/` — gh-style loopback flow: an authenticated browser mints a `PersonalToken` and 302-redirects it to a local CLI callback (validates the callback is a safe loopback target). Pairs with `/canopy:canopy-web-pat-mint`. Bare Django view (`apps/tokens/cli_authorize_views.py`).

### Projects
- `GET /api/projects/` — List projects with latest context
- `POST /api/projects/` — Create project
- `GET /api/projects/slugs/` — Lightweight slug list
- `GET /api/projects/{slug}/` — Project detail with full context
- `PATCH /api/projects/{slug}/` — Update project
- `DELETE /api/projects/{slug}/` — Delete project
- `POST /api/projects/{slug}/context/` — Push context entry
- `GET /api/projects/{slug}/context/` — List context entries
- `GET /api/projects/{slug}/context/latest/` — Latest context per type
- `POST /api/projects/seed/` — Bulk seed projects
- `POST /api/projects/batch-context/` — Create context entries across many projects in one request (body: `{updates: {slug: [...]}}`)
- `POST /api/projects/batch-actions/` — Record actions across many projects in one request (body: `{updates: {slug: [...]}}`)
- `POST /api/projects/{slug}/actions/` — Record a skill action
- `GET /api/projects/{slug}/actions/` — List actions (filter: ?skill=name)
- `GET /api/projects/{slug}/actions/summary/` — Latest action per skill

### Insights
- `GET /api/insights/` — List all insights across projects. Filters: `?category=<slug>` (matches `[<slug>]` content prefix), `?source=<producer>` (filters by writer), `?project=<slug>`. Bearer-readable for machine producers (e.g. `canopy:portfolio-review`) so they can dedupe before re-publishing.
- `DELETE /api/insights/{id}/` — Dismiss an insight (OAuth only — bearer is GET-only here).
- `POST /api/insights/clear/` — Clear insights (regeneration helper).

### Workspaces (`apps/workspaces`) — multi-tenancy
The tenant that owns agents + runs. `Workspace` + members (owner / editor / viewer) + email invites (ported from ace-web, domain-agnostic). Replaced the retired `apps/workspace` (singular) co-authoring session app — that whole SSE skill-authoring engine and its `/api/workspace/*` routes are gone.
- `POST /api/workspaces/` — Create a workspace
- `GET /api/workspaces/` — List my workspaces
- `GET /api/workspaces/{slug}/` — Get a workspace (member-only)
- `GET /api/workspaces/{slug}/members/` — List members (member-only)
- `DELETE /api/workspaces/{slug}/members/{user_id}/` — Remove a member (owner-only)
- `POST /api/workspaces/{slug}/invites/` — Invite by email (owner-only)
- `GET /api/workspaces/{slug}/invites/` — List invites (member-only)
- `POST /api/workspaces/{slug}/invites/{invite_id}/revoke` — Revoke an invite (owner-only)
- `GET /api/workspaces/invites/{token}/preview` — Pre-auth invite preview (`auth=None`; minimal disclosure — a dead token reveals only that it's dead, never which workspace it pointed at). Drives `/invite/:token`
- `POST /api/workspaces/invites/{token}/accept` — Accept an invite

**`dimagi` is the only auto-join workspace; every other workspace is invite-only.** `auto_join_domains` (non-empty only for `dimagi`, seeded from `AUTH_ALLOWED_EMAIL_DOMAIN` by `ensure_default_workspace()`) is server-only — never client-settable (`WorkspaceCreateIn` has no such field; `StrictModel` 422s a request that sends it anyway) — so getting into any other workspace means an owner invites you by email from `/w/:workspace/members` and you accept via the emailed-by-hand `/invite/:token` link (canopy sends no email itself; copy the link and send it yourself, e.g. Slack/email). Audit production for drift with `uv run python manage.py audit_auto_join` (reports every workspace with non-empty `auto_join_domains` — slug, domains, member count); `--fix` clears it on every workspace except `dimagi`. Safe to run repeatedly.

### Issues (`apps/issues`)
A `canopy.origin` record store — GitHub issue provenance / evidence capture (the issues ACE files as it runs).
- `POST /api/issues/` — Upsert an origin record
- `GET /api/issues/` — List origin records (paginated)
- `GET /api/issues/{repo_slug}/{number}/` — Get an origin record
- `DELETE /api/issues/{repo_slug}/{number}/` — Delete an origin record (cleanup)

### AI backend (`apps/common`)
- `GET /api/ai/status/` — Current backend + auth state
- `POST /api/ai/switch/` — Switch between `api` and `cli` at runtime
- `POST /api/ai/auth/start/` — Begin headless Claude CLI login
- `POST /api/ai/auth/complete/` — Submit OAuth code
- `GET /api/ai/auth/poll/` — Poll auth status

### Personal Access Tokens (`apps/tokens`)
- `GET /api/tokens/` — list my tokens (no raw values)
- `POST /api/tokens/` — mint a token (raw returned once)
- `DELETE /api/tokens/{id}/` — revoke a token (owner-only; 404 hides other users' tokens)

Tokens are long-lived bearer credentials per Django user. The raw value is sha256-hashed at creation and never persisted. Pass `Authorization: Bearer <raw>` on any request; `apps.tokens.middleware.BearerTokenAuthMiddleware` resolves it to `request.user`. Replaces the retired `WORKBENCH_WRITE_TOKEN` shared-secret + `/api/auth/e2e-login/` flow.

Bootstrap a token via the management command:

```bash
uv run python manage.py create_token --email ace@dimagi-ai.com --label "canopy plugin" --create-user
```

### Walkthroughs
- `GET /api/walkthroughs/` — List. Filters: `?project=<slug>`, `?kind=html|video`, `?mine=true`
- `POST /api/walkthroughs/` — Upload (multipart). Fields: `file`, `title`, `kind` (html|video), optional `description`, `project_slug`, `visibility` (`private` | `link`; `link` mints a share token)
- `GET /api/walkthroughs/<uuid>/` — Detail. `auth=None`: public (`visibility=link`) walkthroughs require `?t=<share_token>` for anonymous read — a missing/wrong token 404s, same as private (no existence leak). `is_owner` flag tells the UI which toolbar to render; owners additionally get `share_url` (the absolute `.../walkthrough/<uuid>?t=<token>` link)
- `PATCH /api/walkthroughs/<uuid>/` — Owner-only update of title/description/project_slug/visibility. Flipping to `link` mints a token if none exists; flipping to `private` keeps the existing token (re-publishing later revives the same link)
- `DELETE /api/walkthroughs/<uuid>/` — Owner-only. Deletes Drive file and the row
- `POST /api/walkthroughs/<uuid>/rotate-token` — Owner-only; re-mints the token, killing shared links
- `GET /walkthrough/<uuid>/content` — Streams file bytes. Session-auth OR (`visibility=link` AND correct `?t=<share_token>`). Range-aware (supports `<video>` scrubbing). Reclaimed from `/w/<uuid>/content` when `/w/` became the tenant prefix; the legacy `/w/<uuid>/content` path 302-redirects here (`RedirectView`, query string preserved)

Walkthrough visibility is **token-gated**: `visibility=link` means "anyone holding the current share token", not "anyone with the bare URL" — anonymous read of the detail GET and the content stream both require `?t=<share_token>`; the token is minted on publish and backfilled onto pre-existing `link` rows by migration `0008_mint_share_tokens`. Owners see `share_url` in the detail response and can rotate it via the endpoint above, which kills previously shared links without deleting the artifact or changing where it lives. **Reviews remain tokenless** (unchanged — see below). See `docs/superpowers/specs/2026-07-13-walkthrough-share-token-revival-design.md`.

Settings:
- `WALKTHROUGHS_ENABLED` (default `True`) — `/api/walkthroughs/` and `/w/<id>/content` 404 when off
- `CANOPY_DRIVE_SA_KEY_JSON` — Google Drive service-account key (JSON string). Empty disables uploads/streams (500 with `code=drive-not-configured`)
- `CANOPY_DRIVE_ROOT_FOLDER_ID` — Shared-drive folder ID. `walkthroughs/<uuid>/` subfolders are created under it
- `WALKTHROUGH_MAX_UPLOAD_BYTES` (default 75 MB)

### Debug access (`apps/common/views_debug`)
- `POST /api/debug/mint-session/` — authenticated user mints a short-lived Django session cookie (body: `{ttl_seconds: int}`, clamped to 60s–1w). Returns cookie + curl example. Used to hand access to an AI assistant without going through OAuth. UI lives at `/settings` → "Debug access".

### Reviews (`apps/reviews`) — DDD narrative review surface
- `GET /api/reviews/` — List review requests (the `/ddd` dashboard)
- `POST /api/reviews/` — Create a review request (DDD orchestrator)
- `GET /api/reviews/{rid}/` — Get review detail or poll for resolution
- `POST /api/reviews/{rid}/submit/` — Submit approve/redraft decisions + narration edits (human → server)
- `DELETE /api/reviews/{rid}/` — Delete a review request (dashboard cleanup)

Reviews are tokenless. `visibility=link` reviews are readable by anyone with the URL — the auth middleware lets anonymous holders through the `/review/:id` shell + the per-review read API (which self-enforce). **Submitting** a decision always requires a Dimagi login (public-readable never grants anonymous write).

**Run-child gates belong to no narrative.** `RUN_CHILD_GATES` (`apps/common/ddd.py` — `product_findings` today) hang off the *run*, not the narrative timeline: `create_review` stores `narrative_slug=None` + `version=0`, the serializers never re-derive a slug from the run_id for them, and `/review/:id` renders them **standalone** (no DDD rail). In `apps/runs/aggregate.py` they may **attach** to a narrative that already exists but never **create** one — because the gate can't discriminate: a DDD findings review and Ada's fleet audit both use `product_findings`, but only the former is a child of a real run. Without that rule, parsing a slug out of any run_id conjured a phantom narrative into the DDD rail (active, empty, unnavigable). Note `_NON_NARRATIVE_GATES` (aggregate) is a *superset* — `external_release` isn't a narrative *version* but does belong to a narrative, so it may create one.

### DDD runs (`apps/runs`, mounted at `/api/ddd`)
- `GET /api/ddd/narratives/` — List DDD narratives
- `GET /api/ddd/narratives/{slug}/` — Get a narrative + its runs (grouped by version)
- `GET /api/ddd/runs/{run_id}/` — Get a run package (video + deck + narrative + links)
- `PATCH /api/ddd/narratives/{slug}/visibility/` — Set Public/Private for an entire narrative; cascades visibility to every walkthrough + review under the slug (auth required). The narrative detail response carries a computed `visibility` (`public` / `private` / `mixed`)
- `DELETE /api/ddd/runs/{run_id}/` — Delete a run (cascades its walkthroughs + reviews)
- `DELETE /api/ddd/narratives/{slug}/versions/{version}/` — Delete a narrative version (and its runs)
- `DELETE /api/ddd/narratives/{slug}/` — Delete an entire narrative (all versions + runs)

The narrative is identified by `narrative_slug` (decoupled from `run_id`); a server backstop rejects narrative-less package artifacts.

### Shareouts (`apps/shareouts`)
- `GET /api/shareouts/` — List shareouts (teammate-facing work briefings, timestamped per window)
- `POST /api/shareouts/` — Create shareouts (batch; idempotent per `period`+`source`)
- `POST /api/shareouts/clear/` — Clear shareouts by source / project / date (AND-combined)

### Agents (`apps/agents`) — first-class AI-agent workspace
An `Agent` (e.g. "Echo") is a first-class entity — distinct from a code Project — with its own Google-Doc syncs, work products, skill catalog, packaged turns, and an actionable task board. The **DB is the source of truth**; the board renders by "who has the ball" (the agent vs a human). A human's board action POSTs a *command*; the agent drains pending commands on its next turn and marks them applied (`result_note` + `applied_at`). All routes are session-authed and `x-mcp-expose`d.
- `GET /api/agents/` — List agents
- `POST /api/agents/` — Create or update an agent (upsert by slug)
- `GET /api/agents/{slug}/` — Agent detail (with counts, incl. `turn_count` + `latest_turn_at`)
- `GET|POST /api/agents/{slug}/turns/` — List / package an `AgentTurn`: the request(s) a turn advanced (`task_ext_ids`) → what it did (`title`/`summary`) → deliverables (`work_product_urls`) → optionally a `/share/<token>` transcript link (uploaded to the sessions app; the turn only holds its `slug`/`share_token`, so the apps stay decoupled). Idempotent per `(agent, cli_session_id)`. Drives the **Turns** rail section
- `GET|POST /api/agents/{slug}/syncs/` — List / post a Google-Doc manager sync (idempotent per period+source)
- `GET|POST /api/agents/{slug}/work-products/` — List / upsert work products (by url)
- `GET|PUT /api/agents/{slug}/skills/` — List / replace (PUT) the skill catalog so it mirrors the repo
- `GET /api/agents/{slug}/tasks/` — List the board
- `POST /api/agents/{slug}/tasks/sync` — Upsert tasks from the (legacy) source sheet (non-destructive)
- `POST /api/agents/{slug}/tasks/` — Create a task
- `PATCH /api/agents/{slug}/tasks/{task_id}/` — Update a task
- `POST /api/agents/{slug}/tasks/{task_id}/commands` — Post a board action (`accept`/`decline`/`dispatch`/`reassign`/`edit`/`comment`/`done`); some apply immediately server-side, `accept`/`dispatch` also queue agent work
- `GET /api/agents/{slug}/commands` — List commands (the agent reads `?status=pending`; each carries `result_note` + `applied_at`)
- `POST /api/agents/{slug}/commands/{cmd_id}/apply` — Mark a command applied (the agent calls this after acting)
- `GET|POST /api/agents/{slug}/schedules/` — list / create a **recurring turn** (cron + IANA tz). Fires onto the normal harness turn path; the `Turn` *is* the occurrence (`origin=cron`, `idempotency_key="sched:<id>:<slot>"`). Firing slot N+1 supersedes slot N's unfinished turn as `MISSED` — you only ever owe the newest.
- `PATCH|DELETE /api/agents/{slug}/schedules/{id}` — edit / remove
- `POST /api/agents/{slug}/schedules/{id}/run-now` — trigger off-cycle (`origin=manual`; supersedes an open occurrence, but never advances `last_slot` — the cadence is unaffected)
- `POST /api/agents/{slug}/schedules/preview` — preview the next 3 fire times for a cron+tz pair, computed with the same `next_slots()` the firing path uses (so the client never re-implements cron)
- `GET /api/agents/schedules/week?start=<iso>` — a week of scheduled fires across the visible fleet, driving `/schedules` + `/w/:workspace/schedules`. Scope follows the URL (flat = all your workspaces, `/w/{ws}/` = that one); fires computed via `canopy_cron.slots_between`
- `GET|PUT /api/agents/{slug}/runners` — read / wholesale-replace the agent's **ordered runner assignment list** (index = rank); edited in place on the Runners tab (`RunnerDetail`'s per-agent `RunnerAssignments` editor, expanded by default) — there is no separate routing tab. Each row carries `enabled` (default `true`); a disabled row is **kept** — rank preserved, rendered greyed — but never routes (the claim path excludes it entirely; it also doesn't count as a better-ranked availability blocker for a lower rank). `PUT` accepts either `runners` (ordered rows, each with its own `enabled` — the toggle UI's write shape) or the legacy `runner_ids` (ordered ids, all implicitly enabled); exactly one of the two must be provided (422 otherwise). `PUT` replaces all rows in one transaction (no per-chip endpoints); an unknown/retired/invisible runner id 422s. A disabled row stays fully **drillable** (`POST .../drill` fans out over every assigned row regardless of `enabled` — drill-before-enable is the intended workflow). THE routing authority for agent turns — see the Harness section below and the Design Decisions bullet.
- `PATCH /api/agents/{slug}/runner-preference` — **DEPRECATED**: superseded by the routes above; no longer read by claim routing; kept one release (returns a deprecation note in its docstring), then removed.

### Agent runs (`apps/agent_runs`, mounted under `/api/agents`)
The unified agent **run lifecycle** (run → step → artifact → verdict/QA → decision → gate → fork) as a storage-agnostic read model behind a `RunStore` Protocol (DB adapter persists rows; Drive adapter reads ACE's YAML). The keystone of the framework harvest — see `docs/superpowers/specs/2026-06-29-unified-agent-run-lifecycle-design.md`. Backed by the installable Django-free `canopy_agent_runs` library (`packages/canopy_agent_runs`).
- `GET /api/agents/{slug}/runs/` — List an agent's runs (paginated)
- `POST /api/agents/{slug}/runs/` — Create a run
- `GET /api/agents/{slug}/runs/{run_id}/` — Full run read model
- `GET /api/agents/{slug}/runs/{run_id}/steps/` — A run's steps
- `POST /api/agents/{slug}/runs/{run_id}/steps/{step_key}/gate` — Record a gate decision on a step
- `POST /api/agents/{slug}/runs/{run_id}/steps/{step_key}/verdict` — Record a step verdict (QA/eval aggregate)
- `POST /api/agents/{slug}/runs/{run_id}/fork` — Fork a run at a step boundary

### Harness (`apps/harness`, mounted at `/api/harness`) — runner registry + turn lifecycle
The agent-execution control plane: paired `Runner`s (laptop emdash daemons, cloud containers) heartbeat and claim queued `Turn`s; a `Turn` is the execution envelope for one unit of agent work, with an append-only `TurnEvent` ledger. See `docs/superpowers/specs/2026-07-05-agent-execution-control-plane-design.md`.
- `POST /api/harness/runners/` — Pair a runner
- `GET /api/harness/runners/` — List my runners
- `POST /api/harness/runners/{runner_id}/heartbeat` — Heartbeat (status + active turns)
- `POST /api/harness/runners/{runner_id}/claim` — Claim the next eligible queued turn
- `POST /api/harness/runners/{runner_id}/resolve-session` — Resolve whether this runner can reuse an existing emdash session for an (agent, thread) pair
- `POST /api/harness/runners/{runner_id}/record-session` — Record a session's durable link + live-session hint after create/reuse
- `POST /api/harness/runners/{runner_id}/sessions` — The runner reports its open emdash sessions (wholesale per runner); the payload also carries `archived: [<emdash task name>]` alongside `sessions` — the closing signal that lets the server retire a row instead of inferring it from absence
- `POST /api/harness/turns/` — Enqueue a turn (idempotent per `idempotency_key`)
- `GET /api/harness/turns/` — List turns (filter: `?agent=<slug>`, `?status=<…>`)
- `GET /api/harness/turns/{turn_id}` — Get a turn
- `GET|POST /api/harness/turns/{turn_id}/events` — Read / append the turn's event ledger
- `POST /api/harness/turns/{turn_id}/start` — Mark a claimed turn running
- `POST /api/harness/turns/{turn_id}/finish` — Finish a turn (`done`/`failed`)

**Directed routing (`RunnerAssignment`) is THE routing authority for agent turns** — a per-agent ordered runner list, replacing `capabilities.agents` + `Agent.runner_preference` (kind-level; deprecated, see Agents above). Claim-time cascade: the highest-ranked *available* (`live_status=ONLINE` + `ready`) **enabled** assigned runner claims; a lower rank takes over the moment a higher one goes unavailable or disabled, or after `CASCADE_GRACE_SECONDS = 60` queued even if the higher rank still looks online (a wedged-but-heartbeating runner can't stall the queue forever). `RunnerAssignment.enabled` (default `true`) is a toggle, not a removal — a disabled row keeps its rank and stays visible (greyed in the UI) but is excluded from both legs of the cascade: it never claims, and it never counts as a better-ranked blocker for the next enabled rank. An agent with no assignment rows — or only disabled ones — is **explicitly unroutable** (surfaced in UI, never silently defaulted). `Turn.pinned_runner` hard-pins a turn to one runner, bypassing assignments/capabilities but never the tenant gate or the one-executing-turn constraints (producers: drills, chat "wait"/"continue", directed session starts; `Turn.origin="drill"` marks drill turns). Session turns add stickiness on top: a session bound to a `RunnerBinding` claims only on that binding's runner; if the binding holder is unavailable, no other runner auto-claims — the turn waits for a chat-side placement decision (see Chat below). See `docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md`.
- `POST /api/harness/runners/{runner_id}/drill` — Owner-gated: fan out a read-only doctor/preflight turn per agent (default = every agent assigned to this runner), pinned to it. Proves a standby can actually reach canopy-web and execute for an agent without granting it real routing weight.
- `GET /api/harness/runners/{runner_id}/drills` — Per-agent drill grid (outcome, summary, timestamps) for this runner; `RunnerOut.drill_rollup` (`passed`/`failed`/`pending`/`last_finished_at`) surfaces the badge summary on `GET /api/harness/runners/`.
- `POST /api/harness/drills/{drill_id}/report` — Agent-callback: the drilled agent POSTs its own `{outcome: "pass"|"fail", summary}` using its runner environment's bearer token — the callback itself proves that environment can reach canopy-web. A drill turn that fails without a report is marked `fail` by `finish_turn`.

**Recurring turns** — the runner-facing half of scheduling; the supervisor's CRUD is the `/api/agents/{slug}/schedules/` surface above. `runner_id` is a query param on both routes; the tenant is derived from `runner.paired_by` (the human who paired the runner) rather than the `Runner.workspace` FK — see the Design Decisions entry below.
- `GET /api/harness/schedules/?runner_id=…` — runner syncs the schedules it may fire. **Tenant-scoped, never scoped by `capabilities`** (a caller-supplied hint, not a boundary — see b4f5ead).
- `POST /api/harness/schedules/{id}/fire?runner_id=…` — the runner reports a due slot; the server materializes the turn.

Firing is automatic: on each poll tick the runner syncs its schedules, evaluates every cron with `canopy_cron.due_slot(cron, tz, after=fire_after)`, and POSTs any due slot to `fire`. The anchor is the server-computed `ScheduleOut.fire_after` (`= last_slot or created_at`), never `last_slot` — see the Design Decisions entry below. Both halves of the slot math are backed by the installable Django-free `canopy_cron` library (`packages/canopy_cron`): the server's `preview` endpoint and the runner's firing call the **same** `next_slots()` / `due_slot()`, so the UI cannot promise "Fridays" while the runner fires Thursdays. It also owns the `croniter>=6.0,<7.0` bound, once, where the DST/slot semantics live.

> **Operational note — deleting a user bricks their runners' schedules.** `Runner.paired_by` is `on_delete=SET_NULL`, and `_runner_schedule_qs` derives the schedule tenant from it, failing closed when it is NULL: deleting a pairing user's Django `User` orphans their runners, and every schedule route then returns nothing for that runner, forever. The runner must be re-paired (a new row); the orphan can only be retired. This is correct — a runner with no owner has no tenant to derive, and inferring one would be privilege escalation — so prefer deactivating a departing user (`is_active=False`) over deleting them if their runners should keep running.

### Items (`apps/harness`, mounted at `/api/agents/{slug}/items/` + `/api/items/{id}/`)
An `Item` is **a thing that needs addressing — the dual of `Turn`**: `Turn` is work an agent does, `Item` is work *you* do. They cycle: a turn raises items (`Item.raised_by`) → you decide → an approved item's `dispatch` enqueues turns (`Turn.raised_from`). `TurnSpec.target_agent=""` means **self** (the default); Ada's cross-agent fan-out is that field set — a parameter, not a code path.

The Item **carries its own text** (message semantics, like an email) rather than resolving a subject: `origin_ref` is provenance, not identity, and nothing resolves it to render the row. That is what keeps the model free of a source registry, of drift, and of any framework→product import. Decisions are a **closed set** (`implement | skip | defer`) so a generic inbox can render buttons for an item it has never seen; only `implement` dispatches. `kind ∈ {review, question}` — there is no `notify` item (that is `/timeline`). **decide + dispatch are one transaction**: a bad `target_agent` is a 422 on an item still `open`, never a decided-but-undispatched row that deciding-once (409) would strand forever. See `docs/superpowers/specs/2026-07-15-item-and-turn-design.md`.
- `GET /api/agents/{slug}/items/` — List an agent's items (`?state=`, `?kind=`, `?batch=`)
- `POST /api/agents/{slug}/items/` — Raise items (batch; idempotent per `idempotency_key`; the whole batch commits in one transaction, so N items push once)
- `GET /api/items/` — **Fleet inbox**: open items across every agent you can see, ranked `review → question` then oldest-first (`?state=`, `?kind=`). Drives `/supervisor` and the per-agent Inbox. Replaced the old `needs_you` aggregation
- `GET /api/items/{id}/` — Get an item
- `POST /api/items/{id}/decide` — Decide (`implement` dispatches; 409 if already decided; 422 rolls back a bad spec)
- `POST /api/items/{id}/dismiss` — Dismiss (never dispatches)

**The inbox is a pure `Item` query — no projections.** The supervisor inbox (`/supervisor`) and the per-agent **Inbox** rail both render `Item.filter(state=open)`, decidable in place (`decide`/`dismiss` inline). The old `needs_you` aggregation and its projections (`SUGGESTED`/human-blocked tasks, run gates/failed steps, the schedule nag) were **deleted** — "needs you" was never a first-class concept, just a label on a function. Producers now raise real Items: the **schedule nag** is server-local (an unattended grace-released occurrence raises a `review` Item whose `implement` re-runs the schedule — `services._raise_schedule_nag`, dismissed on a later `DONE`); **run gates** and **task decisions** are raised by their producers (the runner's `reviews.py`, the fleet `task-tracker` skill) as follow-on repo work — the inbox simply shows whatever Items exist. The task board (`AgentTask`) stays as the "who has the ball" surface but no longer feeds the inbox. See `docs/superpowers/specs/2026-07-21-supervisor-inbox-items-only-design.md`.

### Chat (`apps/canopy_sessions`) — multiplayer chat sessions (the live front-door)
A `Session` is a durable conversation **with an agent** (agent-agnostic, workspace-tenanted). A send commits the co-edited `Draft` → enqueues a **session `Turn`** (a third `Turn` target: agent XOR project XOR session) → a session-capable runner executes it and the reply streams back over the ledger; `Message` rows are a materialized projection. The per-session WebSocket `SessionConsumer` (`ws/canopy-sessions/{id}/`) carries the connect snapshot, co-edited draft (version guard + derived soft-lock), presence, and the streamed reply — speaking ace-web's **canonical protocol** (`session.state`/`chat.stream_*`/`draft.*`/`presence.*`) so the shared `canopy-ui/chat` kit drives it and ace-web can adopt the same kit. Fan-out is generic (`apps/realtime`); the consumer translates `TurnEvent`s → `chat.stream_*`.
- `POST /api/canopy-sessions/` — Create a session (`agent_slug`, `title`, `metadata`, optional `runner_id` — the composer's "Run on" picker; pins the session's first turn, and the `RunnerBinding` then forms on that runner so stickiness takes over); tenant route `/api/w/{ws}/canopy-sessions/` creates in that workspace (cross-workspace new-chat).
- `GET /api/canopy-sessions/?state=active|archived|all&limit=<n>` — List sessions (default `active`). A session is archived when it is **written** so (the runner reported its emdash task archived, or `/archive` was called) **or derived** so (a `runner`-origin session whose binding has not been seen within `SESSION_STALE_AFTER` = 3 days). The derived half is recomputed every read, so a reappearing task un-archives itself; web sessions are exempt from it. Nothing is ever deleted.
- `POST /api/canopy-sessions/{id}/archive` · `POST /api/canopy-sessions/{id}/unarchive` — Retire / restore a session by hand (idempotent; the escape hatch for web chats, which no runner will ever close).
- `GET /api/canopy-sessions/{id}` — Session + transcript. `POST /api/canopy-sessions/{id}/send` — Send a message (`text`, `client_id`, optional `placement`: `"wait"` pins to the offline bound runner, `{runner_id}` re-pins elsewhere).
- `POST /api/canopy-sessions/{id}/place` — The chat banner's after-the-fact placement decision on an already-queued turn (bound runner went offline mid-flight) — same `placement` shape as `send`, applied to the existing turn rather than a new one.

**Execution:** `CHAT_STUB_EXECUTOR` (default `True` in dev; **`False` on labs**) — off means a session `Turn` stays QUEUED for a **session-capable** runner (`capabilities.sessions:true`) rather than the inline stub. The laptop runner (`packages/canopy_runner`) drives the agent's emdash session and **bridges** the reply back: `execute_chat_turn` + `chat_bridge.py` tail the Claude transcript and post assistant text as `TurnEvent`s. Chat therefore depends on a session-capable runner being online (else the turn waits). See the reusable-chat-kit spec + `2026-07-16-*` Wave-4 specs.

**Live sessions on the phone (WS-push):** the supervisor "Sessions" tab shows all open emdash sessions **grouped by project, across every live runner** (`services.list_visible_sessions`), with the real emdash task tag. The runner reports **change-driven** (the instant a transcript grows — byte-offset `tail.TailReader`), and `harness.signals.sessions_reported` → `apps/realtime` fans the owner's sessions as a `supervisor.sessions` frame to `supervisor.user.{id}` — one broadcast to every open device instead of each polling (`useLiveSupervisor.sessions` → `OpenSessions`).

### Sessions (`apps/session_sharing`) — shared Claude Code transcripts
Token-based session sharing (the `/canopy:share-session` flow); the app was renamed from `sessions` to `session_sharing` to free the `sessions` name for the live-session harness. Routers still mount at `/api/sessions` + `/api/share`. This is a **separate** token model from the visibility gating above (token-gated walkthroughs / tokenless reviews) — shared sessions (and arcs) carry their own rotatable `share_token`.
- `POST /api/sessions/upload` — Upload a Claude `.jsonl` transcript (multipart)
- `GET /api/sessions/` — List my shared sessions
- `GET /api/sessions/{slug}` — Get one session (owner)
- `PATCH /api/sessions/{slug}` — Update a session (owner)
- `DELETE /api/sessions/{slug}` — Delete a session (owner)
- `POST /api/sessions/{slug}/rotate-token` — Rotate the share token (owner) — invalidates the old `/share/<token>` link

**Arcs** — a multi-session "arc" groups several transcripts into one shareable build (the `/share` for a whole build):
- `POST /api/sessions/arcs` — Create an arc
- `GET /api/sessions/arcs` — List my arcs (filter: `?project=<slug>`)
- `GET /api/sessions/arcs/{slug}` — Get one arc (owner)
- `PATCH /api/sessions/arcs/{slug}` — Update an arc (owner)
- `DELETE /api/sessions/arcs/{slug}` — Delete an arc (owner)
- `POST /api/sessions/arcs/{slug}/rotate-token` — Rotate an arc's share token (owner)

- `GET /api/share/{token}` — Public read-only view of a shared session (no login; `share_router` mounted at `/api/share`, drives `/share/:token`)

### Timeline (`apps/timeline`)
- `GET /api/timeline/` — Team activity timeline. Generic activity-log aggregation that reads other apps' events via a string registry (cursor-paginated; link-out only). See `docs/superpowers/specs/2026-06-19-team-activity-timeline-design.md`.

### Push (`apps/push`) — Web Push for `/supervisor`
- `GET /api/push/vapid-public-key` — The VAPID public key the browser needs to subscribe
- `POST /api/push/subscribe` — Register this browser (upsert by endpoint)
- `DELETE /api/push/subscribe` — Unregister this browser (idempotent)

Empty `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY` disable push: the endpoints 503 and nothing ever sends.

### System (`apps/system`)
- `GET /api/system/overview` — Capability catalog: the canopy plugin's skills/agents/commands, read live from the plugin.
- `GET /api/system/{kind}/{name}` — Capability detail for one skill/agent/command. Drives the `/system` Workflows view.

### MCP (`apps/mcp`, mounted at `/api/mcp/`)
Not a Ninja router — a FastMCP 3.x Streamable-HTTP ASGI app mounted in `config/asgi.py`. Auth is enforced inside the server via `MultiAuth` (per-user PAT `CanopyPATVerifier`, always on; interactive Google OAuth is an env-gated seam, `MCP_OAUTH_ENABLED`). Every tool call writes an `MCPAuditLog` row; mutating tools are rate-limited per user. Tools today: `list_insights` + `clear_insights` (insights), and `list_schedules` / `preview_cron` (read) + `create_schedule` / `update_schedule` / `delete_schedule` / `run_schedule_now` (write) for recurring turns. The schedule tools call `apps/harness/schedule_services.py`, the same request-free service layer the REST routes call, so the MCP and REST surfaces can't drift. The legacy single-shared `CANOPY_MCP_BEARER` and the hand-rolled ASGI gate are gone. See `docs/architecture/mcp-surface.md`.

## Design Decisions

- **API is Pydantic-first via Django Ninja**: every request/response is a Pydantic v2 model declared in `apps/<app>/schemas.py`. Routes live in `apps/<app>/api.py`, registered on the single `NinjaAPI` instance in `apps/api/api.py`. Errors are RFC 7807 `application/problem+json`. Frontend types are generated from the OpenAPI 3.1 schema by `openapi-typescript` into `frontend/src/api/generated.ts` and consumed via `openapi-fetch`. **When you change an `apps/**/schemas.py` or `api.py`, regenerate the types and commit them: `cd frontend && npm run gen:api` (backend up on :8000) or `npm run gen:api:local` (against a dumped `openapi.json`).** The `regen-openapi.yml` workflow VERIFIES they're fresh on every such PR and fails if `generated.ts` is stale — it does NOT commit for you (an auto-commit pushed with `GITHUB_TOKEN` can't trigger the required CI checks, which used to leave the PR head unchecked and block the merge).
- **Streaming endpoints stay on Django**: `GET /walkthrough/<uuid>/content` (the walkthrough viewer) is a bare Django view at `apps/walkthroughs/streaming.py` — HTTP Range support (for `<video>` scrubbing) doesn't fit the Ninja contract. It is the only `StreamingHttpResponse` left now that the co-authoring workspace SSE engine has been retired. (Reclaimed from `/w/<uuid>/content` by the tenancy migration; the legacy path 302-redirects.)
- **Bare Django views**: `/api/csrf/`, `/api/debug/mint-session/`, `/auth/cli/authorize/`, and `/health/` (the last is also Ninja-mountable via `public_router`) — they manipulate sessions/cookies/redirects directly. Matched in `config/urls.py` BEFORE the Ninja `/api/` catch-all so they don't get shadowed.
- **MCP is in-process FastMCP, not OpenAPI-derived**: `apps/mcp/` mounts a FastMCP 3.x Streamable-HTTP server at `/api/mcp/` whose tools are explicit Python functions calling the same service layer as the REST views (no HTTP self-loopback). Auth is per-user PAT inside the server (fail-closed), every call is audited, and writes are rate-limited.
- **Push fires on an increase, never a decrease**: the fleet's waiting set is a **count** (open `Item`s per agent), not a single event, so nothing naturally emits "the fleet needs you now." `AgentWaitingSnapshot` (`apps/push`) holds the last count per agent; a single `post_save`/`post_delete` receiver on `Item` marks its agent dirty and `transaction.on_commit` coalesces everything dirtied in one transaction into a single recompute per agent — so a batch of N items (a fleet audit) still sends at most one push (`create_items` commits the batch in one transaction). A drop in count updates the snapshot but never sends. Because the waiting set is a single real table (`Item`), there are no per-producer hops and no Drive-backed staleness — the old known gap (Drive-run-store gates never pushing) is gone with the projections. See `apps/push/signals.py`.
- **Visibility is Public/Private, token-gated for walkthroughs, tokenless for reviews:** `visibility=link` for **walkthroughs** requires `?t=<share_token>` for anonymous read (detail GET + content stream) — a missing/wrong token 404s, same as private, so existence never leaks; `share_url` is returned to owners only, and `POST /api/walkthroughs/{wid}/rotate-token` re-mints the token to kill shared links without touching the artifact. `visibility=link` for **reviews** stays tokenless — "anyone with the URL" is still sufficient for review *read*; only review *submit* and all mutations require auth. `private` is Dimagi-OAuth-gated and 404s to anonymous for both artifact types. The login middleware (`apps/common/middleware.py`, default-deny with an allowlist) allowlists the `/walkthrough/` viewer shell + `/walkthrough/<uuid>/content` stream + walkthrough detail GET + the legacy `/w/<uuid>/content` redirect (alongside the `/review/` + `/share/` allowlists) — the API layer self-enforces the token check underneath the allowlist. Note `/w/` itself now means the **authed** workspace tenant shell and is NOT public. The narrative-level toggle (`PATCH /api/ddd/narratives/{slug}/visibility/`) cascades to every artifact + review under a narrative. See `docs/superpowers/specs/2026-07-13-walkthrough-share-token-revival-design.md` (walkthroughs — supersedes the tokenless model for that artifact type) and `docs/superpowers/specs/2026-06-08-tokenless-narrative-visibility-design.md` (original tokenless design, still current for reviews).
- **Shared frontend kit (`canopy-ui`):** the DDD and Agent workspaces share a two-pane (left rail + scrolling main) shell plus the broader design-system primitives, all extracted to `frontend/packages/canopy-ui` (imported as `canopy-ui` / `canopy-ui/ui` / `canopy-ui/lib`; published to public npm as `canopy-ui`, also mirrored as `@marshellis/canopy-ui`). Started life as `@canopy/workbench` (just the Workbench shell) and was expanded + renamed in 0.2.0→0.3.0. Surfaces consume it instead of re-implementing chrome, and use semantic design tokens (`bg-card` / `border-border` / `text-foreground` / `text-muted-foreground` / `text-primary`) — not raw `stone-*`/`orange-*` palette literals. See `docs/superpowers/specs/2026-06-17-shared-workbench-package-design.md`.
- **Light + dark themes via one token set.** The app ships **both** themes off the same semantic tokens in `frontend/src/index.css`: `:root` = Warm Earth **light**, `.dark` = Warm Earth **dark** (the default). `index.html` applies `.dark` before first paint (default dark; an explicit `light` choice in `localStorage` removes it — no flash), and `src/theme/ThemeProvider.tsx` (`useTheme` / `<ThemeToggle/>`, mounted in the `AppLayout` header) toggles + persists the class on `<html>`. Because components only reference token names (never `dark:` variants), both themes "just work." The light palette deepens brand/status hues (~600) for contrast on white.
- **Design tokens are the single source of truth (no raw palette literals).** The whole authenticated app styles off the semantic tokens (`@theme inline` maps `--color-*` → the per-theme vars). Do **not** introduce raw Tailwind palette literals (`stone-*`, `orange-*`, `zinc-*`, `slate-*`, `red-*`, `amber-*`, `emerald-*`, `sky-*`, `violet-*`); use the tokens:
  - **Surfaces:** `bg-background` (page), `bg-card` (cards/popovers), `bg-muted` (fills/hover), `bg-input` (elevated controls). Borders: `border-border` (default), `border-input` (elevated/controls). Brand: `bg-primary` / `text-primary` / `border-primary` (+ `hover:bg-primary/90` for the button-hover darken).
  - **Text emphasis ladder** (brightest → dimmest): `text-foreground` (primary/headings) → `text-foreground-secondary` (secondary/body) → `text-muted-foreground` (meta/captions) → `text-foreground-subtle` (faint).
  - **Status / categorical accents:** `success` (emerald — opportunity), `warning` (amber — ship-gap), `info` (sky — alignment), `special` (violet — pattern), `destructive` (red — errors). Each has a `-foreground` for solid fills; tinted badges use `bg-<token>/10 text-<token> border-<token>/30`.
  - **Exception:** `/share/:token` (`SessionSharePage` + the `transcript/` components) is a deliberate **light-themed** public viewer mounted outside the app shell (`bg-white`); it intentionally uses neutral literals and is the one surface that does not consume the dark token set.
- APP UI: dense, readable, tables not cards
- SSE streaming for AI responses (Scout pattern)
- **Auth:** Google OAuth via django-allauth (allowed-domain restricted via `AUTH_ALLOWED_EMAIL_DOMAIN` — comma-separated list, default `dimagi.com`; `dimagi-associate.com` is also allowed). Personal Access Tokens (`apps/tokens/`) authenticate machine callers via `Authorization: Bearer <raw>` — `BearerTokenAuthMiddleware` resolves them upstream of `LoginRequiredMiddleware`. `/api/debug/mint-session/` lets an authenticated user mint a short-lived session cookie to hand to an AI assistant.
- **Multi-tenancy (Workspace is the tenant anchor):** `Workspace` (`apps/workspaces` — members owner/editor/viewer + email invites) anchors every surface that owns tenant data. `agents` + `agent_runs` carry a `workspace` FK, and the tenancy rollout (PR #183) added the same FK + backfill migration to the product roots `projects`, `walkthroughs`, `reviews`, and `shareouts`; their authenticated queries filter by `request.workspace_slug`. `harness` also carries a `Runner.workspace` FK (nullable, `PROTECT`), but the actual `claim_next_turn` tenant gate is the **pairing human's** workspaces — `wsvc.user_workspace_slugs(runner.paired_by)` — **not** the `Runner.workspace` FK. A single runner serves a fleet that spans workspaces (agents each link to their own), so scoping by the one-workspace FK once took prod down (a runner backfilled onto `dimagi` while `ace`/`ada`/`echo`/`hal` live in `connect` → 4 of 5 agents' turns sat QUEUED forever); `paired_by` is server-assigned from `request.user` at pairing (unlike `capabilities`, not attacker-controlled), and a NULL `paired_by` fails closed. This matches the schedules rule (`_runner_schedule_qs`) — see `apps/harness/services.py::claim_next_turn`. The caller-supplied `capabilities` routing hint is intersected with this tenant gate for **project and session** turns only — since directed runner routing, `capabilities.agents` no longer gates agent turns; `RunnerAssignment` does (see below). `Turn` deliberately has **no** workspace FK of its own — it derives its tenant one hop away via `turn.agent.workspace`. **Insights are deliberately excluded** (user-scoped, not tenant-scoped). Tenant surfaces live under `/w/:workspace/` (browser) and `/api/w/{ws}/` (API, via `WorkspaceResolveMiddleware`); a default workspace is assigned when unspecified and the flat `/api/…` routes stay as a compat shim, so the change was non-breaking / Echo-safe. Public `visibility=link` reads (token-gated for walkthroughs, tokenless for reviews) and review-submit-login are preserved through the scoping. See `docs/superpowers/specs/2026-06-30-workspace-multi-tenancy-design.md`.
- **Directed runner routing (`RunnerAssignment` is the routing authority):** per-agent turns no longer route by a runner's self-declared `capabilities.agents` or an agent's kind-level `runner_preference` — both are superseded by `RunnerAssignment`, an explicit per-agent **ordered** runner list (`GET|PUT /api/agents/{slug}/runners`) that only that agent's owner edits, in place on the Runners tab (no separate routing tab). Routing is an availability cascade (highest ranked online+ready+**enabled** runner claims; lower ranks take over on unavailability, disablement, or after a 60s grace) rather than a static preference, because the operating loop is "shift the fleet to the next account/box when the current one runs out of tokens" and the system needs to degrade automatically, not silently deadlock. `enabled` (default `true`) replaced removal as the row-level control (an operator-requested follow-up): hitting the toggle greys a runner out instead of dropping it from the list, so the rank is preserved and re-enabling doesn't require re-adding it; a disabled row stays fully drillable. `Turn.pinned_runner` and session-turn stickiness (bound to a `RunnerBinding`, no auto-failover — the user is asked) sit on top of the cascade for the cases where availability-by-rank is wrong (drills, an in-flight chat's live context, an explicit "run on X"). See `docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md`.
- **Scheduled turns are runner-fired, server-configured, and self-superseding:** `AgentSchedule` (`apps/harness`) holds cron config server-side so it is visible/editable in the Agent UI, but the runner evaluates the cron and POSTs a due slot — the scheduler is a *producer of turns*, not a second execution engine (no celery, no beat, no new deploy surface). `ScheduleOut.fire_after` (`= last_slot or created_at`) is the anchor the runner must pass to `due_slot(after=...)`, not `last_slot` directly — `last_slot` is NULL until the first fire, and looking backward with no lower bound would fire a fresh schedule for a slot that predates it. Both macOS-account runners may fire the same slot safely: the slot-derived `idempotency_key` collapses the race inside `enqueue_turn`. There is deliberately **no occurrence table** — the `Turn` is the occurrence (`latest_occurrence_turn` selects by `origin_ref__schedule_id`, scheduled or manual, regardless of status). Unattended occurrences are released as `MISSED` after `grace_minutes`, because an abandoned session otherwise wedges the agent forever via `one_executing_turn_per_agent` (the runner's heartbeat keeps renewing its lease, so the lease sweep never rescues it) — release is scoped to `EXECUTING` turns and anchored on `claimed_at` (not `created_at`, which measures *owed* time, not *held* time), and it runs lazily on the runner's **claim** tick (`claim_next_turn` → `release_stale_occurrence_turns_all()`), not the fire tick, since a weekly schedule's fire tick is too far apart to ever honor a short grace window. A grace-released occurrence raises a real `review` **`Item`** (`services._raise_schedule_nag`, honoring the schedule's `notify` channels) whose `implement` re-runs the schedule's prompt — the generic Item action replaces the old bespoke "Run now" button; `finish_turn` on a later `DONE` occurrence dismisses it (`resolve_schedule_nags`). So the nag reaches the **Inbox** rail as an ordinary item, with no scheduling-specific UI. See `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md` and `2026-07-21-supervisor-inbox-items-only-design.md`.
- **PWA navigate-fallback is fail-safe (allowlist SPA routes, not denylist server routes):** the service worker serves the precached SPA shell (`index.html`) for a navigation **only** when its path matches a known SPA route prefix; every other navigation goes to the network and reaches Django. This inverts the old "shell for everything minus a denylist" default, which silently swallowed any server route nobody remembered to denylist — a `<iframe src="/walkthrough/<id>/content">` (an iframe load **is** a navigation) rendered the whole SPA again inside itself (issue #345). The rule now fails safe: a **new server route** is excluded by construction (unknown ⇒ network); a **forgotten SPA route** only loses *offline* shell fallback (online it still resolves via the `spa_view` catch-all). The two regex lists (`NAVIGATE_FALLBACK_ALLOWLIST` / `NAVIGATE_FALLBACK_DENYLIST`) live in — and are unit-tested in — `frontend/src/pwa/navigation-fallback.ts`, and `vite.config.ts` imports them. Server-content streams that sit *under* an allowlisted SPA prefix (`/walkthrough/<id>/content`, legacy `/w/<uuid>/content`) are carved back out on the denylist (workbox: denylist wins), and those carve-outs end in `(?:\?.*)?$` because workbox matches `pathname + search` and the DDD console now embeds artifacts with a `?t=<share_token>`. See `docs/superpowers/specs/2026-07-23-pwa-navigation-fallback-fail-safe-design.md`.
- **Rolling sessions (`SESSION_SAVE_EVERY_REQUEST = True`):** Django's default sets the 2-week session expiry AT LOGIN and never extends it, so an installed PWA would log you out every fortnight no matter how often you opened it. Fix costs a session write on **every request**, on every surface, for every user — and changes a failure mode: any view with `except IntegrityError` now needs its own `transaction.atomic()` savepoint, or the session write `SessionMiddleware` makes on the way out hits a poisoned outer transaction instead of the intended 409/handled error. Worked example: `apps/projects/api.py::create_project`; pre-existing precedent: `apps/harness/services.py`.
- PostgreSQL on the shared labs RDS (`canopy_web` DB on `labs-jj-postgres`)
- Dual AI backend lets users run either against an API key or their own Claude Code subscription

## Reference Docs

Design **specs** (the "why" record) live in `docs/superpowers/specs/`. The executed **implementation plans** — point-in-time checklists that shipped as described — are archived under `docs/archive/plans/` (git-tracked, kept as historical record, not current-state); consult them only when you need the blow-by-blow of how something was built.

- `docs/architecture/mcp-surface.md` — MCP server surface: module layout, dual-auth model, audit + rate-limit, tool inventory
- `docs/superpowers/specs/2026-04-10-project-workbench-design.md` — Workbench design spec
- `docs/superpowers/specs/2026-04-14-google-oauth-auth-gate-design.md` — OAuth gate design spec
- `docs/superpowers/specs/2026-05-26-walkthrough-sharing-design.md` — Walkthrough sharing design spec
- `docs/superpowers/specs/2026-06-02-ddd-run-views-design.md` — DDD run views (narrative → run → package) design spec
- `docs/superpowers/specs/2026-06-03-ddd-narrative-run-versioning-design.md` — DDD narrative/version/run model design spec
- `docs/superpowers/specs/2026-06-08-tokenless-narrative-visibility-design.md` — Tokenless Public/Private + narrative-level visibility design spec (shipped, PR #105)
- `docs/superpowers/specs/2026-06-17-shared-workbench-package-design.md` — shared Workbench shell design (shipped as `@canopy/workbench`, since expanded + renamed to `canopy-ui`; PRs #123/#124)
- `docs/superpowers/specs/2026-06-19-team-activity-timeline-design.md` — `/timeline` team activity feed design (shipped, PR #138)
- `docs/superpowers/specs/2026-06-24-canopy-framework-harvest-design.md` — Canopy-as-the-framework harvest strategy (umbrella for Waves 0–4: the framework/product boundary + what moves out of ACE)
- `docs/superpowers/specs/2026-06-28-shared-agent-client-design.md` — Shared agent-client, the framework's first harvested piece
- `docs/superpowers/specs/2026-06-29-unified-agent-run-lifecycle-design.md` — Unified agent⊕run lifecycle (Wave 1 keystone; the `apps/agent_runs` + `canopy_agent_runs` library, shipped PR #154)
- `docs/superpowers/specs/2026-06-29-wave2-3-harvest-execution.md` — Wave 2/3 execution spec (run-step verdicts + multi-tenant workspaces; shipped PRs #158–#162)
- `docs/superpowers/specs/2026-06-30-workspace-multi-tenancy-design.md` — Workspace-as-tenant full multi-tenancy design (anchor-roots-inherit-children, `/w/` reclaim, path-prefix API; shipped PR #183)
- `docs/superpowers/specs/2026-07-05-agent-execution-control-plane-design.md` — Agent-execution control plane: paired `Runner`s heartbeat and claim queued `Turn`s, with an append-only `TurnEvent` ledger
- `docs/superpowers/specs/2026-07-13-walkthrough-share-token-revival-design.md` — Walkthrough share-token revival: anonymous read re-gated on `?t=<share_token>`; reviews stay tokenless (partially supersedes the 2026-06-08 spec)
- `docs/superpowers/specs/2026-07-14-canopy-mobile-design.md` — canopy-mobile: one `/supervisor` surface consumed by the phone (PWA), the menubar's WKWebView, and the desktop browser; drives Phases 2-5
- `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md` — Agent scheduled turns (recurring turns, supersede-as-give-up, the nag projection)
- `docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md` — Directed runner routing: `RunnerAssignment` as the per-agent routing authority (availability cascade + grace), `Turn.pinned_runner`, session-turn stickiness, readiness drills; supersedes `capabilities.agents` + `Agent.runner_preference` for agent-turn claim routing
- `docs/designs/canopy-web-design.md` — Product design + glossary (open claw, skill, collection, eval suite, workspace session)
- `docs/designs/ceo-plan-conversation-to-agent.md` — CEO review, scope decisions, deferred work
- `docs/walkthroughs/project-workbench.yaml` — Project workbench walkthrough spec
- `docs/case-studies/workbench-self-improvement.md` — Self-improvement case study
- `docs/personas/jonathan.md` — Primary user persona
- `TODOS.md` — Deferred V2 work (proactive detection, prompt hardening, OAuth integrations, multi-tenant auth, cowork adapter)
