# Wave 2 + Wave 3 Harvest — Execution Spec

**Status:** Active build · **Date:** 2026-06-29 · **Owner:** Claude (driving)
**Parent:** `2026-06-24-canopy-framework-harvest-design.md`
**Branch:** `wave2-3/harvest` (canopy-web) + companion work in the canopy plugin.

> Lean execution spec — built in verified increments, each TDD + green + committed.
> Not for sign-off; it's the build's own tracking contract.

## Corrected starting state (what Wave 1 already delivered)

Wave 1 (`apps/agent_runs` + `packages/canopy_agent_runs`) built more than its commits implied:
- **Tables exist:** `AgentRunVerdict` (judge/qa, score, passed, per-dim `criteria`),
  `AgentRunDecision` (P4 ✓), `AgentRunArtifact` (role-attributed, partial P2),
  `AgentRunGate`.
- **Read model** lives in the Django-free `canopy_agent_runs` package; `RunStore` Protocol
  has `record_gate` / `record_decision` / `fork` — **but NO `record_verdict`**, and the
  `Run` read model has a `verdicts` list with **no run-level aggregate**.

So P1 (run_state) and P4 (decisions) are done. The remaining harvest:

## Scope (in priority order)

**Wave 2 — plugin crown jewels (remaining):**
- **P3a (web, THIS spec's first increments):** verdict recording + QA-gates-judge
  enforcement + run-level eval aggregation on `agent_runs`/`canopy_agent_runs`. The plumbing a
  self-grading agent writes to.
- **P3b (plugin):** the eval *runner* — a shared `canopy eval` lib/skill that grades an
  artifact against a rubric and POSTs a verdict (the generic port of ACE's verdict-schema +
  calibration discipline). Consumes P3a.
- **P2 (plugin):** artifact-manifest registry primitive. (Partially covered by
  `AgentRunArtifact.role`; remaining = the producer/consumer dependency graph.)
- **P7 (plugin):** generic gdrive MCP — relocate ACE's `google-drive-server.ts`. *Defer
  unless needed* (it already works in ACE).
- **P8 (plugin):** video-engine single-package consolidation. *Defer* (lowest urgency;
  both copies work).

**Wave 3 — web substrate (untouched):**
- **W1 (web, high value):** multi-tenant `workspaces` app + RBAC + invites + auto-join,
  and scope `Agent`/runs to a workspace. The real blocker for onboarding more users.
- **W3 (web):** `ingest` app — transcript upload + cost/structure rollup. Net-new.
- **W7 (web):** `service_accounts` credential vault + impersonation. Net-new.
- **W4 (web):** websocket chat sessions + presence (converge SSE). Larger; sequence after W1.
- **W5/W8/W9:** AI-backend, workbench-shell, auth convergence. *Defer* — canopy-web already
  has working versions; converge opportunistically.

## Transcript-confirmed status (from the wave0 session + ace-web)

The wave0/keystone agent did **Wave 0, Wave 1, and Wave 4** — Wave 4 is *further than
first reported*: ace-web PR #660 (`wave4/run-reader-swap`, merged) swapped `apps/opps`
onto the shared `canopy_agent_runs` adapter; ace-web now depends on `canopy-agent-runs`. The team
chose **Option 2** (ace-web imports framework code, stays standalone), which the agent
noted *removes Wave-3 multi-tenancy from ACE's critical path*. Per the user, multi-tenancy
is still wanted — for the **non-ACE** agents (Echo + the new ones) — and then **multiplayer
mode** (real-time collab) on top. So Wave 3 is back on, justified by the agent fleet, not ACE.

## Increment order (each = TDD, green, commit)

1. **P3a-1** ✅ — `record_verdict` on `RunStore` (Protocol + InMemory + DB) + read-model
   aggregate (`Run.overall_score`, `Run.qa_gate_ok`). Migration-free. (commit aeef191)
2. **P3a-2** ✅ — QA-gates-judge: judge score on a QA-failed step excluded from
   `overall_score` (centralized in the read model) + `POST .../steps/{key}/verdict`.
   181 tests green. (commit da72a2a)
3. **P3b** — plugin `canopy eval` runner lib/skill (grades an artifact vs a rubric, POSTs a
   verdict). The *scorer* the wave1 agent left out. ← **next**
4. **W1-1** — `workspaces` app: `Workspace`/`Membership`/`Invite` + RBAC + REST (harvest
   ace-web's `apps/workspaces`).
5. **W1-2** — scope `agents` + `agent_runs` to a workspace (nullable FK, default workspace,
   non-member 404).
6. **W4 (multiplayer)** — websocket sessions + presence (harvest ace-web's `apps/sessions`
   consumer + Channels), scoped to a workspace.
7. **W3** — `ingest` app (transcript + cost rollup); **W7** — `service_accounts` vault.
8. Deferred tail (P2 manifest graph, P7 gdrive MCP, P8 video consolidation) as pulled.

## Invariant

All new framework apps stay framework-tier: may FK `agents.Agent` / `workspaces.Workspace`;
must not import product apps (`skills`, `collections`, `workspace`, `reviews`, `runs`).
Enforced by the Wave 0 boundary check.
