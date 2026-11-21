# Flexible tasks, and an agent confined to one app

Status: design, for Jonathan's approval (2026-11-09). Asked for in a Bailey chat: "improve projects and
tasks so they are flexible to handle everything we need", and "build in the cut features needed so that
Addy's turn context and session context is only aware of the app".

## Why

The Jackson household's assistant (Addy) needs a phone checklist of her week's chores, a shared chore
tracker the family edits, a page where Jon and Gina pick this week's extras, and a way to ask Bailey
things from inside that app and nowhere else. Bailey built it as a workspace app with its OWN records
(`household` in marshellis: `chore`, `item`, `week_note`) because canopy tasks could not do the job:

1. **A page can read tasks but never change them.** A dynamic view and an app page are read-only on
   canopy's catalog; a check-off from a custom page would cost an agent turn per tap.
2. **No recurrence.** Weekly / every-two-weeks / monthly / as-needed chores have no home; an agent has to
   copy them by hand each week.
3. **No comments.** "Couldn't do it: the car wash was closed" has nowhere to go but `notes`.
4. **No way to let a person into one app only.** A workspace viewer sees every project and task.
5. **No way to confine an agent's conversation to one app.** Confinement exists for read mode and for
   external host sites (`docs/architecture/host-sites.md`), not for canopy's own app pages.

That duplicated canopy's work model (board, assignees, history, notifications, the fleet's task tracker)
inside one app. This design closes the five gaps in canopy itself, generally, so the household app and
every later app (a family trip planner, a club's rota, ACE's field checklists) use real tasks.

## Goals

- An app page can act on the tasks of ONE declared project, as the viewer, through the existing task
  services, gates and history.
- Recurring tasks are native: a template task with a rhythm; canopy makes each period's instance.
- Tasks have comments.
- A person can be given an app (and only that app) in a workspace.
- An agent can have a profile bound to one app; a person given only that profile reaches the agent
  only there, with only that app's tools.

## Non-goals

- Payroll. Natalia runs it from a spreadsheet; a human in the loop on purpose.
- A general app SDK, cross-workspace task access, or apps writing anything but tasks of their declared
  project and their own collections.
- Replacing the board UI. The board shows recurring templates and instances like any task.

## Anti-goals (fails even if it works)

- Any path that widens what a person can read or change beyond what an admin explicitly granted.
- An app that can touch a project it did not declare, or a declaration a workspace overlay can widen.
- A confined conversation that can reach the agent's email, Drive, memory, other conversations, or any
  canopy tool not on its list.

## Design

### 1. Recurring tasks (`apps/agents`)

- `AgentTask.recurrence` (JSON, null = not recurring): `{"every": "day"|"week"|"2weeks"|"month"|"as_needed",
  "weekday": "mon".."sun" (optional), "auto": bool}`. A task with a recurrence is a **template**; it lives
  in `backlog` and never completes.
- An **instance** is an ordinary task with a new `template` FK to its template (NOT `parent`: `parent`
  is a subtask with progress roll-up and `MAX_TASK_DEPTH`, and a weekly template would collect hundreds
  of children). Its `due` set to its date, title and
  notes copied, assignee copied. `TaskRelation`s are not copied.
- `services.recurrence.spawn(template, due, by)` is the ONE writer of instances; idempotent per
  (template, period): a second spawn for the same period returns the existing one.
- A tick (`apps/common/ticks.py`) spawns `auto` templates at the start of each period (Monday for weeks,
  the 1st for months, every day for `day`), in the project workspace's timezone (`TaskSettings.timezone`,
  new, default UTC). `as_needed` never auto-spawns; it is spawned by hand ("add to this week").
- `last_done` is derived: the newest `done` instance's completion time. No stored copy.
- API: `recurrence` on task create/patch; `POST /api/tasks/{key}/spawn {due}`; list filter
  `recurring=true|false`, `template=<key>`. MCP tools follow the REST routes as usual.

### 2. Task comments

- `TaskEvent.kind` gains `comment` (`body` text, author user or agent). Written by
  `work.record_comment` through a `comment` task action, member tier, on any task the caller can read.
  It is NOT `reply`: a comment answers no ask and never wakes or starts an agent. Shown in the task's history; returned by `GET /api/tasks/{key}/` and listable.

### 3. App pages act on one project's tasks (`apps/workspace_apps`)

- Manifest gains `tasks`:
  `{"project": "<handle>", "read": "<role>", "actions": {"<name>": {"role": "<role>", "do": "create"|"done"|"reopen"|"comment"|"spawn"|"patch", "fields": [...]}}}`.
  Template-only (like `then`): a workspace overlay cannot add or widen it. Validated in `manifest.py`.
- Page API: `app.tasks.query({status, due_after, due_before, parent, recurring, limit})` and
  `app.tasks.act(name, {key?, input})`. Routes: `GET/POST /api/workspace-apps/{ws}/{key}/tasks…`.
- **The project is bound at install**, not named in the template: the manifest names a project
  *setting* (`tasks.project_setting`), which only an `integrations` admin sets, and `template_diff`
  / `overlay.is_risky` flag any change to `tasks` or that setting as needing an admin.
- **App task actions never start a turn and never move a task.** `patch` may declare only `title`,
  `notes`, `due`, `priority`, `labels`; never `agent`, `assignee_user`, `status`, `project`, `parent`,
  `template`, `on_approve`. Status changes are the `done` / `reopen` actions only. `create` files into the
  declared project with no agent assignee.
