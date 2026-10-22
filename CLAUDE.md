# Canopy Web

Collaborative web workspace for the canopy agent ecosystem — first-class AI agents, demo-driven development (DDD), walkthroughs, and shareouts.

> **How to write this file.** It is loaded into every session, so it holds only the current state an agent needs before it knows what it is working on: how to run, test and ship, and the invariants nothing may break — each as a rule plus a pointer. Subject detail goes in `docs/architecture/<topic>.md` (index below). History — what something replaced, what broke, when, who decided — goes in the PR description, not here or in the topic docs. `tests/test_claude_md_budget.py` fails above 60k characters or on a date or PR reference.

## Architecture

- **Backend:** Django 6 ASGI + uvicorn, Django Ninja 1.x + Pydantic v2, PostgreSQL (shared labs RDS, `canopy_web` DB). OpenAPI 3.1 at `/api/openapi.json` (Scalar at `/api/docs/`, Redoc at `/api/redoc/`). Errors are RFC 7807 `application/problem+json`. Frontend types are generated from the schema into `frontend/src/api/generated.ts` and consumed via `openapi-fetch`.
- **Frontend:** React 19 + Vite + Tailwind CSS 4 + shadcn/ui; shared kit in `frontend/packages/canopy-ui`.
- **AI:** canopy-web makes **no model calls of its own**. Every agent turn, chat reply and review runs on a **runner**, under that box's Claude login (`/supervisor` → Runners).
- **MCP:** `apps/mcp/` (FastMCP, Streamable HTTP, mounted at `/api/mcp/` in `config/asgi.py`) exposes every REST route as a tool, run in-process as the caller (OAuth or PAT) → `docs/architecture/mcp-surface.md`.
- **Framework/product boundary:** framework apps never import product apps; product imports framework freely. Every app's tier is in `ARCHITECTURE.md` and in `FRAMEWORK`/`PRODUCT` in `tests/test_architecture_boundary.py` — a new app must be tiered in both. Inside the framework, apps are layered (kit → core → features) and an import points only down; today's upward imports are a shrink-only ratchet in `tests/test_layered_app_imports.py` → `ARCHITECTURE.md` § Layers inside the framework.
- **Deployment:** one container (Django serves SPA + API + MCP) on AWS ECS Fargate in the shared labs account, at `https://canopy.dimagi.com/`. Merging does NOT deploy: `gh workflow run "Deploy to Labs (AWS)" --ref main` (main only; migrations run automatically; CloudFormation owns the task def + service — never `ecs update-service` by hand).
- The old address `labs.connect.dimagi.com/canopy/` still serves machine traffic (API, WS, OAuth, health) in place and 302s browser pages to canopy.dimagi.com — don't turn machine paths into redirects; docs must name the new address (`tests/test_old_address_in_docs.py`).
- **IAM:** canopy-web's deploy role must trust the IMMUTABLE OIDC subject `repo:dimagi-internal@272902307/canopy-web@1193139337:ref:refs/heads/main` (name-based silently fails); never attach `SecretsManagerReadWrite` to the shared task-execution role. → `docs/architecture/deploy.md`

## Development

