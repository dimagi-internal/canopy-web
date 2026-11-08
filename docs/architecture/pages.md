# Pages: every SPA/server page, what it is for, and how it behaves

Every browser-facing address canopy-web serves. The SPA route table is
`frontend/src/router.tsx` (`routeTable`); server-side pages are in
`config/urls.py`. What each page is for is its descriptor in
`frontend/src/guide/surfaces.ts`; what it shows is its component. API routes are
in [`api-surface.md`](api-surface.md). This doc holds only the rules every page
shares.

## Rules for every page

- **Tenant data lives under `/w/:workspace/`.** Personal/global surfaces, the
  pre-tenant pages and redirects stay at root. Every route outside
  `/w/:workspace` carries ITS REASON — a page's `flat` in
  `frontend/src/guide/surfaces.ts`, a redirect's entry in `guide/coverage.ts`
  `NOT_PAGES` — checked by `frontend/src/routeTenancy.test.ts`; a flat route
  holding tenant data fails it.
- **One URL per artifact, nothing forwarded.** The flat `/walkthrough/…`,
  `/review/…`, `/share/…`, `/storyboard/…`, `/narrative/…`, `/ddd-release/…`
  and the pre-tenancy `/w/<uuid>/…` are a plain server 404, reachable signed
  out, saying links now carry the workspace (`config/views.py::flat_artifact_gone`, matched by
  `FLAT_ARTIFACT_PATH` before the SPA catch-all). Build links with
  `scoped_path` / `scoped_path_or_none` (`apps/workspaces/links.py`) or
  `scopedPath` / `useScopedPath` (`frontend/src/lib/scopedLinks.ts`; a hand-built
  `/w/${…}` fails `frontend/src/scopedPathLiterals.test.ts`); neither has a flat
  fallback — a row with no workspace has no address (`Unhomed`). Every URL
  field the API returns is under `/w/<ws>/`
  (`tests/test_api_urls_are_workspace_scoped.py`). `manage.py
  rewrite_flat_links` (`apps/walkthroughs/flat_links.py`) rewrites a stored
  flat link to its scoped address.
- **Anonymous visitors reach only the scoped viewers** (`_SCOPED_VIEWER` in
  `apps/common/middleware.py`, listed in
  [pages-public-and-personal.md](pages-public-and-personal.md#scoped-public-viewers-anonymous-with-a-token)),
  the plugin catalog (self-enforced 401; the old per-workspace URLs answer 410), and the
  flat-artifact 404. Every other `/w/` path is the authed tenant shell.
- **One registry of pages: `frontend/src/guide/surfaces.ts`.** Every route is
  a descriptor there or a non-page (redirect, catch-all) in `guide/coverage.ts`
  `NOT_PAGES` with its reason; `guide/coverage.test.ts` fails on either missing.
  A descriptor says where the page lives — `home` (the page it is reached from:
  the guide files it under that page's menu, and the workspace switcher lands
  there when its section has no page), `group` (the guide heading for a page in
  no menu), `flat` (why it is outside `/w/:workspace`). The guide's grouping,
  the switcher's fallback (`nav.ts` `LIST_FOR`) and the tenancy test read these
  fields; the menus themselves are `NAV`, `HELP_ITEMS` and `ACCOUNT_ITEMS` in
  `components/AppLayout/nav.ts`, and a page in neither a menu nor a group fails
  `guide/grouping.test.ts`.
- **Adding a page:** add it to `routeTable` and give it a descriptor (with
  `flat` if it is not under `/w/:workspace`, and `home` or `group` if no menu
  links it). Every route gets an error boundary
  automatically (`guarded()` in `router.tsx`); a route needing a different one
  sets its own `errorElement`. A label or control you name in a descriptor
  goes in double quotes (“Keep a copy in canopy”), and `guide/labels.test.tsx`
  fails unless the page's source contains it, so new descriptors are covered.
- **One editor per object.** A core object people configure — an agent, a
  workspace's settings, a runner, a vault — has ONE page at ONE address, built
  from the shared kit in `frontend/src/components/objectPage/`: `ObjectPage`
  (name, status, one line, actions), `ObjectTabs` (the one routed tab row; a tab
  that needs attention carries a dot) and `Section`/`Setting`
  (`settingsLayout.tsx`). Each tab is a route (`…/:tab`), so a deep link names a
  place and a tab loads only its own data. Every other surface LINKS to that page
  (`runnerHref`, `vaultHref` in `lib/scopedLinks.ts`; `alerts.runner_credential.runner_path`
  server-side) or embeds one of its read-only pieces (the fleet map shows
  `RunnerFacts`) — it never rebuilds the object's controls. An object page in
  the agent rail (Agent settings) renders `ObjectTabs` inside the rail's section.
- **A moved page redirects, keeping query and hash** where a link may carry
  one (`SettingsRedirect`, `KeepQueryRedirect`, `CredentialsRedirect` in
  `router.tsx`). Flat ARTIFACT addresses do not redirect — see above.
- A header workspace switcher appears when you belong to more than one
  workspace. A user with no workspace sees `FirstRunPage` instead of a blank
  redirect.

## The pages, by area

- [pages-workspace-work.md](pages-workspace-work.md) — tenant pages for work: header, tasks, projects, views, apps, approvals.
- [pages-workspace-agents.md](pages-workspace-agents.md) — tenant pages for agents: the table, creation and the agent workspace.
- [pages-workspace-settings-and-ops.md](pages-workspace-settings-and-ops.md) — workspace settings, sessions and chat, health and the gated ops pages.
- [pages-public-and-personal.md](pages-public-and-personal.md) — anonymous token-scoped viewers and the root (personal, global, pre-tenant) pages.

## Server pages (not the SPA)

The list is `config/urls.py`. The SPA catch-all (`spa_view`) excludes `api/`,
`admin/`, `accounts/`, `health/`, `static/`, `auth/`, `assets/`, `oauth/` and
`.well-known/`; `assets/` is deliberately NOT served by the catch-all, because a
missing hashed bundle must 404, not return `index.html` as `200 text/html`.
Related docs: OAuth/HCP endpoints [`mcp-surface.md`](mcp-surface.md),
[`hcp-service.md`](hcp-service.md); the framable `/embed/chat` page
[`embedding-a-canopy-agent.md`](embedding-a-canopy-agent.md);
`/plugins/marketplace.json` (bearer canopy token, a 401 and never a sign-in
redirect, `apps/plugins`) [`plugin-marketplace.md`](plugin-marketplace.md).
