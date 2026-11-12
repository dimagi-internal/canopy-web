# Work: tasks and projects

Tasks and projects belong to a **workspace**, not to an agent. Every route: `api-agent-projects-and-tasks.md`. Models and their field-level rules: `apps/agents/models/work.py` (read the class docstrings first). Writers: `apps/agents/services/{tasks,projects,task_actions}.py` and `apps/agents/work.py`.

## The nouns

- **Task** (`AgentTask`). Named by its **key**, `<PREFIX>-<number>` (`DIM-142`): the workspace's prefix (`TaskSettings.prefix`, unique across canopy, frozen once a task carries it) and a counter that never goes back. A task that moves to another workspace (its project moved) takes a new key there; its old key still finds it (`work.rehome`).
- **Assignee**: a person (`assignee_user`) or an agent (`agent`), never both (CHECK `task_one_assignee`), or nobody. An agent assignee is who approval starts a turn for; a person assignee is who "My tasks" lists it for.
- **Agent alias** (`ext_id`, `T12` / `P5`): the name the agent that filed it (`created_by_agent`) gives it. Unique per that agent and kept through reassignment.
- **Project** (`AgentProject`). Addressed `<workspace>/<handle>`, its one page; the agent alias link only forwards there. Led by a person (`owner_user`) and/or a **lead agent** (`agent`), worked on by **member agents** (the lead agent is always one). Any agent of the workspace, or one above it, may file tasks into any project.
- **Recurring task** (`AgentTask.recurrence`): a TEMPLATE with a rhythm (`day`, `week`, `2weeks`, `month`, `as_needed`; an optional `weekday`; `auto`). It stays in backlog and never completes; each period's work is an **instance**, an ordinary task whose `template` names it (not `parent`: subtasks roll up progress and nest at most `MAX_TASK_DEPTH`). `last_done` is derived (the newest done instance's completion), never stored.
- **Comment**: words on a task anyone who reads it may add (`comment` action, `TaskEvent.COMMENT`). Not a `reply`: it answers no ask and never starts or wakes a turn.
- **Statuses** are fixed values, each in one Linear category; code reads the category (`AgentTask.CATEGORY`, `AgentProject.CATEGORY`), never the raw status. A workspace renames how a category is shown (`TaskSettings.status_names`).

## Rules