Backend uses [`uv`](https://docs.astral.sh/uv/) (`uv.lock` is committed).

```bash
cp .env.example .env
uv sync --extra dev
uv run python manage.py migrate
uv run python manage.py runserver
cd frontend && npm install && npm run dev      # frontend
uv run honcho start -f Procfile.dev            # both
docker compose up                              # backend + frontend + Postgres

gh workflow run "Deploy to Labs (AWS)" --ref main                          # deploy (migrations run automatically)
gh workflow run "Deploy to Labs (AWS)" --ref main -f skip_migrations=true  # EMERGENCY ONLY
```

**Migrations:** write them the obvious way. Drops, renames and type changes need no expand/contract split; a brief window of errors during a deploy is accepted.

## Testing and shipping

```bash
uv run pytest                                    # all backend tests
uv run pytest tests/test_agents.py -v            # one file
cd frontend && npm run build                     # frontend type check + build
cd frontend && npm run gen:api                   # regenerate TS types (backend on :8000), or gen:api:local
```

- **Changed an `apps/**/schemas.py` or `api.py` — including a route docstring, which is published as the OpenAPI description — regenerate `generated.ts` and commit it.** The required `Backend tests` check fails on a stale one. Put maintainer rationale in `#` comments, not docstrings.
- **Every PR: `gh pr merge <n> --auto`, with NO strategy flag** (`--squash` leaves auto-merge unarmed — the merge queue owns the strategy). Confirm with `gh pr view <n> --json autoMergeRequest`.
- **A queue failure ejects the PR and disarms auto-merge** (your checks green, `autoMergeRequest: null`): check `gh run view <queue-run> --log-failed`, then re-arm with `gh pr merge <n> --auto`.
- **Confirm what deployed** — deploys aren't ordered by commit: `gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion`, then check the feature is live (e.g. `/api/openapi.json`). Ruleset/queue settings → `docs/architecture/deploy.md`
- **Bump the version when you change published package source.** Changing `frontend/packages/{canopy-ui,canopy-client}/src` or `sdk/python/src` without a version bump fails a required CI check, and merging the bump publishes the release. Never scope the npm names (`@canopy` belongs to someone else). → `docs/architecture/embedding-internals.md`

## Key URLs

Every page, what it is for and how it behaves: **`docs/architecture/pages.md`**. Routes: `frontend/src/router.tsx` (SPA), `config/urls.py` (server).

- **Tenant data lives under `/w/:workspace/`**; root holds personal/global pages, pre-tenant pages (invite, first run), redirects and the public site. Any route outside `/w/` carries its reason (`flat` on its descriptor, or `NOT_PAGES` for a redirect), checked by `frontend/src/routeTenancy.test.ts`.
- **One URL per artifact, nothing forwarded** — except `/w/<ws>/review/<id>`, which forwards a narrative review to its storyboard (`apps/reviews/views.py`, `docs/architecture/pages.md`). Flat `/walkthrough/`, `/review/`, `/share/`, `/storyboard/`, `/narrative/`, `/ddd-release/` are a plain 404 (`config/views.py::flat_artifact_gone`). Build links with `scoped_path` / `useScopedPath` — no flat fallback.
- **One registry of pages: `frontend/src/guide/surfaces.ts`.** A new page needs a descriptor there (the `/guide` coverage test fails without one), saying where it lives (`home`/`group`/`flat`); the guide, the workspace switcher and the tenancy test read it → `docs/architecture/pages.md`.

**Tenant-scoped (`/w/:workspace/…`):**
- (index) → `agents` — the workspace's agents (a table, `GET /api/agent-roster/`); `projects` — every project in the workspace
- `agents/new` — create an agent: pushes the factory scaffold into an empty repo the person made, with their fine-grained token (which then becomes its GitHub identity), registers it, lands on its Settings setup checklist (persona + first skill are handed to Claude Code)
- `agents/:slug` — agent workspace: Projects (landing), Project, Tasks, Turns, Schedules, Huddles, Mail, Skills (+ History), Capabilities, Settings
- `agents/:slug/projects/:ref` is a project's ONE page, for teammates and editors alike (`projects/:agent/:ref` redirects there, and a workspace project's address `projects/:handle` opens it on its steward's board, at `board_url` — the steward's workspace, which for a division's project kept from the parent is not the address's); an idea is a task with `kind: "idea"` (never inferred) carrying a person's words, its work as subtasks
- `settings/<section>` — workspace settings: members, access-requests, slack, inbound, email, connected-apps, secrets, retention, admission-rules, topology (fleet map + runners/agents tables)
- `chat`, `chat/:id` — your chat sessions; live multiplayer chat with an agent
- `huddles` (+ `/:id`), `threads/:id` — agent huddles; a bounded agent-to-agent thread
- `schedules`, `activity`, `timeline` — schedule calendar, turn log, team activity feed
- `health`, `analytics`, `network` — what is not set up right across the workspace; turns, outcomes and usage; who works with whom through which threads (`docs/architecture/analytics.md`)
- `shareouts` (+ `/:period`), `walkthroughs`, `storyboards`, `ddd` (+ `/:narrative/:runId`) — briefings, demos, storyboard index, DDD operator console
- `marketplace.json`, `plugins/…` — retired (410); the agent catalog is `/plugins/marketplace.json` (root, below)
- Public with a token (the only `/w/` pages anonymous visitors reach, `apps/common/middleware.py::_SCOPED_VIEWER`): `walkthrough/:id` (+ `/content`), `review/:id`, `share/:token`, `storyboard/:slug`, `narrative/:slug?b=`, `ddd-release/:narrative/:runId`

