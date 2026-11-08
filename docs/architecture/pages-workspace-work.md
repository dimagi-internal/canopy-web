# Pages: workspace work (tasks, projects, views, apps, approvals)

The tenant-scoped (`/w/:workspace/…`) pages for work: header and navigation, tasks, triage, projects, views, apps, MCP, approvals and knowledge bases. Part of the [pages](pages.md) topic; the index there lists every part. What each page shows is stated by its component: find it by path in `frontend/src/router.tsx` and by purpose in `frontend/src/guide/surfaces.ts`.

## Cross-cutting rules

- **The header is `NAV` in `frontend/src/components/AppLayout/nav.ts`.** Every entry is scoped to the active workspace; a sub-menu is a `group` on its items. The active entry is the item with the longest matching href (`activeHref`), so Runners (`settings/topology/runners`) lights, not Settings. The workspace switcher lands on the same section in the other workspace, and a page about one object falls back to its list (`switchWorkspacePath`, `LIST_FOR`).
- **A runner has one page, `runners/:runnerId/:tab`** (`pages/runner/RunnerPage.tsx`). The Credentials tab exists only for a cloud runner the viewer administers (`runnerTabs`; the routes behind it 404 for anyone else). A link naming another workspace forwards to the runner's own.
- **A workspace vault has one page, `vaults/:vaultId`** (`pages/vault/VaultPage.tsx`), drawn by the shared `VaultView` (`components/vault/VaultView.tsx`, the same frame as an agent's vault). The Log section is for the vault's owners and admins (`managesVault` in `pages/vault/VaultTabs.tsx`). A value is sealed in the browser: the page lists names only.
- **One task list.** `components/work/TasksView.tsx` is the Tasks page, the agent rail's Tasks section (`pages/agents/AgentTasksSection.tsx`) and a project's task list (`pages/work/ProjectPage.tsx`), pinned by filter, so rows, peek and New task are the same code everywhere. The task page (`TaskDetailView`) is also the peek beside a list.
- **A project has one page, `/projects/:handle`**, for teammates and editors alike. `/projects/:agent/:ref` and `/agents/:agent/projects/:ref` redirect to it (`guide/coverage.ts` `NOT_PAGES`).
- **An idea is a task with `kind: "idea"`**, never inferred; every tally on every page counts `apps/agents/ideas.py::of_project`, which also states how an idea's stage and shipping work.
- **An approval page renders its preview on the server** from the request's parameters, with the requester's own words kept apart from it (`apps/approvals/api.py`, [`approvals.md`](approvals.md)).
- **Personal apps** are installed per person and computer, never by a workspace; a web page can install, configure and stop one but never start it ([`local-apps.md`](local-apps.md)). The review page (`pages/LocalAppReviewPage.tsx`) is for the submitter and reviewers; anyone else gets "Not found, or not yours."
- **Workspace apps** (`apps/workspace_apps`, [`workspace-apps.md`](workspace-apps.md)): anyone in the workspace reads; admins install, switch on/off and make public pages live.
- **A project's knowledge base page is read-only** (`pages/projects/KnowledgeBasePage.tsx`); the steward's writes are `apps/agents/api_kb.py`.
