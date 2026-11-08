# Pages: workspace work (tasks, projects, views, apps, approvals)

The tenant-scoped (`/w/:workspace/…`) pages for work: header and navigation, tasks, triage, projects, views, apps, MCP, approvals and knowledge bases. Part of the [pages](pages.md) topic; the index there lists every part.

## Tenant-scoped (`/w/:workspace/…`)

- **`runners/:runnerId/:tab`** — a runner's one page (`pages/runner/`): Overview
  (status, facts, alerts, health, readiness checks), Credentials (a cloud box's
  Claude logins, tokens and API keys and its vault — only for its administrators),
  Routing (workspaces it serves, agents it takes, a laptop's repos — add one, see runners.md § Adding a repo to a laptop — each agent's routing), Access
  (administrators, private), Settings (name, pause, sessions, flags, egress, runtime,
  retire). Reads `GET /api/harness/runners/{id}/`; a link naming another workspace
  forwards to the runner's own. `/supervisor?tab=runners&runner=<id>` (old mail and
  alerts) forwards here.
- **`vaults/:vaultId`** (`/:tab` kept for old links) — a workspace vault's one page
  (`pages/vault/`), drawn by the shared **`VaultView`**
  (`components/vault/VaultView.tsx`, the same frame as an agent's vault): a summary
  line (where it lives, secrets / people / machines, what needs you), then, for a
  vault with no key yet, its first step above everything else: make its key (who
  gets a copy now, and who only once they set up a vault key), then
  collapsible sections — **Secrets** (names only; a value is sealed in the browser),
  **Access** — one tab per kind, in a fixed order: People (each with why they can),
  Agents, Projects and Workspaces (what it is given to, each picked from a real list,
  with a confirm naming the secrets it gives) and Machines (keepers holding a copy,
  and runners whose own credentials live here) — and **Encryption key** (one sentence
  saying what it is, then its version, copies for whoever waits, and changing it),
  then, for its owners and admins, **Log** (`GET /api/vaults/{id}/log`: each change from
  the audit rows its write routes leave, and each read by an agent's turn or a machine,
  newest first). The old tab addresses open the section that tab became (`/log` too).
  Settings → Secrets lists the workspace's vaults and links here.

- **The header** (`frontend/src/components/AppLayout/nav.ts`): **Home** and
  **Apps** (workspace and personal apps and the store, `/w/:workspace/apps`) are links;
  **Agents** (All agents, Sessions, New agent, Huddles, Schedules, Turn log,
  Runners), **Projects** (All projects, Tasks, Triage, and a **Demos**
  sub-menu: Storyboards, Walkthroughs, DDD runs, Shareouts) and **Workspace**
  (Members, Health, Timeline, Views, Settings) are menus; a project's views and
  MCP server are on its own page, and the MCP list is linked from Projects;
  every menu entry is scoped to the active workspace; a sub-menu is a `group` on its
  items; Guide, Get started and
  System sit behind `?`; personal pages (account settings, `/people/me`,
  `/sessions`, `/supervisor`, `/ide`) in the account menu. The active entry is
  the longest matching href, so Runners (`settings/topology`) and not Settings
  lights on the topology pages.
- **`/w/:workspace`** → redirects to `/w/:workspace/agents`. Tasks and projects
  are the workspace's (`docs/architecture/work.md`).
- **Anywhere in the app**: **⌘K** opens the command palette (pages, projects,
  agents, tasks by title or key, New task / New project / My tasks;
  `components/work/CommandPalette.tsx`), and **?** lists every keyboard shortcut.
- **`/tasks`** — every task in the workspace as one dense table
  (`components/work/TasksView.tsx`, one `TaskTable` row everywhere): tabs My tasks
  (`assignee=me`), Waiting on me (`waiting=me`), Created by me, All; filters for
  category, assignee, project, label, priority, kind, due and text, group and
  sort, and layout Table / Board (by status, drag to change status or order) /
  Calendar (by due date) — all of it in the URL, and saveable as a view (yours,
  or shared by an editor). Status, priority, assignee and due are edited in the
  row. `X` or a checkbox selects; the bulk bar changes many at once. `J`/`K` move,
  `Space` opens the task beside the list (`?peek=<KEY>`), `C` makes a task.
- **`/tasks/:key`** — a task's one page (`TaskDetailView`; the peek is the same
  component beside a list): properties edited in place, what it asks you (Start
  now · Accept · Backlog · Snooze · Decline), the people's own words, the plan,
  subtasks, relations, the assigned agent's current turn, and its history with
  the reply box.
- **`/triage`** — what waits for a decision (`GET /api/triage/`): suggested tasks,
  ideas and proposed projects, oldest first; `A` accept, `S` start now, `B`
  backlog, `D` decline, `H` snooze, `M` duplicate on the focused row.