**Root:**
- `/` — public product site (Astro, `site/`, `config/public_site.py`) for everyone; `/app` — the door into the app (`LOGIN_REDIRECT_URL`); `/about` — public explainer; `/get-started` — public: how to use a canopy agent in your own Claude Code
- `/home` — what is waiting on you across your workspaces (`/api/me/attention/`, one list every client reads; Hide takes a thing off your own Home, closing nothing, until something new happens on it); where `/app` lands
- `/supervisor` — cross-fleet Feed, Waiting on you, Sessions, Agents, Runners (phone PWA + menubar)
- `/ide` (+ `/:id`) — desktop session IDE: project tree of every session (state, runner, mode, age) beside the open chat
- `/activity`, `/schedules` — turn log and schedules across your workspaces
- `/plugins/marketplace.json` — your agents as ONE Claude Code catalog, `canopy-agents` (github sources, your own GitHub access; `docs/architecture/plugin-marketplace.md`)
- `/settings` — your account (runners you administer, GitHub, connected apps, tokens)
- `/sessions` — your shared transcripts; `/system` — capability catalog; `/guide` — in-app guide
- `/people/me` — what agents know about you; `/hcp-clients` — HCP client registry (superusers); `/beta-requests` — requests for access to Canopy
- `/invite/:token` — accept an invite (pre-auth preview); `/new-workspace` — first run
- `/api/` REST (`/api/docs/`), `/admin/`, `/health/`

## API

Every route with its behaviour: **`docs/architecture/api-surface.md`**; machine-readable: `/api/openapi.json`.

