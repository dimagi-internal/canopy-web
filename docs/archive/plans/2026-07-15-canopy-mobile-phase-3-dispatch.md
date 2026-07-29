# canopy-mobile Phase 3 — dispatch + session input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the dogfooding loop — from the phone, launch a named command against an agent *or a repo*, and type into a session that is already running.

**Architecture:** Three changes, none of which touch `canopy_runner`'s CDP layer. (1) `AgentSkill` gains `launchable` + `args_hint` so an agent declares its own phone entry points — the catalog stays one wholesale-PUT source of truth. (2) `Turn.agent` becomes nullable and `Turn.project` appears beside it under a CheckConstraint, because the session you want to revise from the phone is working on **canopy-web**, a repo, and `cdp_control.create_task(project, ...)` is already project-generic. (3) Session input needs no new mechanism at all: a stable `thread_key` of `phone:{user}:{target}` resolves to `open_and_send` on the existing reuse path.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2 + Postgres; React 19 + Vite 7 + Tailwind 4 + canopy-ui; pytest; vitest; Playwright.

**Spec:** `docs/superpowers/specs/2026-07-14-canopy-mobile-design.md` — §1 (command catalog), §2 (session input), §3 (repo targets), §8 (authorization), the Phases table (Phase 3), and Testing (the Phase 3 bullet).