- **One writer per object.** Tasks: `services.tasks.create_tasks` / `patch_task`, `task_actions.act`, and `recurrence.spawn` for a template's instances. Projects: `services.projects.new_project` (and `create_project` over it) / `patch_project` / `promote_project` / `set_steward` / `set_member_agents` / `share_project` / `unshare_project`. The models fill what a row may not lack on save (workspace, key, handle), so a test or a migration that builds a row directly still gets a whole one.
- **Every change is history.** `work.record_created`, `record_changes`, `record_action` and `record_comment` write `TaskEvent`s from every writer. `AgentTaskAction` is the assignee agent's queue (`pending` until it drains them).
- **Approve means start now.** On an agent's task it starts that agent's turn (`on_approve`, or one written from the card; `task_actions.act`); on a person's it moves the task to in progress and starts nothing. `accept` (to todo) and `backlog` decide without starting. A proposed project starts only through its decision card: `projects.patch_project` refuses a PATCH out of `proposed` with 409.
- **An agent proposes, a person decides.** An agent's project update is always a draft; a person publishes it, choosing the health. The one exception is per project and a workspace admin's (`projects.decide`): with `lead_agent_may_publish` on, the project's LEAD agent may publish its own drafts (recorded in `published_by_agent`); a member agent, or the lead on another's draft, still may not. Only a person grants it (never an agent's login, `api_tasks.py` update-settings), and it covers the lead the admin saw: handing the lead seat on, or moving the project to another workspace, turns it off. The rule is `work.agent_publish_refusal`, which `publish_update` applies to every agent publish.
- **Access** is `apps/agents/gates.py` (`task_in_reach`, `may_edit_task`, `project_in_reach`, `may_edit_project`, `project_ws_or_404`); do not re-derive it in a route. A view over many tasks reads the facts once (`proposals.task_cards`).
- **A workspace app may act on ONE project's tasks** (`apps/workspace_apps/tasks.py`): the actions its template declares, as the viewer, gated by the app role instead of `may_edit_task`, through the writers above, never starting a turn. Its rules: `workspace-apps.md` § Acting on one project's tasks.
- **An agent's route is the agent's view.** `/api/agents/{slug}/tasks|projects|actions/…` list what that agent is assigned, filed, leads or works on; they do not widen the workspace rules above.

## Recurring tasks

`services/recurrence.py` (its docstring has the period rules). **`spawn(template, due, by)` is the one writer of instances**: it copies title, notes, assignee, project, priority and labels (never relations), and is idempotent per (template, period) because the period is stamped on the instance (`template_period`, unique per template) when it is made, so moving an instance's `due` neither frees its period nor fills another. `as_needed`'s period is the date itself.

- **The tick** (`agents.recurrence`, `AgentsConfig.ready`, every 10 minutes) gives each `auto` template the current period's instance once that period has begun, in the workspace's `TaskSettings.timezone`. A template made part-way through a period starts at the next one; `as_needed` is never auto. A run that is late, or runs twice, still makes each period's instance once.
- **`2weeks`** alternates from the period of the template's first instance (a first one spawned by hand sets it), else from the week the template was made.
- **A personal project's template** spawns into that project, and only to an assignee who may still have work there (`_refuse_outsiders`); its instances are as private as any of its tasks.

## Sharing a project across its workspace tree

A project belongs to one workspace; its agents and readers come from that workspace, the ones above it, and its lead agent's. A **share** (`ProjectShare`, `services.projects.share_project`) opens it to one more workspace of the SAME tree and every workspace below that one. Nothing is ever shared across organisations.

- **The rule.** The shared workspace has the project's tree root, and is neither the project's workspace nor one above it (those reach it already): else 422. Moving a workspace under another parent ends the shares that would now straddle two trees (`prune_cross_tree_shares`); moving a project to another workspace drops the shares and the member agents that no longer fit (`promote_project`).
- **Members.** `may_join_workspace_project(agent, project)` = leading's rule (`may_hold_workspace_project`) OR the agent's workspace is a shared workspace or below one. It gates `set_member_agents`, a proposal's partner, a task's assignee in that project (`resolve_assignee(..., project)`) and `get_project` by address. **Leading** (`set_steward`, `new_project`) keeps the stricter rule: a share never lets the other side lead. A project's `member_agents` slugs are resolved by the PROJECT's tree (`gates.agent_in_project_tree`), not by the caller's reach.
- **Readers.** `project_reader_slugs(project)` is what the projects connection checks (`project_gateway._openable`); the by-address detail (`gates.project_ws_or_404`), a task by key (`gates.task_in_reach`) and `workspace_projects` agree with it. A shared reader does not get the project's board (`board_readable` stays false) or edit rights. Sharing needs an editor of the project's OWN workspace; a lead or working agent's login is not enough.
- **Moving an agent** (`move._project_memberships`): memberships in projects it could no longer join from its new home are dropped and reported in `stops_working`; the move is not blocked. Leading a project of another workspace still blocks it.

## Personal projects

`AgentProject.visibility` is `workspace` or `personal`. A personal project still lives in a workspace, but only its lead (`owner_user`) and the logins of its lead and member agents read it. A workspace admin or owner, an inherited role, an agent's admins, a share and a `ProjectParticipant` do not; to them it is the same 404 as a project that does not exist.

- **One rule, ANDed with the tenant scope, never instead of it.** `services.projects.project_read_q(user, prefix="")` on a query, `may_read_project(user, project)` on a row; a task, `task_read_q(user)`. No user means what anyone may read: the stored and shared figures (`refresh_agent_waiting`, an agent's `task_count`) count that. `tests/test_personal_project_never_leaks.py` walks every GET as a non-reader, and fails a new `AgentProject.objects` or board `AgentTask.objects` query that neither asks the rule nor says why not in its allowlist.
- **Making one** is a person's act; an agent's route or login makes none. **Switching** is its lead's alone; going personal is 409 while anything would reach past its readers (`_personal_blockers` names what).
- **Its people.** Only its lead changes its lead, its agents, its lead agent or its workspace. It always has a person as lead, and does not move to a workspace where they are not a member. Its tasks go only to its lead and its agents (`AssigneeError`).
- **Refused on one** (`services.projects.refuse_on_personal`, 422 to a reader): everything that would widen who sees it (shares, check-ins, session shares, vault attachments, views, linked artifacts, proposal partners, relations across its edge). A handle a caller may not read is not found, for filing or moving a task too.
- **Its turns.** A turn raised from a task in it (`Turn.raised_from_task`) has a log only its readers read (`harness/turn_access.py`); that is the log, not the conversation: the session a board turn ran in follows `canopy_sessions.access`. Runner requirements (`harness.sensitivity`) are not used for this: they would ground the turn on sensitive-data boxes.
- Known side channel: a handle is unique per workspace, so naming a taken one is 409 and a task key's number may skip, even when the holder is a personal project.

## A project's people

A project has member agents (`member_agents`) and member **people** (`apps/agents/project_members.py`): its lead (`owner_user`, role `owner`, no row) and those added by name (`ProjectMember`). The roles, the highest-role-wins merge and the rule that an agent's login is never a person are the vault's own (`member_roles.py`). `participants` is different: who canopy has seen take part, filled in automatically, granting nothing.

- **What it gives:** you may change the project, it is among `GET /api/me/projects/`, and you are a member of every vault attached to it (`vault.md` § Workspace vaults). It does not widen who may read the project. A personal project has only its lead.
- **Who may be on it:** someone in a workspace that can read it (`project_reader_slugs`), else 422. Leaving that workspace or a share ending ends it; the row stays and counts again if they return.
- **Who may change the list** is `project_members.may_manage`: its lead, an admin on it, or an owner of its workspace. Not an editor, and never an agent's login: this list opens its vaults. A browser needs a passkey step-up; a token is allowed.

## What a project serves

A project can serve things to its workspace as `ProjectService` rows, one per thing, by `kind` (`kb`, `mcp`, `skill`, `plugin`; the `ProjectService` docstring says what each is). canopy serves a `kb` (as the project's own MCP server) and a `skill`'s instructions; it neither proxies nor vets an `mcp` server and does not load a `plugin`: those are listed with their add lines.

A skill that needs a secret uses the project's **vault**: a workspace vault attached to the project (`VaultAttachment.project`) opens to the project's people and is read in the turns of every member agent in the vault's workspace tree, with no per-agent attachment (`vault.md` § Workspace vaults). The project's detail lists the vaults by name, the secret names and who they serve, never a value.

## A project's toolbox reaches a session only when the work asks

**One connection serves every project, with the same few tools however many projects exist.** Never wire a project's own server into an agent's plugin or every session's config: each one adds its tools to every session that agent runs. The **projects connection** at `/api/mcp/projects/` (`apps/mcp/project_gateway.py`, whose docstring has the find / open / use steps; the canopy plugin declares it as `projects`) keeps the tool list fixed and every call names its project, because under MCP 2026-07-28 a connection has no session to keep state in and a server cannot add one session's tools. A board turn on a task whose project carries something has `open_project("<address>")` in its brief (`apps/harness/dispatch.py::_task_context`). A project's own server (`/api/mcp/projects/<ws>/<handle>/`) still serves one project, for a client that should always have it.
