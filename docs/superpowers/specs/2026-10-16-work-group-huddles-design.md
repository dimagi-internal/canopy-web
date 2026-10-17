# Work-group huddles on a multi-player project foundation

**Date:** 2026-10-16 · **Status:** approved (Jonathan) · **Owner:** Ada

## What Jonathan asked for

From the AI Learning Champions thread and the follow-up on 2026-10-16:

- Canopy agents listen in the AI work-group Slack channels and hold huddles about the group's work.
- A huddle has **assigned agents**, one or more.
- **A huddle says it is running, and a person confirms it, through canopy's scheduler.**
- **"The huddle should have a lead that has channel access as a requirement."**
- **"The output should all be part of the huddle UI, and we should learn what, if anything, is working for people to interact well in multi-player mode."**
- **"If the huddle only has one agent, that just becomes a single agent on a schedule, with the project about that Slack topic."**
- **"Projects are also trying to get to good multiplayer mode, so we should be DRY where possible and have a strong foundation we can improve on."**

Standing rule, unchanged: a channel is read only on a clear human command given in Slack. **Confirming a work-group run is that command.** The lead presses Confirm in the channel. Nothing else starts a read: not the schedule, not an agent, not the web UI.

This doc supersedes the decisions section of `2026-10-16-huddle-outcomes-multiplayer-design.md`. The recommended answers there are taken as approved:

- 1 yes, 2 yes, 3 yes, 4 (b), 6 yes, 7 yes, 8 yes.
- 5: anyone in the lead's workspace can back and comment. Nobody is invited automatically.

One change from that doc: a person acts on outputs **on the huddle page**, as well as on the project page. Both pages use the same components.

## Shape: three layers, each built on the one below

```
3. Work groups      HuddleGroup (lead + channel + agents + schedule) → HuddleGroupRun (confirm → read → dispatch)
2. Huddle UI        the huddle page renders its outputs as proposed projects, using layer-1 components
1. Project multi-   proposed status · decision card · ProjectBacking · reply = comment ·
   player (shared)  participants · EngagementEvent (what people actually do) — used by ANY project
```

Layer 1 is not huddle code. Any project an agent proposes uses it: from a huddle, a chat, or an agent's own turn. Making projects better at multiplayer makes huddles better at it too.

## Layer 1 — project multi-player (apps/agents)

1. **`AgentProject.status` gains `proposed`.**
   - Proposed projects get their own "Proposed" section on the board and the Projects page.
   - `AgentProject.source_url` is a new indexed field, the same idea as `AgentTask.source_url`. It holds the page that proposed the project, e.g. a huddle.
