# Project management in canopy-web — audit and direction

**Date:** 2026-10-24 · **Status:** audit, for decision · **Scope:** `AgentProject`, `AgentTask` and everything hanging off them (backing, ideas, check-ins, actions), plus every screen that shows them.

## Verdict

The **model of who decides what** is strong, and in places ahead of commercial tools:
- the ask / approve gate,
- `human_why` quotes (a person's own words, verbatim and attributed),
- backing that goes stale when a proposal is revised,
- check-ins that read a channel only after the lead confirms.

Linear only reached the same ideas recently, with Customer Requests and agent elicitations.

The **PM basics** are thin, and the UI is thinner still:
- A task has no page, no create form and no edit controls.
- The same task is drawn about six different ways.
- The data model has no priority, labels, dependencies, start or target dates, milestones, health, saved views, or history of field changes.
- Every task belongs to exactly one agent, so a person cannot own work and an initiative spanning agents is split across several projects.

Recommendation, in three phases:
1. **UI-only** quick wins. No migrations, and most of the API already exists.
2. **Additive model** work.
3. **One structural change:** separate the accountable person from the agent doing the work.

---

## 1. Data model

### What exists
| Model | Where | Shape |
|---|---|---|
| `AgentProject` | `apps/agents/models.py:1250` | FK `agent`, `scope` agent\|workspace, `ext_id` P1…, `name`, `outcome`, `status` proposed/active/done/archived, `owner_user` + free-text `owner_note`, Drive folder, `repo_slug`, `notes`, `links` JSON, `source_url`, `proposed_at`, `spawned_from`, participants. **Also** about 10 knowledge-base columns (`mcp_kind`, `kb_*`) |
| `AgentTask` | `models.py:1673` | FK `agent`, `ext_id` T1…, `project`, `parent` (depth ≤3), `kind` work\|idea, `title`, `next_action`, `status` suggested/in_progress/done/declined, free-text `owner`, `assigned`, `confidence`, `score`, plus `rationale`, `plan`, `review`, `notes`, `due`, `links` JSON, `waiting_on_user`, the ask (`ask_kind/body/closed_at`), `on_approve` JSON, `position` int, provenance (`origin`, `raised_by`, `batch_key`, `idempotency_key`) |
| `AgentTaskAction` | `models.py:1932` | approve / decline / reply / nudge / done. Serves as both history and the agent's to-do queue |
| `AgentTaskHumanWhy` | `models.py:1876` | One verbatim quote per row, FK person |
| `ProjectBacking` | `models.py:1601` | backs / backs_with_changes / passes with a line, on a project **or** a task, from a person **or** an agent, with `stale` |
| `ProjectParticipant` | `models.py:1533` | Person ↔ project, `source`, contributor-page `summary` |
| `ProjectCheckin`, `CheckinRun` | `apps/huddles/models.py:20,86` | A recurring meeting on a project; each run's `summary` and `tasks_proposed` |

### Findings

**D1. A task belongs to an agent, not to a person.**
- `AgentTask.agent` is the task's identity: `ext_id` is unique per agent (`models.py:1818`), and the address is `<agent>/T12`.
- The human side is free text. `owner` is a string whose help text says "never the agent", and `assigned` is a string. Only `waiting_on_user` is a real foreign key.
- Consequences:
  - A person cannot own a task.
  - A task cannot be handed from one agent to another without changing its id.
  - "My tasks" can only mean "waiting on me".
  - Tasks filed by different agents into a workspace project carry colliding T-numbers. `project_address` exists to work around this (`models.py:1841`).
- Every tool researched separates the accountable human (**assignee**) from the agent doing the work (**delegate**). Linear: "issues can only be assigned to humans, and only delegated to agents".

**D2. An agent project is single-agent.**
- "Two agents on one initiative have a project each" (`models.py:1263`).
- Workspace projects were added on top of `AgentProject` with a `scope` flag, a CHECK constraint, a second addressing scheme (`handle`) and a second URL. The result is two addresses (`url` and `board_url`, `models.py:1427-1437`), and the redirect logic is spread through the frontend.

**D3. Statuses are too coarse to plan with.**
- Tasks have four statuses. There is no "accepted, not now". `defer` was removed (spec 2026-10-07), and approving means "start now".
- So a person cannot queue work, and suggested tasks pile up as the de facto backlog.
- Projects lack `paused` and `canceled`; a declined proposal becomes `archived`.
- No status maps to a fixed category, which is what every serious tool reads for roll-ups:
  - Linear: triage, backlog, unstarted, started, completed, canceled.
  - Jira: To Do, In Progress, Done.
- Minor: `Meta.ordering = ["status", …]` sorts statuses alphabetically. Projects come out active, archived, done, proposed, with proposals last. Routes override this, but anything that forgets gets the wrong order.

**D4. The planning fields are missing.**
- There is no priority, estimate, start date, target date (projects have none at all), labels, milestones or cycles.
- There are no relations: blocks, blocked-by, related, duplicate.
- Dependencies matter more with agents than with people, because an agent that starts work blocked on another agent's output wastes a turn.

**D5. There is no history of field changes.**
- `services.patch_task` writes no event. Reassigning, refiling, retitling, changing the due date or setting status by PATCH all leave no trace.
- `AgentTaskAction` records only the five actions, and it is both the history and the agent's queue.
- Comments exist only as `reply` actions, with no thread.
- Linear and Asana both keep a single activity stream per item: field changes, comments, and agent activity.

**D6. Text fields stand in for structured ones.**
- `confidence` is free text meant to hold "high/low". `score` is free text ("A-", "4/5").
- A task carries six prose blobs: `rationale`, `plan`, `notes`, `review`, `next_action`, `ask_body`.
- `links`, `on_approve` and `origin_ref` are JSON.
- `ProjectCheckin.agents` is a JSON list of slugs rather than a many-to-many (`huddles/models.py:45`).

**D7. Project health is not recorded.**
- A project has no health indicator and no update stream.
- `CheckinRun.summary` is the closest thing to a status update, but it is a meeting record. It has no on track / at risk / off track judgement, and nothing flags an update as missing.

**D8. Knowledge-base serving is a different object stored in the project table.**
- About 10 `kb_*` and `mcp_kind` columns are meaningful only for workspace projects that serve a KB.
- They belong in a one-to-one `KnowledgeBase` table, which would keep the project model about work.

**D9. Smaller issues.**
- `position` is an integer per column. Drag-to-reorder will need a fractional rank to avoid renumbering every row.
- The `AgentTask` docstring still says "Source of truth is a Google Sheet … synced wholesale" (`models.py:1674`). This is no longer true.

### Strengths to keep, not replace
- **Ask / approve gate.** The equivalent of GitHub Copilot's rule that an agent cannot approve its own work. Height shut down after shipping autonomy with no gate.
- **`AgentTaskHumanWhy`.** The same design as Linear Customer Requests: a quoted need, attributed, kept apart from the agent's reasoning.
- **`ProjectBacking` with `stale` on revision.** Better than Linear's single "important" flag.
- **`spawned_from`, `source_url`, `raised_by`.** Provenance most tools do not have.

---

## 2. UI

### What exists
| Surface | Route | Layout |
|---|---|---|
| Workspace projects | `/w/:ws/projects` | Table, the only real one (`pages/projects/ProjectsPage.tsx`) |
| Agent projects | `/w/:ws/agents/:slug/projects` | Bordered cards stacked as rows (`AgentProjectsSection.tsx:111`) |
| Project page | `…/projects/:ref` | Proposal card, overview, ideas, proposed tasks, check-ins, timeline; work and activity shown to editors only (`AgentProjectPage.tsx:271`) |
| Agent tasks | `…/agents/:slug/tasks` | Two-column card grids in "who has the ball" sections (`TasksBoard.tsx:859`), not a kanban |
| Home / Supervisor | `/home`, `/supervisor` | Attention rows; Supervisor's Waiting on you is TaskCards |

### Findings

**U1. A task cannot be created, opened or edited.**
- There is no task route (`router.tsx:427` has only `tasks`). `getTask` and `patchTask` in `api/agents.ts` have no callers.
- A long `ask_body`, full notes and the action history can't be reached.
- No screen can add a work task or subtask, assign, set a due date, move a task to another project, reparent or reorder.
- Project fields other than status are shown read-only even though `AgentProjectPatch` accepts them.
- Projects can only be created from an agent's page. The workspace index's empty state sends you there.

**U2. One object, many renderings.**
- A task is drawn about six ways:
  - `TaskCard`
  - the Declined mini-card
  - Home's `AttentionRow`, which re-implements Approve / Decline / Reply with different copy and without the "starts now" warning
  - the read-only workspace-project list
  - the check-in timeline
  - `ProposalCard` and the huddle `TaskLine`
- Their headlines don't match: TaskCard leads with `next_action`, the others lead with `title`.
- For editors, a suggested check-in task appears **twice on the same project page**, as a ProposalCard ("Start this task") and as a TaskCard ("Approve"). `proposed_tasks` is a subset of `tasks` (`api_board.py:150`).
- A proposed project can be started two ways with different effects: through the decision card, or by setting the status select from proposed to active, which skips the decision.

**U3. Cards where the house rule says tables.**
- Agent projects, the task board, subtasks (nested as full cards), check-ins, the check-in timeline and proposals are all cards.
- Thirty live tasks render thirty reply text boxes.

**U4. No views.**
- Missing:
  - a workspace-wide task list
  - filters by person, assignee, due date or kind
  - column sorting
  - a kanban board
  - a due-date calendar or timeline
  - saved views
  - bulk select
  - a command palette or keyboard shortcuts
- "My tasks" exists only as "waiting on me", in three places.
- Home folds "Tasks and requests" by default (`HomePage.tsx:53`), on the page meant to show what is waiting.

**U5. The same words mean different things.**
- "Project" means four things:
  - an agent project
  - a workspace project
  - the emdash session project shown on Home and `/ide`
  - an opted-in repo ("Canopy projects")
- The header's **Projects** menu holds Storyboards, Walkthroughs, DDD and Shareouts.
- The same row is labelled suggested / in progress / done / declined as a task, and Proposed / Started / Shipped / Not pursued as an idea.
- "Idea" means a person's `kind:"idea"` task on a project, and an agent's proposal in a huddle.
- "Waiting" means three different things:
  - On project counts: waiting on anybody.
  - The Tasks chip: waiting on you.
  - `ProjectGroupHeader`: worked out in the browser as `suggested || ask_open` (`projectParts.tsx:63`), which disagrees with the server's `waiting_q`.

**U6. Dead ends.**
- `ProjectGroupHeader` shows "Mark done" to viewers, with no confirm and no `.catch` (`projectParts.tsx:83-93`).
- "← Projects" always goes to the agent's list, even for someone who arrived from the workspace index.
- The Tasks page count includes subtasks the board doesn't draw.
- Check-ins take a raw cron string, though the API already returns `next_runs` for a preview.
- Teammates see no work on a project page. This is deliberate (see the comment at `AgentProjectPage.tsx:268`), but it also hides progress from the people the project is for.

**U7. Performance.**
- The project page builds `proposals.task_card` for each suggested task. Each card calls `perms.can` through `may_decide_task` (`proposals.py:517`), against CLAUDE.md's rule that a view judging many pairs reads the facts once.
- `project_detail` loads every task, done ones included, with no pagination.
- Both `AgentProjectPage` and `ProjectOverview` refetch on every `TASK_RESOURCE` event anywhere in the workspace, with no debounce, and card actions also call `reload()` directly.
- The rail badge downloads the whole `?waiting=me` list just to count it.
- `ProjectsPage` fetches every status and filters in the browser.

---

## 3. What the best tools do (condensed)

Full research, with sources, is in the session transcript.

| Idea | From | Fit for canopy |
|---|---|---|
| **Assignee (human, accountable) ≠ delegate (agent, doing it)**, both filterable, both in My Tasks | Linear agents | Fixes D1. The single highest-value change |
| **Agent session state derived from emitted activity** (pending / active / awaitingInput / error / complete / stale) with a typed activity stream and a plan checklist on the item | Linear | We already derive turn status once (`harness/turn_status.py`). Surface it on the task |
| **Elicitation = waiting on you** | Linear | We have this (`ask_kind=question`). Keep it |
| **Agent can't approve its own work; the requester isn't the only approver; only writers' comments reach the agent** | GitHub Copilot agent | Mostly true here already; worth stating as invariants |
| **Fixed status categories, custom names** | Linear, Jira, Plane | Fixes D3. Roll-ups and agents read only the category |
| **Triage queue**: Accept 1 / Duplicate 2 / Decline 3 / Snooze H (wakes early on new activity) | Linear, Plane Intake | Suggested tasks and ideas need this rather than a card grid |
| **Project updates**: health plus a short note, a reminder to the lead, an "update missing" state, auto-appended changes (target moved, milestone +x%) | Linear, GitHub Projects | Check-ins become the update producer (D7) |
| **Agent drafts the update, a human picks the health** | Asana Smart Status, Linear's rationale | Fits our model: the agent proposes, the person decides |
| **Backing as an entity with the backer's own words, sortable by count** | Linear Customer Requests | We have the entity. Add sort and filter by backing, and a Backers tab |
| **Saved views as objects** (scope, favourite to sidebar, landing page, subscribe to new matches; filters personal unless saved for everyone) | Linear, Notion | Replaces URL-only filters |
| **List + peek + full page, one URL per item** | Notion, Linear | Fits the one-URL rule: the peek *is* the task's page rendered in a panel |
| **Keyboard-first**: Cmd-K, C create, Space peek, X select, bulk edit | Linear | Phase 2 |
| **Scope-vs-done progress graph** | Linear | Shows agent-generated subtasks inflating scope |
| **Relations**: blocks / blocked by / related / duplicate, reverse created automatically | Jira, Linear, GitHub | D4 |
| **Hand-off when entering a column** (moving into a state hands to an agent) | Jira Rovo | Later. `on_approve` already does a narrow version |

**Deliberately not adopting:**
- Story points and estimates: agents don't need them, and people won't maintain them.
- Gantt charts.
- Unlimited custom fields (ClickUp).
- Multi-homing a task in N projects (Asana): it conflicts with the per-project Drive folder.
- Autonomous agent edits with no review (Height's failure mode).

---

## 4. Target shape

### Model
- **Task**
  - `assignee` (FK user, accountable). Defaults to the project lead; may be blank only for a suggestion.
  - `delegate` (FK agent, nullable): the agent doing it. Replaces the agent-as-identity role of `agent`.
  - Workspace-scoped key `<WS>-142` alongside a legacy `ext_id` alias.
  - Drop free-text `owner`; keep `assigned` only as a label for counterparts canopy doesn't know.
- **Status categories**
  - Task: `triage` (= suggested), `backlog` (accepted, not now), `started` (= in_progress), `completed` (= done), `canceled` (= declined).
  - Project: `proposed`, `planned`, `started`, `paused`, `completed`, `canceled`.
  - Rename statuses freely later; logic reads the category.
- **`TaskEvent`**: one append-only stream per task, covering field changes, comments and actions, with agent turns linked. `AgentTaskAction` keeps its queue role or becomes `TaskEvent` rows with `pending`.
- **Planning fields:** `priority` 0–4, `start_date`, `target_date` (on project too), and workspace `Label` many-to-many.
- **`TaskRelation`:** type is blocks, related or duplicate.
- **`ProjectUpdate`:** health on_track / at_risk / off_track, body, author, auto-diff, optional `checkin_run`. A check-in run *produces* a draft update that the lead confirms.
- **`ProjectMilestone`:** name, target, description; tasks FK to it.
- **Project membership:** a project has a `lead` (user) and **member agents** (many-to-many), so one project spans agents. A workspace project becomes simply a project with a handle. The steward becomes the lead agent, and the second addressing scheme goes away.
- **`KnowledgeBase`:** one-to-one with project, taking the `kb_*` columns.
- **Smaller fixes:**
  - `ProjectCheckin.agents` becomes a many-to-many.
  - `position` becomes a fractional rank.
  - `confidence` becomes an enum.
  - Fix the stale docstring and the alphabetical `Meta.ordering`.
- **`SavedView`:** workspace, owner, scope personal/workspace, filters JSON, group, sort, layout.

### UI
- **One `TaskRow`** (dense table row) and **one task page** at `/w/:ws/tasks/:key`, also rendered as a side peek from any list. Every current task rendering is replaced.
- **Task page anatomy:**
  - Header properties: status, assignee, delegate agent, priority, project, milestone, due, labels, all inline-editable.
  - The ask and its decision buttons.
  - Human why (quotes first).
  - Plan and next action.
  - Subtasks as rows.
  - Relations.
  - The **agent session strip**: current turn state from `turn_status`, plus a link to the turn.
  - The activity stream, with comments.
- **Workspace Tasks** (`/w/:ws/tasks`): one table across agents. Filters (assignee, delegate, project, status category, kind, label, due), group, sort and saved views. Layout toggle: table / board by status category (drag, rank) / calendar by due date. "My tasks" = assignee is me, with tabs for Waiting on me and Delegated by me.
- **Triage** (`/w/:ws/triage`, or a tab on Tasks): suggested tasks, ideas and proposed projects, one per row. Keys: Accept / Backlog / Decline / Snooze / Duplicate. The agent's suggested project and owner come with its reasons. Backing is shown inline.
- **Project page anatomy (Linear's):**
  - Property header: status, health, lead, member agents, target.
  - Latest update, with "update missing".
  - Milestones with progress.
  - Scope-vs-done graph.
  - Tasks table.
  - Ideas.
  - Backers.
  - Check-ins as a table with a cadence preview.
  - Activity.
  - Teammates see progress (counts, milestones, latest update) without the admin controls.
- **Agent projects list** becomes the same table as the workspace one, filtered to the agent.
- **Cmd-K palette** (create task or project, jump to anything, assign), plus `C`, `Space`, `X`.
- **One vocabulary:**
  - project / task / idea / proposal.
  - Status words come from the category.
  - "Waiting" always means "waiting on you".
  - Rename the repo opt-in ("Opted-in repos") and the header menu ("Demos" or "Artifacts").

---

## 5. Roadmap

### Phase 1: UI only, no migrations (about 1–2 weeks)
1. Task page plus side peek, using existing `getTask`/`patchTask`, at `/w/:ws/agents/:slug/tasks/:ref` (the address of today's model). Inline edit of title, next action, waiting-on, project, parent, due and status.
2. "Add task" on the project page and the Tasks page; full project edit (name, outcome, owner, links, repo).
3. One `TaskRow` table, replacing the TaskCard grids, nested subtask cards, `AttentionRow`'s re-implementation and the duplicate ProposalCard/TaskCard on the project page. Make the status select respect the decision card.
4. Workspace Tasks table over the existing `GET /api/tasks/` (fleet route, unused today), with filters held in the URL.
5. Agent projects list, check-ins and timeline become tables; cron preview from `next_runs`.
6. Vocabulary pass (U5); Home shows tasks unfolded; fix `ProjectGroupHeader` (gate, confirm, catch) and the "← Projects" back link.
7. Performance: batch `task_card` / `may_decide_task` through the bulk-authz helpers, debounce `TASK_RESOURCE`, add a count endpoint for the rail badge, filter projects by status on the server, and paginate done tasks on the project page.

### Phase 2: additive model (about 2–4 weeks)
1. `TaskEvent` history for every PATCH, plus comments as a thread.
2. `priority`, `start_date`, `target_date`, `Label`, `TaskRelation`.
3. `backlog` task status and `paused`/`canceled` project statuses, behind a status-category mapping.
4. `ProjectUpdate` with health, produced as a draft by check-in runs and confirmed by the lead; "update missing" on the project list and Home.
5. `ProjectMilestone` and the progress graph.
6. Triage view with keyboard actions; `SavedView`; Cmd-K.
7. Split `KnowledgeBase` out of `AgentProject`.

### Phase 3: structural (needs a design spec and a decision)
1. **Assignee / delegate split.** A task is accountable to a person and delegated to an agent. `agent` becomes `delegate`, keys become workspace-scoped, and agent CLIs and skills move to the new address. This touches `access.decide`, the claim path, the CLI, MCP tools and every agent's task-tracker skill, so it needs its own spec.
2. **Multi-agent projects.** Lead plus member agents. Workspace projects fold into ordinary projects; `scope`, `handle` vs `ext_id` and `board_url` vs `url` collapse into one address.

---

## Decisions needed
1. Do humans own tasks? That is, is the assignee/delegate split (Phase 3.1) the direction? Most other changes are easier if they're designed toward it.
2. Should a project span agents (Phase 3.2), or does it stay one agent's work with workspace projects as the shared exception?
3. Should teammates (non-editors) see a project's work and progress, or stay limited to the overview?
4. Status vocabulary: adopt the category set above, including a `backlog` ("accepted, not now"), which the 2026-10-07 spec removed as `defer`?
