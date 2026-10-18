# Project check-ins: the project is the unit, a huddle is how it meets

**Date:** 2026-10-17 · **Status:** approved (Jonathan) · **Owner:** Ada

This doc supersedes **layer 3 (work groups)** and the parts of layers 2 and 4 that depend on it in `2026-10-16-work-group-huddles-design.md`. Layer 1, project multiplayer, stays as it is and is extended here.

## What Jonathan said (2026-10-17)

- "The group could change though, so the group shouldn't be the organizing unit. It's basically just a project, with the fact that it huddles. You could imagine this construct in projects too: a check-in or weekly-meeting type data structure."
- On what a check-in produces: "Tasks inside the project, though tasks may spawn new projects."
- On reads, from the first live test: "I'm fine with the content read, I just want it to be accurate." A run reads exactly the window its announcement names (canopy-web#1584), and never canopy's own posts (#1590).

Standing rule, unchanged: a channel is read only on a clear human command given in Slack. For a check-in with a channel, **the lead's Confirm in the channel is that command**, and nothing else starts a read.

## The model

```
AgentProject ── the organizing unit: outcome, tasks, participants, backings, timeline
  └─ ProjectCheckin ── a recurring meeting on the project: cadence, lead, who takes part,
       │                optional Slack channel, format (solo | huddle)
       └─ CheckinRun ── one occurrence: announce → (lead confirms → read) → meet → outputs
            outputs → proposed TASKS in the project (status `suggested`), each with a
                      decision (Start / Decline) and backings; a task may SPAWN a project
```

People, agents, a channel and a cadence can all change. The project, and its record of every check-in and every task they produced, stays.

## 1. `ProjectCheckin` (apps/huddles, replacing `HuddleGroup`)

| Field | Rule |
|---|---|
| project (AgentProject) | **required.** The check-in belongs to one project. Its leading agent is `project.agent`. |
| name | e.g. "Weekly check-in". Unique per project. |
| topic | optional. Defaults to the project's outcome. It is the brief the meeting works from. |
| lead (User), lead_slack_id | **required.** The person each occurrence answers to. With a channel, the lead must be a member of the project's workspace AND of the channel, checked at save and at every announcement, as before. |
| installation, channel_id, channel_name | **optional.** With a channel, saving puts it on `project.agent`'s read allow-list, reusing an entry that's already there (#1582), and deleting removes only an entry the check-in added. Without a channel, there is no announcement and no read. |
| agents | ≥1 agent slug, and it always includes `project.agent`. Every other agent must be one `project.agent` can run (an owner/admin edge, as before). Agents other than `project.agent` become `ProjectParticipant`s (source `checkin`). |
| format | derived. One agent → **solo**: the project's agent works the project. More → **huddle**, led by the project's agent. |
| schedule (AgentSchedule 1:1) | on `project.agent`, with the check-in's cron and timezone. The prompt is the generic `/canopy:huddle`. |
| enabled, added_allowance, created_by, created_at, updated_at | |

**Who may write a check-in:** the project's `owner_user`, or a workspace owner or admin. Putting a channel on an agent's read list still needs a person: an agent's own login can't add a channel to its own list, but it may reuse an entry already there.

## 2. `CheckinRun` (replacing `HuddleGroupRun`)

`HuddleGroupRun`'s fields, plus:
- `checkin` (FK) and `project` (FK, denormalised so the project timeline doesn't need a join through a check-in that may since have been deleted). Deleting a check-in keeps its runs: `checkin` is SET_NULL.
- `participants`: a JSON snapshot of who took part, `{agents: [...], lead_email}`, so history holds when membership changes.
- `tasks_proposed`: a count, set at finish.

The lifecycle is unchanged (`awaiting_confirm → reading → running → done`, or `skipped / expired / failed`), with one addition:
- **A check-in without a channel** skips straight from announce to `running`. The schedule enqueues the turn directly; there is no Slack post and no Confirm, because nothing is read.

Read rules carry over from #1584 and #1590:
- `read_since` is fixed at announce: where this check-in's last read ended, or one cadence back for its first run.
- The read is cut to exactly that window.
- canopy's own posts are excluded.

## 3. Outputs: proposed tasks in the project

**`POST /api/agents/{slug}/projects/{ref}/tasks/propose/`** (also an MCP tool) is the task-level twin of `projects/propose/`.

- **Body:** `{title, why, done_when, plan: [step…], partners: [agent…], source_url, run?, new_evidence?}`.
- **What it creates:** one task, `status=suggested`, in that project. The `plan` steps become its child tasks (`parent`). `origin_ref` is `{kind: "checkin_proposal", checkin, run, huddle_id}`.
- **Revising and declined-before:** the same rules as project proposals. A same-title open proposal is revised, and its backings go `stale`. A title declined before needs `new_evidence`, or the call is refused with 409.
- **Partners:** each partner gets one linked `suggested` task on its own board, as with project proposals.
- **Returns** `{created, task}`: 201 when created, 200 when revised.

**Decision:** the existing task actions, with no new verbs.
- **Start** (approve): `suggested → in_progress`, and the project's agent gets a turn to do it.
- **Decline:** the task becomes `declined`, with the reason kept.
- **Who may decide:** the project's `owner_user`, the check-in's lead, or anyone with `projects.decide`.

**Backing:** `ProjectBacking` gains an optional `task` FK and becomes the one backing table.
- A row is on a project OR a task, never both.
- There is one row per (task, person) and one per (task, agent).
- **Routes:** `GET|PUT|DELETE …/projects/{ref}/tasks/{task}/backings/…`, mirroring the project routes.
- `huddle file` records agents' co-signs this way.

**A task spawns a project.** `AgentProject.spawned_from` is a new nullable FK to `AgentTask`.
- `projects/propose/` accepts `from_task: "<agent>/<task ext_id>"` and sets it.
- The task shows "spawned → <project>", and the project shows "from <task> in <project>".
- That's how a check-in's proposal grows into its own project. **Huddle survivors are tasks by default.**

**Expiry:** a proposed task nobody starts or declines within 14 days is declined as "not taken up", the same as proposed projects.

## 4. UI — the project page is where the work lives over time

The project page gains:
1. **Check-ins** panel (settings): each check-in's name, cadence, lead, channel and agents, with Edit, Delete and **Run now**. Run now announces; it never reads.
2. **Proposed** section: the project's `suggested` tasks that came from check-ins, rendered with the shared decision card. The card is generalised from `ProposedProjectCard` to a `ProposalCard` taking a project or a task, so people can Start/Decline, back and comment right there.
3. **Timeline:** every `CheckinRun`, newest first, showing:
   - the date and the check-in name;
   - who took part (the snapshot);
   - the read window ("N messages since …", or "no channel");
   - who confirmed it;
   - a link to the huddle;
   - what it proposed, as task links with their live status;
   - any projects those tasks spawned.

**Other surfaces:**
- **Huddle page:** it renders the run's proposed tasks with the same `ProposalCard`, plus a header that links back to the project and the check-in.
- **Huddles page:** it lists check-ins across projects as a navigation view only. The record lives on the project.
- **The work-group list and group page are removed.** The URL `/w/:ws/huddles/groups/:slug` redirects to the project's page.

Engagement: `EngagementEvent.group` is renamed to `checkin`, and `task` is recorded on task proposals. `GET /api/engagement/summary` takes `checkin=` in place of `group=`, and reports proposals at both levels, tasks and projects.

## 5. API

- `GET/POST /api/agents/{slug}/projects/{ref}/checkins/`
- `GET/PATCH/DELETE /api/agents/{slug}/projects/{ref}/checkins/{name}`
- `POST …/checkins/{name}/run-now`
- `GET /api/checkins/` lists the check-ins in your workspaces.
- `GET /api/checkins/runs/{id}` (with its material) and `POST /api/checkins/runs/{id}/finished {huddle_id?, result_url, summary, tasks_proposed}`
- `GET /api/agents/{slug}/projects/{ref}/checkin-runs` is the project timeline.
- **Kept for one release:** `/api/huddle-groups/runs/{id}` and `…/finished`, as aliases onto check-in runs, so an engine that hasn't updated yet still finishes. The other `/api/huddle-groups/*` routes are removed.

## 6. Engine (canopy) and bindings (ada)

- **`/canopy:huddle --checkin-run <id>`** (`--group-run` accepted as an alias). Solo vs huddle is decided by the check-in's agents.
- **`canopy huddle plan --checkin-run <id>`:**
  - the brief is the check-in's topic plus the project's outcome;
  - the channel material is quoted as before;
  - the context pack carries the project's open and recently decided proposed tasks, with their backings and comments.
- **`canopy huddle file`** with a check-in run:
  - each survivor → `projects/{ref}/tasks/propose/` on the check-in's project;
  - each co-sign → a task backing;
  - then `checkins/runs/{id}/finished` with `tasks_proposed`.
  - A survivor the huddle marks `"new_project": true` is proposed as a task anyway; it says in its `why` that it should become its own project, and the decision to spawn one is a person's.
- **`canopy checkin material --run <id>` / `finish`** replace the `huddle-group` verbs. The old verbs stay as aliases for one release.
- **Solo format:** the agent reads the material and files proposed tasks in the project through the same route.
- **Ada:** `bin/ada-huddle` and the skills use `--checkin-run`. The weekly multiplayer read uses `checkin=`.

## 7. Migration

- `HuddleGroup` → `ProjectCheckin`:
  - A single-mode group already has a project, and becomes its check-in.
  - A multi-mode group gets a new active project on its leader agent: the name is the group's name, the outcome is the topic, the owner is the lead, and the lead is a `manual` participant. It then becomes that project's check-in.
- `HuddleGroupRun` → `CheckinRun`, keeping its ids. Projects proposed by a huddle on an earlier run are linked from the new project's timeline through the run's `huddle_id`.
- **Live today:** the "Canopy support" group becomes the "Canopy support" project on `ada`, with a weekly check-in: lead Jonathan, #canopy-support, agents ada and hal, Tuesdays 09:00 America/Denver. Runs 1 and 2 move onto its timeline. Hal's P7 and P8, proposed by run 1, each get a task in the project with `spawned_from` linking to them.

## Order of work

1. **canopy-web:** models, migration, routes, task proposals and backings, `spawned_from`, the project-page UI, and the redirects (one PR, or two if the frontend is large).
2. **canopy:** the engine's `--checkin-run`, `file` → task proposals, and the `checkin` verbs.
3. **ada:** the bindings and skills.
4. **Data:** link P7 and P8 to the Canopy support project.