2. **`POST /api/agents/{slug}/projects/propose/`** (also an MCP tool) creates or revises a proposed project in one call.
   - **Body:** `{name, outcome, why, plan: [step…], partners: [agent slug…], source_url, owner_email?}`.
   - **What it creates:**
     - the project, `status=proposed`;
     - its plan steps, as the project's tasks, `status=suggested`;
     - ONE decision card: a `review` task titled "Start this project?" whose body is the why and the done-when.
   - **Revising.** If the same agent already has an open proposed project with the same name, the call updates it (outcome, plan, why) and marks the existing backings `stale`. It never files a second card.
   - **Declined before.** A project of that name declined before comes back only when `new_evidence` is given, and is otherwise refused with 409. This is the existing huddle rule, moved here.
   - **Partners.** Each partner gets one linked `suggested` task on its own board, with `source_url` set to the project page. Projects stay per agent (Jonathan's 2026-09-19 rule).
3. **Decision card actions:** these are the existing task actions, with no new verbs.
   - **Approve:** the project becomes `active`, and the lead agent gets a turn to start it. Each partner's linked task moves to `accepted`.
   - **Decline:** the project becomes `archived`, and the reason is kept in `notes`.
   - **Who may approve or decline:** a workspace owner or admin, or the project's `owner_user`. This is decision 4(b).
   - **Reply is the comment thread.** Any workspace member may reply, and an editor's reply wakes the lead, as today.
4. **`ProjectBacking`:** `(project, person | agent, stance, line, source, huddle_id, stale, created_at, updated_at)`.
   - `stance` ∈ `backs` / `backs_with_changes` / `passes`.
   - `line` is at most 280 characters, and is required unless the stance is `backs`.
   - Each row has a person OR an agent, never both. There is one row per (project, person) and one per (project, agent).
   - **Routes** (each also an MCP tool):
     - `GET …/projects/{ref}/backings/`
     - `PUT …/projects/{ref}/backings/me/`: the person calling.
     - `DELETE …/projects/{ref}/backings/me/`
     - `PUT …/projects/{ref}/backings/agents/{agent}/`: called by that agent's token, or by the project's own agent's token; `huddle file` uses it for co-signs.
5. **Participants.** Anyone who backs or replies becomes a `ProjectParticipant`, with new sources `backing` and `reply`, so they show in the team view.
6. **Expiry.** A proposed project that nobody starts or declines within 14 days is archived as "not taken up" by the existing periodic loop. Its decision card is declined and the reason recorded.
7. **`EngagementEvent`: learning what works.** One append-only table for every project and huddle:
   - **Columns:** `(workspace, kind, surface, user, agent, project, task, huddle_id, group, at, meta)`.
   - **`kind`:** `viewed`, `backed`, `commented`, `approved`, `declined`, `idea_added`, `run_announced`, `run_confirmed`, `run_skipped`, `run_expired`, `proposed`, `expired`.
   - **`surface`:** `web`, `slack`, `email`, `mcp`.
   - **What records it:** the backing routes, the task actions on a project's tasks, idea creation, the run lifecycle (layer 3), and a view beacon.
     - The beacon is `POST /api/engagement/view {project | huddle_id}`, de-duplicated per user, per object, per UTC day.
     - The project page and the huddle page call the beacon.
   - **Report:** `GET /api/engagement/summary?days=14[&group=<slug>][&agent=<slug>]` returns:
     - counts by kind and surface;
     - the unique people who did anything;
     - for each proposed project: time to first human reaction, time to decision, number of backers, and number of comments;
     - proposed projects that got no human reaction at all;
     - for runs: announced vs confirmed vs skipped vs expired, and the median time to confirm.
   - Ada reads this report weekly and tells Jonathan in plain words what people actually do.
8. **Frontend components,** shared by the project page and the huddle page:
   - `ProposedProjectCard`: name, outcome, why, plan, partners, decision buttons, the backing list, and the reply box;
   - `BackingList`;
   - `BackingControl`;
   - `useEngagementView`.

## Layer 2 — the huddle UI (apps/huddles, frontend/src/pages/huddles)

- **Outputs.** `HuddleOut.projects` lists the proposed projects whose `source_url` is the huddle page. For each it gives the agent, ref, name, status, outcome, url, the decision card's task id, its backings, and its partners' linked tasks.
  - The page renders each one with `ProposedProjectCard`, so people back, comment and decide **on the huddle page**.
  - Huddles filed before this change keep showing their `outputs` (loose tasks) as they do today.
- **Group header.** `HuddleOut.group` is `{slug, name, channel_name, lead_email, run_id, read: {messages, first_ts, last_ts, since}}` when the anchor's `origin_ref` carries a group run, and null otherwise. The page shows "Work group X · #channel · lead Y · read N messages (A → B)".
- The page fires the view beacon.

## Layer 3 — work groups (apps/huddles models; apps/slack; apps/harness)

### `HuddleGroup`

| Field | Rule |
|---|---|
| workspace, slug (unique per workspace), name, topic | the topic is the work group's purpose, in one paragraph |
| lead (User), lead_slack_id | **required**. The lead must be a member of the workspace AND of the Slack channel. This is checked at save and again at every announcement (`users.lookupByEmail` + `conversations.members`). A failed check is refused with a plain reason. |
| installation, channel_id, channel_name | **required**. Saving the group puts the channel on the leader agent's read allow-list (`ingest.allow`, by the person saving). Deleting the group removes it. |
| agents | ≥1 agent slug, each in the workspace tree |
| leader_agent | **One agent:** that agent, single mode. **More than one:** required, and can be ANY agent (Jonathan, 2026-10-16: "any agent can run a huddle if told to, and it would need to abide by all ACLs and requirements"). At save and again at every announcement, the leader must be able to run every other listed agent: its login holds an admin/owner edge over each one, the same edges `agent_topology` serves. A failed check is refused, naming the agents the leader cannot run. |
| schedule (AgentSchedule, 1:1) | created and updated with the group, on `leader_agent`, with the group's cron and timezone |
| project (AgentProject) | **Single mode:** created active on that agent. Its name is the group's name, its outcome is the topic, its owner is the lead, and the lead is a `manual` participant. **Multi mode:** null. |
| enabled, created_by, created_at, updated_at | |

A workspace owner or admin may write a group; members may read groups.

### The run, a `HuddleGroupRun`

`(group, slot, status, announce_ts, confirmed_by, confirmed_at, ingest, read_messages, read_first_ts, read_last_ts, read_since, material, turn, huddle_id, result_url, finished_at, created_at)`.

`status`: `awaiting_confirm` → `reading` → `running` → `done`. It can also end at `skipped`, `expired`, or `failed`.

1. **The schedule fires an announcement, not a turn.** `fire_schedule` (and `run_schedule_now`) on a schedule that has a group does three things:
   - creates the run, idempotent per slot;
   - posts in the channel: "**<group> huddle** is due. <@lead>: Confirm to have <agents> read what was posted here since <last read> and work on it." The post has **Confirm** and **Skip** buttons.
   - records `run_announced`.

   No turn exists yet. The schedule's slot bookkeeping and nags stay as they are.
2. **Only the lead's click counts.** Anyone else who clicks gets a private note naming the lead.
   - **Skip:** records `run_skipped`, and the post is updated to say who skipped it.
   - **No click within 24 h:** the run is `expired`, the post is updated, and `run_expired` is recorded.
3. **Confirm** records `run_confirmed` (surface slack) and starts the read: the existing read-channel machinery, for `leader_agent`, with `pending_ask = {"group_run": <id>}`. When `_deliver` sees that, it hands off to `huddles.groups.deliver_read(run, transcript)`, which:
   - stores `material` and the read window;
   - calls `mark_read`;
   - sets `last_read_by_slack` to the lead;
   - enqueues the leader's turn. The schedule's prompt is the generic `/canopy:huddle`, the same for every leader and both modes, and the dispatch appends ` --group-run <id>`. `origin_ref` is `{schedule_id, slot, schedule_name, kind: "huddle_group_run", group, run}`.
   - posts the `:books:` read line in the announcement's thread, plus "started — follow it at <link>".
4. **The leader's turn runs.**
   - **Multi mode:** the huddle engine runs with `--group-run` (layer 4). Its outputs are proposed projects (layer 1).
   - **Single mode:** the agent works the group's project directly, from the material. Its outputs are tasks in that project.
5. **Finish:** `POST /api/huddle-groups/runs/{id}/finished {huddle_id?, result_url, summary}`, called by the leader's token.
   - The run becomes `done`.
   - One line is posted in the announcement's thread: ":white_check_mark: done — N proposed projects / N new tasks: <link>".
   - **The content lives in the UI. Slack gets a pointer, not the output.**

### API

Each route is also an MCP tool.

- `GET/POST /api/huddle-groups/`, `GET/PATCH/DELETE /api/huddle-groups/{slug}`
- `GET /api/huddle-groups/{slug}/runs`
- `GET /api/huddle-groups/runs/{id}`: material is included for workspace members and agents.
- `POST /api/huddle-groups/runs/{id}/finished`

### Frontend

- The Huddles page gains a **Work groups** section: a list, plus a create/edit form for admins with a channel picker, lead, agents, cadence and topic.
- A **group page** lists runs with their status, the read window, a link to the Slack post, and the result.

## Layer 4 — the engine and the bindings

- **canopy `/canopy:huddle [--group-run <id>]`:** a generic command. Any agent runs it under its own identity, repo, PAT, turn mode and send path, following `agent-core/huddle.md`.
  - With `--group-run` and one agent, the agent works the group's project directly (single mode).
  - With several agents, it leads the huddle.
  - Nothing in the engine assumes the leader is Ada. A team binding such as `/ada:huddle` stays valid as a thin wrapper.
- **canopy `huddle plan --group-run <id>`:**
  - Members are the group's agents. The leader leads, and is a member only if listed.
  - A brief is generated from the group (name, topic, lead) in place of a priorities file.
  - The channel material goes into rounds 1–2 as quoted material: "What the work group's channel said since the last huddle".
  - The anchor's `origin_ref` carries `group` and `run`.
  - The context pack carries the backings and replies on the group's earlier proposed projects, so people's reactions steer the next huddle.
- **canopy `huddle file`:**
  - each survivor → `projects/propose/`;
  - each round-3 answer → `backings/agents/{member}`: co-sign → `backs`, amend → `backs_with_changes`, decline → `passes`;
  - then `runs/{id}/finished`.
  - Outside a group run, `file` behaves the same, minus the finish call.
- **canopy `canopy huddle-group material --run <id>`:** prints a run's material. Single-mode turns use it.
- **Ada:**
  - `/ada:huddle --group-run <id>` takes everything from the group, sends no email (the output is in the UI), and closes with the huddle page.
  - Ada's weekly turn reads `engagement/summary` and gives Jonathan a short plain-words read of what people actually do.

## Order of work

1. canopy-web: layers 1 + 2 (one PR).
2. canopy-web: layer 3 (one PR, built alongside 1).
3. canopy: layer 4 engine.
4. ada: layer 4 binding.
