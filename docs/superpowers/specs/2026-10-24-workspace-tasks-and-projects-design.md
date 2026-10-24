# Workspace tasks and projects: design

**Date:** 2026-10-24 · **Status:** approved direction, being built · **Follows:** `2026-10-24-project-management-audit.md` (findings D1–D9, U1–U7)

## Decisions (Jonathan, 2026-10-24)

1. **A task is assigned to a person or an agent.** One assignee, either kind. Unlike Linear, an agent can be the assignee, not only a "delegate".
2. **A project belongs to the workspace** and can span agents. A project tied to one agent goes away as a concept.
3. **Teammates see a project's work and progress**, not only its overview.
4. **Status vocabulary: follow Linear.** Fixed status categories, including Backlog ("accepted, not now").

Jonathan asked for the design to be built without a design review, so this document is the record of the choices made under those four decisions.

## Principles

- **The workspace owns the work.** Tasks and projects are addressed in the workspace, never through an agent. An agent is a teammate that can be assigned work, lead a project or file a task, the same as a person.
- **One object, one URL, one rendering.** A task has one page (`/w/:ws/tasks/:key`). Every list shows it as the same dense row. The peek panel *is* that page, rendered beside the list.
- **Fixed categories, logic reads the category.** Progress, health, counts and agents read the status category; names are display.
- **An agent proposes, a person decides.** The ask / approve gate, `human_why` quotes, backing and confirm-before-read check-ins stay exactly as they are. Agent-drafted project updates are confirmed by a person.
- **Keyboard-first and agent-friendly.** Every action in the UI is an API route (so an MCP tool), takes a stable key, and has a shortcut.
- **The fleet keeps working.** Agents name tasks as `T12` and projects as `P5` today, through `/api/agents/{slug}/…` and the canopy CLI. Those routes keep working as views over the new model, and every old id still resolves, so no agent breaks on deploy.

## The model

Class names stay `AgentTask` and `AgentProject` (renaming them is churn across 100 files with no behaviour); the API and UI call them task and project.

### Task

