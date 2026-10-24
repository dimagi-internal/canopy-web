# Work: tasks and projects

Tasks and projects belong to a **workspace**, not to an agent. Design record: `docs/superpowers/specs/2026-10-24-workspace-tasks-and-projects-design.md`; every route: `api-surface.md` (Agents).

## The nouns

- **Task** (`AgentTask`, `apps/agents/models.py`). Named by its **key**, `<PREFIX>-<number>` (`DIM-142`): the workspace's prefix (`TaskSettings.prefix`, derived from its slug, unique across canopy, frozen once a task carries it) and a counter that never goes back. A task that moves to another workspace (its project moved) takes a new key there; its old key still finds it (`work.rehome`, a `key` event).
- **Assignee**: a person (`assignee_user`) or an agent (`agent`), never both (CHECK `task_one_assignee`), or nobody. An agent assignee is who approval starts a turn for; a person assignee is who "My tasks" lists it for.
- **Agent alias** (`ext_id`, `T12`): the name the agent that filed it (`created_by_agent`) gives it. Unique per that agent and kept through reassignment. A task filed through an agent's own route always gets one (the canopy CLI numbers tasks on the client); a task made anywhere else has none.
- **Project** (`AgentProject`). Addressed `<workspace>/<handle>` (`url`: `/w/<ws>/projects/<handle>`, its one page). Led by a person (`owner_user`, "lead") and/or a **lead agent** (`agent`, which holds its `Projects/<name>` Drive folder), worked on by **member agents** (the lead agent is always one). Any agent of the workspace, or one above it, may file tasks into any project. Its `P5` alias is its maker agent's.
- **Statuses** are fixed values, each in one Linear category; code reads the category (`AgentTask.CATEGORY`, `AgentProject.CATEGORY`). A workspace may rename how a category is shown (`TaskSettings.status_names`).

  | Task status | Category | | Project status | Category |
  |---|---|---|---|---|
  | `suggested` | triage | | `proposed` | triage |
  | `backlog` | backlog | | `planned` | unstarted |
  | `todo` | unstarted | | `active` | started |
  | `in_progress` | started | | `paused` | paused |
  | `done` | completed | | `done` | completed |
  | `declined` | canceled | | `canceled`, `archived` | canceled |

## Rules

- **One writer per object.** Tasks: `services.create_tasks` / `patch_task` / `act`. Projects: `services.new_project` (and `create_project` over it) / `patch_project` / `promote_project` / `set_steward` / `set_member_agents`. The models fill what a row may not lack on save (workspace, key, handle), so a test or a migration that builds a row directly still gets a whole one.
- **Every change is history.** `work.record_created`, `record_changes` (each tracked field, as shown) and `record_action` write `TaskEvent`s from every writer; a reply's comment is the comment thread. `AgentTaskAction` stays the assignee agent's queue (`pending` until it drains them).
- **Approve means start now.** On an agent's task it starts that agent's turn (`on_approve`, or one written from the card); on a person's it moves the task to in progress and starts nothing. `accept` (→ todo) and `backlog` decide without starting. A proposed project starts only through its decision card: a PATCH out of `proposed` is 409.
- **An agent proposes, a person decides.** An agent's project update is always a draft; a person publishes it, choosing the health. Check-in runs draft one when they finish with `update`.
- **Access** (`apps/agents/gates.py`). Read: a member of the task's or project's workspace (a project also of its lead agent's). Decide (approve, decline, reply, accept, backlog, snooze, duplicate): any member. Change fields: an editor (`AGENT_WORK`), the task's assignee person, or the login of its agent or its filer; a project: an editor of its workspace or its lead agent's, its lead, or a working agent's login. Giving a task to an agent is the editor's. A view over many tasks reads the facts once (`proposals.task_cards`).
- **The fleet keeps working.** `/api/agents/{slug}/tasks|projects|actions/…` are an agent's view: its tasks (assigned, plus filed-and-unassigned-to-an-agent), its projects (leads or works on), refs by key or alias. Stored status strings never changed; two were added.

## What a project serves

A project can serve things to its workspace — `ProjectService` rows, one per thing, by `kind`. Its people may use them, its lead agent (or an editor) keeps them current, and the project page and the workspace directory list them.

- `kb` — a **knowledge base**, served by canopy as the project's own MCP server at `/api/mcp/projects/<ws>/<handle>/`; one per project, holding every `kb_*` field (published by the lead agent's curator through `…/kb/`, `kb_store.knowledge_base(project)`). The project's `mcp_kind` / `mcp_path` / `kb_published_at` read through to it.
- `mcp` — a **remote MCP server** the workspace shares: its URL and the `claude mcp add` line. canopy neither proxies nor vets it; `GET /api/mcp-servers/` lists it beside the knowledge-base servers.
- `skill` — a Claude Code **skill**: its `instructions` (its SKILL.md body), written on the project page or published by the lead agent; `url` is where it came from, optional when the instructions are there.
- `plugin` — a Claude Code **plugin** (a repository URL), listed with the line that adds it.

`…/workspace-projects/<ws>/<handle>/services/` lists, adds, changes and removes them (a knowledge base is not removed there). Design record: the spec's "What a project serves to the workspace" and "Opening a project".

## A project's toolbox reaches a session only when the work asks

**One connection serves every project, with the same few tools however many projects exist.** Never wire a project's own server into an agent's plugin or every session's config: each one adds its tools to every session that agent runs. The **projects connection** at `/api/mcp/projects/` (`apps/mcp/project_gateway.py`; the canopy plugin declares it as `projects`) works in three steps:

1. **Find** — `projects`: one line per project the caller may open that carries something.
2. **Open** — `open_project("<ws>/<handle>")`: the project's card — what its knowledge base holds (its `kb_info`), its skills by name, its shared servers and plugins with their add lines.
3. **Use** — the knowledge-base tools `kb_search`, `kb_read`, `kb_entity`, … `kb_table` (any table by name), `kb_original`, each taking the `project` it reads; `project_skill(project, skill)` for a skill's instructions.

Every call names its project and nothing is remembered between calls: under MCP 2026-07-28, which Claude Code speaks, a connection has no session to keep state in. A server cannot add one session's tools (`tools/list_changed` reaches a client, but the next `tools/list` arrives as a stranger), so the tool list stays fixed.

canopy serves what it holds (a knowledge base, a skill's instructions). It does not proxy a shared remote MCP server, since it holds no third-party credentials, and it does not load a plugin, since plugins load when Claude Code starts: those are listed with their add lines. A board turn on a task whose project carries something has `open_project("<address>")` in its brief (`apps/harness/dispatch.py::_task_context`). A person asks their Claude to "open <ws>/<handle>"; the project page's Toolbox section says how, with the one connection line for anyone without the canopy plugin. A project's own server (`/api/mcp/projects/<ws>/<handle>/`) still serves one project, for a client that should always have it.