- All routes are Django Ninja on the single `NinjaAPI` in `apps/api/api.py`; request/response models live in `apps/<app>/schemas.py`, routes in `apps/<app>/api.py`.
- The canonical tenant URL is `/api/w/{ws}/…` (`apps/api/tenancy.py::WorkspaceResolveMiddleware` gates membership, strips the prefix, reroutes to the flat mount); a flat `/api/…` list spans the caller's workspaces only within one tenant tree (else 422 `workspace-required`) → `docs/architecture/roles.md` § A read that names no workspace stays inside one organisation.
- **A page about the person, not one workspace (Home, Supervisor, `/ide`), reads a user-level view `/api/me/<list>/`**: the list run once per organisation (`wsvc.each_organisation`), never a client loop over pins → `docs/architecture/roles.md` § A user-level view.
- Bare Django views (session/cookie/redirect work, Range streaming) are matched in `config/urls.py` before the Ninja catch-all: `/api/csrf/`, `/api/debug/mint-session/`, `/auth/cli/authorize/`, `/health/`, `/runner-app/` (the runner desktop window's home, `apps/harness/views.py`), and `/w/<ws>/walkthrough/<id>/content` (`apps/walkthroughs/streaming.py`, the only Range-streaming view); OAuth/HCP/Slack/embed endpoints are bare views too (`/oauth/…`, `/api/hcp/oauth/…`, `/api/slack/…`, `/embed/…`).

## Invariants

Each is enforced or load-bearing across the codebase; the pointer says where.

**Tenancy and identity**
- **Never a NULL-means-allow tenant.** `Agent.workspace` is NOT NULL; don't re-nullable it or write `if row.workspace_id and <check>` anywhere — a row with no workspace is visible to nobody. → `docs/architecture/tenancy.md`
- **One workspace authorizer.** Only `apps/workspaces/services.py` decides membership (`member_role`/`is_member`/`membership`); a `WorkspaceMembership` read filtered by workspace AND user elsewhere fails CI (`# authz-exempt: why` for questions about a target). → `tests/test_workspace_authorizer_is_sole_gate.py`
- **Only `services._grant`, `ensure_member` and workspace creation may create a `WorkspaceMembership`** — never as a side effect of a read or a write. → `apps/workspaces/tests/test_access_requests.py`, `tests/test_no_implicit_enrolment.py`
- **A write names its workspace.** Creates resolve via `wsvc.creation_workspace` (pinned, else sole membership, else 422 `creation_refusal`) then still check `CONTENT_WRITE`; links via `links.scoped_url`, never `build_absolute_uri`. → `tests/test_nothing_outside_a_workspace.py`
- **Two principals, two types.** A contact (`ContactToken`) is anonymous to Django and reaches only `/api/contact/`; recording a contact grants nothing; no shared-secret-plus-email door — a site speaks for someone only by signed assertion. → `tests/test_contact_surface_is_bounded.py`, `tests/test_contacts.py`
- **`Agent.slug` is globally unique** (it is the Slack slash command); don't make it per-workspace. **Uploaded HTML is never `allow-same-origin`.** → `docs/architecture/tenancy.md`
- **Rolling sessions:** every request writes the session, so a view catching `IntegrityError` needs its own `transaction.atomic()` savepoint. → `docs/architecture/tenancy.md` § Rolling sessions

**Access**
- **Nothing outside `apps/workspaces/` names a role.** Gate on a capability with `perms.can(user, ws, perms.X)` (`apps/workspaces/permissions.py`); every route declares its gate at the route, `@gate(...)` directly above its `def` (vocabulary in `apps/api/route_gates.py`). → `tests/test_roles_named_only_in_workspaces.py`, `tests/test_every_route_declares_its_gate.py`, `docs/architecture/roles.md`
- **One rule decides what a person may make an agent do: `apps/agents/access.decide`.** Ask it; never write a second predicate. No interface = default deny for viewers/contacts; editors get full but always `manual`. → `tests/test_access_matrix.py`, `docs/architecture/access.md`
- **A view judging many (person, workspace/agent) pairs reads the facts once** — `perms.slugs_with` (one person), `wsvc.member_roles_of` / `access.Standings` (many) — and asks the one rule with them; never `member_role`/`perms.can`/`is_admin`/`decide` per pair in a loop. → `docs/architecture/roles.md` § A view that judges many pairs reads once, `tests/test_bulk_authz_parity.py`
- **One chat-session ACL.** Every door (REST, socket, MCP, Slack, push…) asks `apps/canopy_sessions/access.py`; opening a chat is never a grant. → `tests/test_session_acl.py`
- **A browser action that reveals or changes a secret needs a passkey step-up**: call `apps.passkeys.stepup.require_recent_step_up(request)` (403 `step_up_required`, which the SPA's API client answers with Touch ID and a retry — no per-page code). A token never steps up; decide per route whether it is refused or allowed. → `docs/architecture/api-surface.md` § Passkeys
- **MCP is the REST API.** A new route is a tool automatically; secure it in the route, never by excluding it from MCP. → `docs/architecture/mcp-surface.md`
- **Every analytics fact goes through `apps.events.activity.emit`**; analytics reads only `Activity` + `SessionUsage` (tokens as each box reports them), never tables retention purges. → `docs/architecture/analytics.md`
- **Every settings change is audited by one plumbing.** Each write route's `@gate` says whether it is a setting (`setting=True`, recorded as a `SettingChange` in the same transaction, from every entry point) or not (`kind=` a `WRITE_KINDS` kind); never write a per-route audit, report before/after with `audit.note_change`. → `tests/test_every_route_declares_its_gate.py`, `docs/architecture/settings-audit.md`

**Runners**
- **A runner serves its OWNER's workspaces, never `Runner.workspace`.** The tenant gate is `routing.runner_tenant_slugs` (owner's `agent.work` ∩ `RunnerEnrollment` coverage), shared by claim and schedules; a NULL owner fails closed → `tests/test_claim_schedule_parity.py`, `docs/architecture/runners.md`
- **A cloud runner is one isolation unit: one workspace tree, or one agent.** It enrolls in one root only (laptops may span); an agent unit runs only its agent, and that agent runs on no other runner of its `machine_id` — `routing.unit_refusal`, asked above the pin and by the vault → `tests/test_runner_units.py`, `docs/architecture/runners.md` § Isolation units
- **A box runs an agent only if its owner is one of the agent's admins** (`agents.services.runner_may_hold_agent`) — claim, credential resolve, GitHub token, pins and routing writes all ask it. **No box holds a GitHub token**: a turn gets its agent owner's delegation, per turn, with no shared fallback (409 when absent; never for a confined turn). → `docs/architecture/runners.md`
- **A runner signs in as itself, never with a person's PAT.** Its approved device key buys an hour-long `RunnerAccessToken` (`POST /api/runner-sessions/`) that opens the runner protocol only (`apps/harness/runner_auth.py`); a laptop pairs with a PAT; a cloud box pairs by invitation (`apps/harness/api_runner_invitations.py`: only the invitation's creator approves it, from a browser behind a passkey; no token can). Don't widen `runner_auth` to non-runner routes or hand the box credential to an agent turn. → `tests/test_keyless_runner_auth.py`, `docs/architecture/runners.md`
- **A WS control frame is a doorbell, never a mechanism.** Anything a runner must do (answer a menu, close a session) gets a durable row it drains on its poll tick (`RunnerBinding.pending_answer`/`close_requested`) — a frame to a dead channel vanishes while the runner still reads ONLINE. → `docs/architecture/blocked-agent-menus.md`

**Chat**
- **Turn status is derived once, server-side** (`apps/harness/turn_status.py`); every channel renders it, never invents its own. A new socket frame needs an explicit passthrough in `apps/canopy_sessions/agui.py` or it is dropped silently. → `docs/architecture/chat-sessions.md`
- **An agent chat's worktree project is `Session.emdash_project`, not `Session.project`** (blank for agent chats); send the bare `project` to a runner and it silently skips the session. → `docs/architecture/chat-sessions.md`