- **Who may:** the viewer must hold the declaration's app role. The app is installed by an admin
  (`integrations`), so its declaration is the admin's consent: the action runs **as the viewer**
  (history says so) through `services.tasks` / `task_actions`, but the workspace-role gates
  (`may_edit_task`, editor-to-create) are replaced, for exactly that project and those actions and
  fields, by the app role. Everything outside the declared project is a 404.
- Reads go through `task_read_q` AND the project filter, so a personal project stays personal.

### 4. Scoped members (`apps/workspaces`)

Jonathan, 2026-11-12: Addy "should have an explicit relationship to the workspace but scoped the right way".

- `WorkspaceMembership.role` gains `guest`, with `WorkspaceMembership.scope` (JSON) naming what it opens:
  `{"apps": {"household": "assistant"}}` (app key -> app role). She is listed in the workspace's people as
  "Guest: Household (assistant)", is invited and removed like any member, and the relationship is
  explicit and auditable.
- **Fail-safe by default.** `member_role(user, ws)` and `is_member` return None / False for a guest unless
  the caller passes `include_guests=True`. So the ~30 existing membership checks, access-list lines that
  match "everyone in the workspace" (`grants.matches`, `role_meets(role, "")`), mail routes and every
  workspace read keep treating her as outside, unchanged. Only code written for guests opts in:
  `workspace_apps.access.roles_for` (her scoped app role, without READ), the app page routes, section 3's
  task routes, the people list, and the shell (which shows her only her apps).
- `permissions.MINIMUM_ROLE` never reaches `guest`; `ROLE_RANK` puts it below viewer.
- Invite: `create_invite(role="guest", scope={...})`, only by someone who may manage members AND the app.
- Audit test, like `test_personal_project_never_leaks.py`: every GET and every access-list / mail-route
  decision as a guest; any workspace data or any match outside her scope fails it.

### 5. An agent confined to one app (`apps/agents`, `apps/harness`, `apps/mcp`, frontend)

- A profile entry may name an app instead of a site: `{"apps": ["household"], "tools": [...]}`,
  one app per entry. `published_profiles.parse` accepts it; the entry key is what an access line opens
  (`use:<key>`), exactly like a site entry.
- **Its tools are fixed by canopy, not listed by the owner:** `who_is_asking`, `current_page`, the
  page's own actions (`page_*`), and new app-scoped MCP tools `app_query`, `app_act`, `app_tasks_query`,
  `app_tasks_act` whose app is resolved from the conversation, never an argument (like the site gateway).
  The owner may only narrow (`ceiling`). No Bash, no Read outside scratch, no other canopy tool.
- **The conversation's app is server-stamped:** the chat panel on an app page creates the session with
  `metadata.workspace_app = "<ws>/<key>"`, set by the route from the page it was opened on, which the
  viewer must be able to open. Nothing in the body sets it.
- **Selection never grants.** The access list decides who reaches the agent at all; a guest is named by a
  `use:<key>` line. An app-stamped conversation SELECTS the app entry for anyone without `write`; it
  opens nothing for someone with no line.
- **Every channel, same confinement** (Jonathan, 2026-11-12: "she should be able to e-mail Bailey"). A
  person whose only line is `use:<key>` reaches the agent from the app's chat panel AND by email (and any
  channel the agent has), and every one of those turns runs in the app entry. The app is fixed by the
  ENTRY (one app per entry), not the conversation, so an email turn has it too. On email the entry also
  holds the thread-pinned read/reply the agent's email channel uses (`{thread_id}`), and nothing else of
  the agent's mail.
- **The confined turn writes as its asker, within the app only.** `app_act` / `app_tasks_act` run the
  app's DECLARED actions as the verified asker (her own app role decides), so "I did the trash" by email
  checks it off. This is a deliberate, narrow exception to "an agent turn's caller token never writes":
  only these two tools, only the entry's app, only a verified asker, and recorded "<agent> for <person>"
  in task history. No act-as-you approval: it is exactly what she could tap herself.
- **Chat panel on app pages:** the panel views have (`/w/<ws>/views/<slug>`) also on
  `/w/<ws>/apps/<key>/<page>`, offering the agents that have an entry for that app.
- The envelope's `access: confined`, `capability: <key>`, and a new `app: {workspace, key}` block tell
  the agent where it is. The agent's own repo adds a skill for the entry (Bailey: `household.ask`).

## Delivery (five PRs, in order)

1. Recurring tasks + comments (sections 1-2). Independent.
2. App task declarations (section 3). Uses 1's `spawn` and `comment`.
3. Guest members (section 4). Independent of 1-2; lands before anyone outside the family is invited.
4. App-confined agent entries + chat panel on app pages (section 5). After 2.
5. Bailey: move the household app to a project `marshellis/household` with recurring templates and
   instances; drop its own `item` / `week_note` collections (a new app key, `household2`, since a
   workspace's own app cannot be reinstalled; retire `household`); Bailey's `household.ask` skill.

Each PR: tests first, `docs/architecture/` updated in the same PR, no migration that rewrites existing
tasks.

## Left to the builder

Field and route names above are proposals; follow each app's conventions where they differ. Timezone
handling, tick cadence, the exact gate refactor for section 3, and how the panel lists agents.