- **`/projects`** — every project in the workspace as a row (status, health or
  Update missing, lead, lead agent, agents, progress, open and waiting, target,
  updated; `GET /api/projects/`). Open by default; **New project** for editors.
- **`/projects/:handle`** — a project's ONE page, for teammates and editors alike
  (`pages/work/ProjectPage.tsx`): properties (status, health, lead, lead agent,
  agents, dates, priority), its latest update with Post update and drafts to
  publish (and, for an admin, whether its lead agent publishes its own),
  milestones with progress, the scope-versus-done graph, its tasks (the Tasks
  page's own list, `TasksView`, pinned to the project — and to a milestone when
  one is picked — so its rows, peek and New task are the same code), its
  dynamic views drawn in their sandboxes (the first three; hidden while there are
  none and the viewer cannot make one), its toolbox, and **Shared with** (`ProjectShares.tsx`: the other
  workspaces of its tree it is open to and the agents that joined through each; an
  editor adds one from the tree or stops sharing), then its proposal card, proposed tasks,
  ideas and people, check-ins and recent turns.
- **`/views`** (+ `/:id`) — the workspace's dynamic views, generated screens over
  its data, each drawn in a sandbox (`DynamicViewsPage.tsx`, `DynamicViewPage.tsx`;
  [`dynamic-views.md`](dynamic-views.md)). The list (`?project=<handle>` narrows,
  `?new=1` opens New view); one view, with History (restore) and, for editors, the
  code and sources beside a live preview of the draft (`?edit=1`).
- **`/apps`** — every app, on one page with two tabs drawn by the same lists
  (`components/apps/AppLists.tsx`): **Workspace apps**, and **Personal apps** (`?tab=personal`).
  Root `/apps` redirects to the active workspace's Personal apps tab: it's what the runner's menu bar
  opens.
- **`/apps`** on the Workspace apps tab (+ `/store?app=<key>`, `/:appKey/:page?`, `/:appKey/customize`) — workspace apps
  (`apps/workspace_apps`; [`workspace-apps.md`](workspace-apps.md)): the installed list, one app of
  canopy's catalog before it is installed (screenshots, roles, records, public pages), an installed
  app's pages drawn in the dynamic-view sandbox, and its draft-based customize page. Anyone in the
  workspace reads; admins install, switch on/off, make public pages live, remove.
- **`/apps?tab=personal`** — apps a person installs for themselves, not for a workspace
  ([`local-apps.md`](local-apps.md)), each badged **Unreleased**: all your copies (computer, version,
  last run, Stop, needs-acceptance) and the store. No switch: there is nothing for a workspace to turn on.
- **`/apps/personal/:key`** — one personal app, in the same layout as a store app
  (`components/apps/AppDetailLayout.tsx`): what it does, where it runs, what it reads and sends, whether
  anyone reviewed it, versions; Install, with a computer and settings drawn from its manifest; then
  Configure, Remove, Stop and accepting a changed promise. The agent picked decides which workspace its
  sessions land in. It never starts an app: that is the menu bar on that computer.
- **`/apps/personal/review/:id`** — a submitted version, for its reviewer and its author: what it says
  it does, what reading the code found, what changed in its promises, per-file diffs and, for a superuser,
  Revoke. Approving is on its approval page only. Anyone else gets "Not found, or not yours."
- **`/mcp`** — the MCP servers a person can add: every published workspace project serving
  its own tools, here and below, each with the line that adds it to Claude Code
  (`McpServersPage.tsx`; served through the one project gateway,
  [`work.md`](work.md), [`mcp-surface.md`](mcp-surface.md)). Nothing is added for the person.
- **`/approvals/:approvalId`** — one typed approval request (`apps/approvals`,
  [`approvals.md`](approvals.md)): what approving will run, rendered by the server from the
  request's parameters, the requester's own words kept apart; Approve or Decline here or from
  the Slack message, push or email that linked to it.
- **`/projects/:handle/kb`** — a knowledge base's sources and version log
  (`GET …/kb/sources`, `GET …/kb/versions`, `KnowledgeBasePage.tsx`), read-only:
  each version its steward published, its counts, size, publisher and which one is
  served. Anyone who may read the project sees it. Linked from a KB project's page.
- **`/projects/:agent/:ref`** and **`/agents/:agent/projects/:ref`** → redirect
  to the project's one page, `/projects/:handle` (`/people/:person` is the
  contributor page).
  **An idea is a task with `kind: "idea"`** — never inferred; `work` is the
  default — and it must carry a person's own words (at least one `human_why`,
  enforced on create and patch). The work toward it is its subtasks. Its stage
  is its status (suggested → proposed, in progress → started, done → shipped);
  subtasks finishing never ship it — its owner or its person marks it done (the
  person may, on `act_on_task`). Every tally on every page is a count over
  `ideas.of_project` (top-level idea tasks), so the views cannot disagree.