**Frontend**
- **Design tokens are the single source of truth: no raw Tailwind palette literals (`stone-*`, `red-*`, …) and no `dark:` variants** — use the semantic tokens (`bg-card`, `text-foreground`, `text-muted-foreground`, `success`/`warning`/`destructive`…). Token list + the one exception → `docs/architecture/frontend.md`
- APP UI: dense, readable, tables not cards.

## Where to read before touching…

- Roles/permissions, who may make an agent do what (`access.decide`, interface, caller envelope, confinement, caller/scoped tokens), getting into a workspace, admission rules, Slack fork/branch, the people brain / HCP → `docs/architecture/access.md` (enforcement detail: `roles.md`; HCP: `hcp-service.md`, `hcp-conformance.md`)
- Workspaces/tenant FKs, who-may-create-where, agent slugs/definitions, PATs/sign-in/contacts/contact tokens, chat-session access, walkthrough/review visibility or share tokens, the uploaded-HTML sandbox, retention, what a sensitive (ZDR) conversation keeps (`harness.sensitivity`) → `docs/architecture/tenancy.md`
- Vaults: an agent's vault, its first-class membership (direct members and links), and who may hold a copy of its key → `docs/architecture/vault.md`
- Runner routing (`RunnerAssignment`, source/actor rules, workspace default order, pins, the claim), per-rule turn mode, runner flags/ZDR, scheduled turns, a turn's GitHub identity (`AgentDelegation`, `RepoIdentity`, the GitHub App), runner readiness, installing/auto-updating the runner → `docs/architecture/runners.md`
- Chat sessions — turn status across channels, transcript recording (`transcript_sourced`), per-person drafts/authorship, session liveness, ACP shape, how the runner drives a chat turn, Feed pushes, viewer presence, `scripts/e2e_session_chat.py` → `docs/architecture/chat-sessions.md`
- Blocked-agent dialogs/menus (hook, transcript, screen parser, ACP permission requests, Notification markers), answering from a phone or Slack, `send-keys`, `pending-menus.json`, the composer lock → `docs/architecture/blocked-agent-menus.md`
- MCP tools/auth → `docs/architecture/mcp-surface.md`. The embedded widget, canopy-ui/canopy-client/dimagi-canopy packaging, MCP Apps Views, the host-grant live probe, `scripts/e2e_embed_widget.py` → `docs/architecture/embedding-internals.md`. Host-facing embedding guide + page contract → `docs/architecture/embedding-a-canopy-agent.md`; host grant wire contract → `sdk/python/README.md`
- Deploys, CI/merge queue, AWS/IAM (incl. canopy-web's own task role and its rollout), the old address, Postgres hosting → `docs/architecture/deploy.md`. Styling, themes, `canopy-ui`, the service worker/PWA, static caching → `docs/architecture/frontend.md`
- Cloud capabilities — an agent leasing its own ephemeral EC2 machine, or a named slot of a `pool` (`capability.yaml`, `canopy-runtime publish-capabilities`, the `Mode=ephemeral` stack contract, grants, leases and long-poll, exec, the sweep, a job on the off-request clock `apps/common/ticks.py`, + `manage.py capability_sweep`, force-release/terminate-orphans/spend, the stack's root-volume perf params) → `docs/architecture/capabilities.md`
- Who changed which setting, through what (`SettingChange`, `@gate(..., setting=True)`, `audit.note_change`) → `docs/architecture/settings-audit.md`
- Analytics facts (`Activity`, the one writer `events.activity.emit`), tokens per Claude session per day from each box's usage report (`SessionUsage`), missing-session reconcile, `backfill_activity`, the `/w/:ws/health` + `/analytics` dashboard → `docs/architecture/analytics.md`
- Workspace email domains (`apps/mail`: SES receive/send, routes, forwarding, agent mail turns, the `canopy-web-mail` stack, rip-out list) → `docs/architecture/mail.md`; setup → `docs/runbooks/mail-domain-onboarding.md`
- Pages and what each does → `docs/architecture/pages.md`. Every API route → `docs/architecture/api-surface.md`. The plugin marketplace → `docs/architecture/plugin-marketplace.md`
- Connecting a system (operator side) → `docs/runbooks/connecting-a-system.md`; Gmail push → `docs/runbooks/gmail-push-provisioning.md`; the mobile loop against real emdash → `docs/runbooks/mobile-loop-real-emdash.md`
- Glossary (the nouns and the one rule) → `docs/architecture/access.md`; deferred work → `TODOS.md`

Design specs (`docs/superpowers/specs/`, dated) are the design record — why something was built the way it was. Topic docs link the relevant ones; read them only to dig. Executed plans are archived under `docs/archive/plans/`.
