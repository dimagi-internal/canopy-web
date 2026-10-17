# Workspace health and analytics — design

Date: 2026-10-17. Asked for by Jonathan (brainstorm in session, then "build it").

## Intent

Two things a workspace owner cannot see today, for a workspace **and every
workspace below it**:

1. **Health** — what is not configured correctly. Two drivers: the new
   canopy-web vault (an alternative to 1Password) means every agent is somewhere
   on a migration, and more people are creating agents, so their setup and
   properties need checking without opening each agent one by one. Every problem
   carries a way to fix it: a link to the control that fixes it when the viewer
   may, otherwise who owns it plus a link to send them.
2. **Analytics** — agents, sessions, messages, tokens/cost over time; active
   users; per-person behaviour.

And underneath, the thing analytics depends on: **one unified way the system
records what happened**, so analytics is a read over one table instead of a
join across fifteen.

## Goals

- ONE server-side answer to "is this agent set up right" — the existing
  `apps/agents/setup.py::setup_steps` — read by the agent's Settings checklist
  AND the dashboard. It becomes vault-source aware (a canopy-web-vault agent no
  longer shows "Give it a vault" as a 1Password to-do).
- ONE server-side vault verdict per agent (`apps/agents/vault_health.py`):
  which store it resolves from, what is declared, what is missing, which refs
  still point at `op://`. Read by the checklist's vault step and the dashboard.
- ONE append-only analytics fact table (`events.Activity`) written through ONE
  helper (`apps.events.activity.emit`), indexed for (workspace, time),
  (workspace, kind, time), (user, time). Analytics reads only this table and
  `TurnUsage`.
- Token and cost capture per turn (`events.TurnUsage`), derived server-side from
  the `result` record of the turn's uploaded `claude -p` transcript — no runner
  change, so no box redeploy. Survives retention (no FK cascade from the
  transcript; keeps workspace/agent/user itself).
- A backfill command that seeds `Activity` and `TurnUsage` from the history
  that still exists (turns, chat sessions, messages, setting changes, MCP calls,
  transcripts), so the dashboard is not empty on day one.
- One tenant page `/w/:ws/health` with two tabs, Health and Analytics.

## Non-goals (this round)

- Rewriting the other fourteen event/audit tables. Audit-grade tables
  (`SettingChange`, `MCPAuditLog`, …) stay authoritative for audit; they ALSO
  emit an `Activity` fact through the one helper. Folding the rest in is follow-up.
- Runner-side usage reporting. Turns whose transcript is never uploaded (some
  interactive chat turns) report "no usage reported", never zero.
- Cost alerts, budgets, verdicts. Per memory "objective data UIs, no verdicts":
  show facts and states, never "stuck"/urgency rankings.

## Anti-goals

- A dashboard with its own idea of "ready" that disagrees with the checklist.
- A second walk of the workspace tree; reuse `wsvc.subtree` + one
  `agents_in_tree` helper (shared with both topology builders).
- Counting from tables retention purges (messages, transcripts) at read time.

## Access

- Both endpoints gate `logs.read` on the root and filter descendants by the same
  capability — the topology pattern (`INHERITS_DOWN` carries owners/admins down).
- `can_fix` per agent: the viewer is the agent's owner, one of its admins, or a
  workspace owner — the same predicate the agent Settings page uses for its
  writes. Otherwise the row shows the owner's email and a copyable link.

## Shape

- `GET /api/workspaces/{slug}/health` → tree of workspaces (depth, shared-vault
  source) and agent rows (owner, workspace, repo, credential source, vault
  verdict, steps with status/href/action/command, last turn, `can_fix`), plus
  totals.
- `GET /api/workspaces/{slug}/analytics?days=N&tree=bool` → daily series
  (turns by status, sessions, messages, active users, tokens in/out/cache,
  cost), totals, per-agent and per-user tables.

## Calls the builder made (no human input; check these)

- Facts live in the existing framework app `events` (it already owns "something
  happened"), not a new app.
- Active users = distinct people with any Activity that day (sent a message,
  launched a turn, changed a setting, called MCP, opened a chat). No page-view
  tracking.
- Charts are hand-written SVG (no new dependency), matching `SkillHistoryChart`.
- One nav entry, "Health"; Analytics is its second tab (the nav is deliberately short).
- Rollups are computed at read time from the indexed fact table; a rollup table
  is added only if it is slow.