| Field | Change | Meaning |
|---|---|---|
| `workspace` | **new**, NOT NULL | The workspace the task lives in. Migrated from its agent's workspace. |
| `number` | **new** | Per-workspace counter. With the workspace's prefix it makes the task's **key**: `DIM-142`. Keys are globally unique, never reused, and are what every caller, URL and agent names a task by. |
| `agent` | meaning narrowed, now nullable | The **assignee agent**: the agent doing the work. Null when a person is the assignee or nobody is. |
| `assignee_user` | **new** | The **assignee person**. A CHECK keeps at most one of `agent` / `assignee_user`. |
| `created_by_agent`, `created_by_user` | **new** | Who filed it. Migrated: `created_by_agent = agent`. |
| `ext_id` | becomes the **agent's alias** | `T12`, unique per `created_by_agent`. Every id an agent already holds still resolves, and a task filed through an agent's own route still gets one (the canopy CLI numbers tasks on the client and keys retries by it). A task made anywhere else has none, and its `ext_id` in the API is its key. |
| `status` | two values added | See statuses below. |
| `priority` | **new** | 0 none, 1 urgent, 2 high, 3 medium, 4 low (Linear's scale). |
| `labels` | **new**, M2M `Label` | Workspace labels. |
| `milestone` | **new**, FK `ProjectMilestone` | Inside its project. |
| `snoozed_until` | **new** | Hidden from Triage until then, or until something new happens on it. |
| `rank` | **new**, replaces `position` | A fractional rank (float) so a drag reorders one row. `position` stays accepted on input and maps onto it. |
| `confidence` | normalised | `""`, `high`, `medium`, `low`. Other values are migrated to `""`. |
| `owner`, `assigned` | kept, deprecated | Free text from before people could be assigned. Still accepted from agents, shown only when no real assignee is set. |

Everything else (the ask, `on_approve`, `human_why`, `parent`, `kind`, provenance, Drive folder) is unchanged.

**Statuses.** The stored values stay the same strings so no agent's skill breaks; two are added:

| Value | Category | Shown as | Notes |
|---|---|---|---|
| `suggested` | triage | Triage | An agent proposed it; a person decides |
| `backlog` | backlog | Backlog | **new**. Accepted, not now |
| `todo` | unstarted | Todo | **new**. Accepted, next up |
| `in_progress` | started | In progress | |
| `done` | completed | Done | |
| `declined` | canceled | Canceled | |

`LIVE_STATUSES` (open work) is the first four. Workspaces may rename the display names per category (`TaskSettings.status_names`); code reads the category.

**Actions.** The five actions stay. Four join them, all at the same tier as approve / decline (deciding what is on the board):

| Action | Does |
|---|---|
| `accept` | triage → todo. Closes a review ask ("yes, but not now"). Starts nothing. |
| `backlog` | → backlog. Closes a review ask. Starts nothing. |
| `snooze` | Sets `snoozed_until` (a date in the comment, default a week). Status unchanged. |
| `duplicate` | → declined, and records a `duplicate` relation to the task named in the comment. |

`approve` still means **start now**: on a task assigned to an agent it starts that agent's turn, as today. On a task assigned to a person it moves the task to In progress and starts nothing.

### Project

| Field | Change | Meaning |
|---|---|---|
| `workspace` | **new**, NOT NULL | Owner. Migrated from `owner_workspace`, else the agent's workspace. Replaces `scope` and `owner_workspace`, which are dropped. |
| `handle` | now **required** | Unique in its workspace. Migrated agent projects get one from their name. The project's one address is `<workspace>/<handle>`, its one page `/w/<ws>/projects/<handle>`. |
| `agent` | meaning: **lead agent**, now nullable | The agent that keeps it current and whose Drive holds `Projects/<name>`. Was the owning agent, or the steward. |
| `owner_user` | meaning: **lead** (person) | The person accountable. |
| `member_agents` | **new**, M2M Agent | The agents working on it. The lead agent is always one. Any agent in the workspace may still file tasks into it. |
| `ext_id` | **legacy alias** | `P5`, still resolved through `/api/agents/{slug}/projects/P5/`. |
| `status` | three values added | `proposed`, `planned` **new**, `active`, `paused` **new**, `done`, `canceled` **new**, `archived`. |
| `start_date`, `target_date` | **new** | |
| `priority` | **new** | Same scale as tasks. |
| `health` | **new**, derived | The latest published `ProjectUpdate`'s health; `update_missing` when an active project has had no update within the workspace's cadence. |
| KB columns | **moved** | To a one-to-one `KnowledgeBase` (D8). |

A proposed project is started only through its decision card. A PATCH from `proposed` to anything but `canceled`/`archived` is refused, which ends the second way to start one (U2).

### New models

- **`TaskSettings`** (one per workspace): `prefix` (unique, immutable once a task uses it, derived from the slug: `DIM`), `next_number`, `status_names` (JSON, category → name), `update_cadence_days` (default 7). Changing it is a setting (`@gate(setting=True)`).
- **`Label`**: `workspace`, `name` (unique there), `color` (a semantic token name).
- **`TaskRelation`**: `from_task`, `to_task`, `kind` `blocks` | `related` | `duplicate`. "Blocked by" is the reverse of `blocks`, read, never stored.
- **`TaskEvent`**: one append-only stream per task: `created`, `field` (field, old, new), `action` (links its `AgentTaskAction`), `relation`. By a user or an agent. Written by `services` on every create, PATCH, action and relation change, from every entry point. `AgentTaskAction` stays the agent's queue; a reply is the comment.
- **`ProjectMilestone`**: `project`, `name`, `description`, `target_date`, `rank`. Progress is the share of its tasks done, by category.
- **`ProjectUpdate`**: `project`, `health` `on_track` | `at_risk` | `off_track`, `body`, `author_user` / `author_agent`, `status` `draft` | `published`, `checkin_run` (nullable), `diff` (JSON: what changed since the last update: target moved, milestones, tasks done). A check-in run's finish may carry a draft; the lead publishes it, choosing the health.
- **`SavedView`**: `workspace`, `owner`, `name`, `shared` (personal or the workspace's), `filters` (JSON), `group_by`, `sort`, `layout` `table` | `board` | `calendar`, `rank`.
- **`ProjectService`**: what a project serves to its workspace, one row per thing, by `kind`. A knowledge base is the first kind and holds every `kb_*` column (see "What a project serves to the workspace").

`ProjectCheckin.agents` becomes a many-to-many to Agent (D6).

## Access

The rules move from "the agent's workspace" to "the task's workspace", and add the assignee:

- **Read** a task or project: a member of its workspace (as today, through the same scoping helpers). Teammates now get a project's tasks and progress, not only its overview (decision 3). Turn prompts stay redacted through `turn_access`.
- **Decide** (approve, decline, reply, accept, backlog, snooze, duplicate): any member (the viewer tier, unchanged).
- **Edit fields, create, nudge, done**: `perms.AGENT_WORK` in the task's workspace; **or** the task's assignee person; **or** the login of its assignee agent or of the agent that filed it. Writing `on_approve` stays editor-or-the-agent-itself, because it is work that runs on approval.
- **Assign to an agent** requires `AGENT_WORK`. The agent must live in the task's workspace or one above it (the rule workspace projects already use). Assigning starts nothing; approving does.
- A view that judges many tasks reads the facts once (`perms.slugs_with`, `access.Standings`) and never calls `perms.can` per row (U7).

## The API

New, workspace-native routes (each an MCP tool automatically):

| Route | |
|---|---|
| `GET /api/tasks/` | Extended: `assignee=me\|<email>\|agent:<slug>`, `created_by`, `project=<ws>/<handle>`, `label`, `priority`, `milestone`, `category`, `kind`, `due_before`/`due_after`, `q`, `sort`, `snoozed`, `limit`. |
| `GET /api/tasks/count/` | Same filters, a number (the badge, U7). |
| `POST /api/tasks/` | Create one task in a workspace (`creation_workspace`). |
| `GET/PATCH /api/tasks/{key}/` | One task, with events, relations and the assignee agent's current turn state (`turn_status`). |
| `POST /api/tasks/{key}/actions` | The nine actions. |
| `POST /api/tasks/bulk` | One PATCH to many keys (bulk edit). |
| `POST/DELETE /api/tasks/{key}/relations/` | |
| `GET /api/triage/` | Suggested tasks, ideas and proposed projects, unsnoozed, oldest first. |
| `/api/labels/` | List, create, update, delete. |
| `/api/views/` | Saved views: list, create, update, delete. |
| `/api/task-settings/` | Read and change a workspace's prefix, names and cadence. |
| `/api/workspace-projects/{ws}/{handle}/` | Now **every** project's address (they all have handles). Plus `…/updates/`, `…/milestones/`. `POST /api/workspace-projects/` creates any project. |

Kept, as views over the new model, so the fleet keeps working: every `/api/agents/{slug}/tasks/…`, `/api/agents/{slug}/projects/…` and `/api/agents/{slug}/actions/…` route. There a task ref is a key or that agent's `T12`; a project ref is a handle or its `P5`. "The agent's tasks" means the tasks it is assigned, plus the ones it filed that no other agent has; "its projects" means the projects it leads or is a member of. Every task in an answer carries its `key`; its `ext_id` is its agent alias where it has one, else the key.

## The UI

- **One `TaskRow`** (dense table row: key, status icon, priority, title, labels, project, assignee, due, age). It replaces `TaskCard` grids, the Declined mini-card, Home's re-implemented `AttentionRow`, the workspace-project list rows, the duplicate ProposalCard/TaskCard and the huddle task line.
- **Task page** `/w/:ws/tasks/:key`, also opened as a side **peek** (`?peek=KEY`) from any list. Header properties inline-editable (status, assignee, priority, project, milestone, due, labels, parent); the ask with its decision buttons; human why first; plan and next action; subtasks as rows; relations; the assignee agent's session strip (turn state from `turn_status`, link to the turn); activity with the reply box.
- **Tasks** `/w/:ws/tasks`: one table across the workspace. Tabs: My tasks (assigned to me), Waiting on me, Created by me, All. Filters, group, sort and layout (table / board by category with drag / calendar by due date), all in the URL and saveable as a view. Bulk select with `X`, bulk edit bar.
- **Triage** `/w/:ws/triage`: one row per suggestion, idea or proposed project, with its reasons and backing inline. Keys: `A` accept, `S` start now, `B` backlog, `D` decline, `H` snooze, `M` mark duplicate.
- **Projects** `/w/:ws/projects`: table with status, health, lead, agents, progress, target date. The agent's Projects tab is the same table filtered to that agent; its Tasks tab is the Tasks table filtered to it.
- **Project page** `/w/:ws/projects/:handle` (one page for everyone): property header (status, health, lead, agents, start, target, priority); latest update with "update missing" and a post-update form; milestones with progress; scope-vs-done graph; tasks table; ideas; backers; check-ins as a table with a cadence preview from `next_runs`; activity. Teammates see all of it except the controls they may not use. The old agent-board project address redirects here.
- **Command palette** (`⌘K`): jump to any task, project, agent or page; create a task or project; act on the open task. Shortcuts: `C` create, `Space` peek, `X` select, `J`/`K` move, `Esc` close, `?` lists them.
- **One vocabulary**: project, task, idea, proposal; status words from the category; "waiting" only ever means waiting on you; the opted-in repos are "Opted-in repos"; the header's Projects menu becomes **Demos**.

## What a project serves to the workspace

Once a project is the workspace's and spans its agents and people, it is the natural place to hang the things the workspace shares (Jonathan, 2026-10-24). Knowledge bases were the first such thing: a project that serves its own MCP server at `/api/mcp/projects/<ws>/<handle>/`.

So the knowledge-base columns do not move to a one-off `KnowledgeBase` table (D8). They move to **`ProjectService`**, one row per thing a project serves, with a `kind`:

| Kind | What the project serves | Status |
|---|---|---|
| `kb` | A knowledge base, as its own MCP server (every `kb_*` field, as today) | Moved in this change |
| `mcp` | A remote MCP server the workspace shares: its URL and how to add it | Next |
| `skill` | A skill (a repo path) every agent working on the project loads | Next |
| `plugin` | A Claude Code plugin, listed in the workspace's catalog | Next |

What every kind shares, and why the project is the right owner:
- **Who may use it** is the project's reach: members of its workspace (and its lead agent's), narrowed per kind where the source is private (a KB built from someone's Drive keeps its readers list).
- **Who keeps it current** is the project's lead agent, or an editor.
- **Where it is listed**: on the project page, and in the workspace directory (`GET /api/mcp-servers/`, which becomes every project's served things).
- **Its history**: the project's activity, like any other project change.

## Compatibility with the fleet

- Every stored status string, `T12` and `P5` keeps working on input. Outputs add fields; none are removed from the agent routes.
- The canopy CLI keeps working unchanged: it numbers tasks per agent on the client, keys retries `<slug>:<ext_id>`, and names the agent in every URL, all of which the agent routes still honour.
- One thing the CLI reads wrongly until it is updated: it maps any status it does not know to `suggested`, and its `--open` filter is `suggested, in_progress`. So a task in Backlog or Todo shows to an agent as suggested and drops out of its open list. The canopy repo's follow-up adds the two statuses, shows keys, and teaches `task-tracker.md` about assignees and Todo.

## Out of scope (deliberately)

Estimates and story points, Gantt charts, unlimited custom fields, a task in several projects at once, cycles, and autonomous agent edits with no review (audit §3).