**Prior phases:** 0+1 shipped (PR #212), 2 shipped (push live, PWA installed). `/supervisor` is live at `https://labs.connect.dimagi.com/canopy/supervisor`, pushes on `waiting_count` increase, and lists one real runner (`jj-mbp-cdp`). Since then: #227 fixed `claim_next_turn` to derive tenancy from `runner.paired_by` (NOT `Runner.workspace`), #228 put Items in the inbox, #229 extracted `lib/needsYouBands.ts`.

## Global Constraints

- **`claim_next_turn` derives tenancy from `runner.paired_by`, not `Runner.workspace`.** This is load-bearing and was a live outage (#227, dc58b1b): the fleet deliberately spans workspaces behind one laptop runner, so scoping by the runner's single FK stranded 4 of 5 agents' turns as QUEUED forever. **Do not "fix" this back.** Project turns must follow the same rule.
- **`capabilities` is NOT a security boundary** — caller-supplied at pairing, never validated. It routes; the workspace gates. The two INTERSECT; one never substitutes for the other. Adding `capabilities["projects"]` does not change this.
- **`one_executing_turn_per_agent` stays agent-only.** A repo is not an agent: emdash gives every task its own worktree, so repo work is meant to parallelize. Widening this constraint to projects would funnel all canopy-web work into one lane. The condition already keys on `agent`, and a NULL `agent` does not participate in a UniqueConstraint — verify that rather than assume it.
- **The `TASK_NOT_FOUND` rule is inherited, not weakened.** `execute.py:66` lets *only* `TASK_NOT_FOUND` fall through from reuse to create. Any other send failure fails the turn. This rule exists because it once spawned two Hal sessions; phone input makes the path hotter, not laxer.
- **`Workspace`'s primary key is its slug** — `agent.workspace_id` is a string.
- **`SESSION_SAVE_EVERY_REQUEST = True`** — any view with `except IntegrityError` needs its own `transaction.atomic()` savepoint, or the session write on the way out hits a poisoned transaction. Precedent: `apps/projects/api.py::create_project`, `apps/harness/services.py`.
- **Design tokens only** — no raw Tailwind palette literals. Use `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, and `success`/`warning`/`info`/`destructive`.
- **Never hand-edit `frontend/src/api/generated.ts`** — run `npm run gen:api`.
- **Tests:** pytest, `pytestmark = pytest.mark.django_db`, fixtures inline per file. There is **no `tests/conftest.py`**.
- **Verify like CI:** run `uv run pytest` with `.env` moved aside. A gitignored `.env` has made the suite green while CI would have failed (3 tests, once).
- **CI does not run Playwright** (decided 2026-07-15, standing). Run `npx playwright test` locally before any merge touching the frontend.

---

## Tasks

### Task 1 — `AgentSkill.launchable` + `args_hint`

The catalog mirrors the repo, and that is the problem: Echo publishes 20 skills but ~5 are human entry points. `agent-turn-review` is a pre-send discipline, `setup` is one-time per-machine — rendering all 20 as buttons puts `/echo:setup` one thumb away.

- [ ] Add `launchable = BooleanField(default=False)` + `args_hint = CharField(max_length=120, blank=True, default="")` to `AgentSkill` (`apps/agents/models.py:152`)
- [ ] Migration
- [ ] Add both to `AgentSkillIn` / `AgentSkillOut` (`apps/agents/schemas.py:130`); `launchable` defaults False so an agent that has not adopted the field publishes nothing launchable — **fail closed**
- [ ] Test: PUT a catalog with mixed `launchable`; GET returns the flags; wholesale-PUT replacement still holds
- [ ] Test: a skill published without `launchable` is False, not True
- [ ] `npm run gen:api`
- [ ] Commit — **including any caller fixes**; the Task-1 commit in the Phase 0+1 plan did not compile because the plan's `git add` list omitted them

### Task 2 — `Turn` repo targets: nullable `agent`, `project`, `workspace`

- [ ] `Turn.agent` → `null=True, blank=True`
- [ ] `Turn.project = CharField(max_length=100, blank=True, default="")`
- [ ] `Turn.workspace` FK (null, PROTECT) — used **only** when `agent` is null. §8's derive-don't-denormalize rule cannot apply: a project turn has no agent to derive from. This is the one accepted exception; comment it as such.
- [ ] `CheckConstraint`: exactly one of `agent` / `project` set — `Q(agent__isnull=False, project="") | Q(agent__isnull=True) & ~Q(project="")`
- [ ] Migration
- [ ] Test: a Turn with both `agent` and `project` is rejected
- [ ] Test: a Turn with neither is rejected
- [ ] **Test: two concurrent project turns for the same project BOTH execute** — proves `one_executing_turn_per_agent` did not leak onto projects
- [ ] Test: `str(turn)` does not crash on a project turn (it reads `self.agent.slug`)
- [ ] Commit

### Task 3 — `SessionLink` repo targets

- [ ] Same nullable-`agent` + `project` treatment
- [ ] Unique key `(agent, thread_key)` → must now cover `(project, thread_key)`. **Two partial UniqueConstraints, not one** — NULLs do not compare equal in Postgres, so a single constraint over a nullable column silently permits duplicates.
- [ ] Migration
- [ ] Test: two SessionLinks with the same `(project, thread_key)` are rejected — the test that would catch the NULL trap
- [ ] Commit

### Task 4 — capabilities + claim widening

- [ ] `Runner.project_names()` beside `agent_slugs()`, reading `capabilities["projects"]`
- [ ] `claim_next_turn`: `Q(agent__slug__in=slugs) | Q(project__in=projects)`
- [ ] **The `if not slugs: return None` early-return becomes `if not slugs and not projects`** — otherwise a projects-only runner silently claims nothing
- [ ] Tenant boundary for project turns: derive from `paired_by`'s workspaces against `Turn.workspace` — the same rule as agents, per the Global Constraints
- [ ] `busy_agents` exclusion must not exclude project turns (`agent_id` is NULL for them)
- [ ] Test: a projects-only runner claims a project turn
- [ ] **Test (negative, tenanted attacker): a runner paired by a non-member cannot claim another workspace's project turn.** Must use a tenanted attacker — after the §8 backfill every real runner has a workspace and the untenanted path no longer exists in prod
- [ ] Test: per-agent `exclude_slugs` pause does not accidentally exclude project turns
- [ ] Commit

### Task 5 — enqueue + API

- [ ] `enqueue_turn` accepts `project` + `workspace`, rejects both/neither
- [ ] `POST /api/harness/turns/` accepts `project`; membership-gate it (404, not 403 — match `_get_agent_or_404`, do not leak existence)
- [ ] Test: enqueue for a project outside my workspaces → 404
- [ ] Test: idempotency still collapses duplicate project turns
- [ ] `npm run gen:api`
- [ ] Commit

### Task 6 — runner: `target = agent_slug or project`

- [ ] `execute.py:113` — `target = turn["agent_slug"] or turn["project"]`; the CDP call underneath is unchanged
- [ ] Test with a project turn payload
- [ ] **Do not touch the `TASK_NOT_FOUND` fall-through** (`execute.py:66`)
- [ ] Commit

### Task 7 — the composer

- [ ] `/supervisor` composer: pick target (agent or repo) → pick a `launchable` command or free text → send
- [ ] `args_hint` renders as the input placeholder
- [ ] Dispatch POSTs `prompt = "/{slug}:{skill} {args}"`, `origin=manual`, `idempotency_key = "cmd-{user}-{target}-{skill}-{ts}"`
- [ ] Test: a non-`launchable` skill is absent from the composer
- [ ] Playwright at Pixel 7 width
- [ ] Commit

### Task 8 — session input via the reuse path

- [ ] `thread_key = "phone:{user}:{target}"` — stable, so message 1 creates and every message after reuses
- [ ] Test: a second phone message on an existing thread **reuses** (`open_and_send`) rather than creating
- [ ] **Test: a non-`TASK_NOT_FOUND` send failure fails the turn and creates NO second session**
- [ ] Surface the existing `REUSE FELL BACK to CREATE` warning (`execute.py:104`) in the UI — phone dispatch makes this path hotter and each fall-through is a new Claude session, i.e. tokens
- [ ] Commit

---

## Shipping split (decided during execution)

Tasks 1-5 are the whole backend — model, constraints, tenancy, claim routing,
enqueue — and ship as their own PR. They are safe alone: a project turn is only
claimable by a runner declaring `projects` in `capabilities`, and the live runner
(`jj-mbp-cdp`) declares only agents, so `Q(project__in=[])` matches nothing.
Verified against prod, not assumed.

Tasks 6-8 (runner target, composer, session input) are the client half and follow.
The spec's "repo targets and session input ship together" is about user-facing
capability; the backend landing first changes no existing behaviour.

## Verification

- [ ] `uv run pytest` with `.env` moved aside
- [ ] `npm run build`, `npm run test`, `npx playwright test`
- [ ] PR, CI green, merge, deploy, **verify the live ECS image tag == the merge SHA** (not that the workflow says success)

## Blockers found by probing prod (must clear before Task 6 ships)

1. **A probe turn is stuck QUEUED in prod** (`project=canopy-web`, `idempotency_key=probe-p3-4`,
   id `1dfc5754-…`). Enqueued while verifying #232 on Postgres. It is inert *only* because no
   runner declares `canopy-web` — the moment Task 6 adds `"projects": ["canopy-web"]` to the
   runner's capabilities it gets claimed and types "probe" into a real emdash session. **Delete it
   before the runner learns projects.** The token user is not staff, so admin delete 403s; needs
   staff access or a management command.

2. **No way to cancel a QUEUED turn.** `finish` 409s on a queued turn (only claimed/running are
   finishable). The phone composer makes this urgent — a misfired dispatch must be cancellable.
   Add `POST /turns/{id}/cancel` (queued → a terminal state) before or with the composer (Task 7).

3. **`SessionLink` project rows have no tenant.** `resolve_session` returns a thread's rolling
   `summary` + live task id; for agent links `_agent_or_404` gates it, but a project `SessionLink`
   has no workspace FK, so any runner paired by any user could read another user's session context
   by guessing `thread_key`. The spec (§3) never addressed project-link tenancy. Options: add a
   `workspace` FK to `SessionLink` (mirrors Turn), or gate the resolve/record endpoints on the
   runner's own tenant. Decide before the runner drives project sessions (Task 6/8).

## Status (shipped)

- **Task 1** — `launchable` + `args_hint` — #232 (live)
- **Task 2** — `Turn` agent XOR repo — #232 (live)
- **Task 3** — `SessionLink` repo targets — #232, tenancy added in #236 (live)
- **Task 4** — claim widening + cross-tenant fix — #232 (live)
- **Task 5** — enqueue + API — #232; multi-workspace 422 fix in #233 (live)
- **Task 6** — runner `target = agent_slug or project` + SessionLink tenancy — #236 (live)
- **Task 7** — the composer (AGENT dispatch) — #237 (live)
- Cancel endpoint (blocker 2) — #233 (live); UI control ships with repo dispatch
- Probe turn (blocker 1) — cancelled via the new endpoint

### Remaining

- **Task 8 — session input.** The backend reuse path is done and tested (a stable
  thread_key resolves to `open_and_send`). What's left is the composer setting
  `origin_ref.thread_key = phone:{user}:{target}` so a second message REUSES the
  first's session rather than launching a fresh turn — a distinct mode from the
  launch-a-command composer that shipped, and it wants its own UX (which session,
  and a visible "continue vs new" affordance).
- **Repo (project) dispatch.** Two coupled unknowns: (a) a repo like canopy-web is
  not owned by one workspace, so the composer must decide which tenant a repo turn
  belongs to; (b) the flat enqueue 422s a multi-workspace user, so it must route to
  `/api/w/{ws}/…` — but `/supervisor` is not under `/w/:ws/`, so the URL-driven
  rewrite in client.v2 does not fire. Needs an explicit-workspace path, not a
  browser-URL one. `cancelTurn` is already in the client for the take-it-back UX.
