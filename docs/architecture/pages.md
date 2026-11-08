# Pages: every SPA/server page, what it is for, and how it behaves

Every browser-facing address canopy-web serves. The SPA route table is
`frontend/src/router.tsx` (`routeTable`); server-side pages are in
`config/urls.py`. API routes are in [`api-surface.md`](api-surface.md).

## Rules for every page

- **Tenant data lives under `/w/:workspace/`.** Personal/global surfaces, the
  pre-tenant pages and redirects stay at root. Every route outside
  `/w/:workspace` carries ITS REASON — a page's `flat` in
  `frontend/src/guide/surfaces.ts`, a redirect's entry in `guide/coverage.ts`
  `NOT_PAGES` — checked by `frontend/src/routeTenancy.test.ts`; a flat route
  holding tenant data (a "FOLLOW-UP") fails it.
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
- **Anonymous visitors reach only the scoped viewers**: `/w/<ws>/{walkthrough,
  review,share,storyboard,narrative,ddd-release}/…` plus the walkthrough
  content stream (`apps/common/middleware.py::_SCOPED_VIEWER` — the trailing
  slash keeps the plural list pages `/walkthroughs`, `/storyboards` behind
  login), the plugin catalog (self-enforced 401; old per-workspace URLs 410), and the flat-artifact 404.
  Every other `/w/` path is the authed tenant shell. Each viewer passes `ws` so
  the API 404s a row from another workspace.
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
- **Retired pages redirect, keeping query and hash** where a link may carry
  one (`SettingsRedirect`, `KeepQueryRedirect`, `CredentialsRedirect`). Retired
  ARTIFACT addresses do not — see above.
- A header workspace switcher appears when you belong to more than one
  workspace. A user with no workspace sees `FirstRunPage` instead of a blank
  redirect.

## The pages, by area

- [pages-workspace-work.md](pages-workspace-work.md) — The tenant-scoped (`/w/:workspace/…`) pages for work: header and navigation, tasks, triage, projects, views, apps, MCP, approvals and knowledge bases.
- [pages-workspace-agents.md](pages-workspace-agents.md) — The tenant-scoped pages for agents: the agents table, creating an agent and the agent workspace with its sub-routes.
- [pages-workspace-settings-and-ops.md](pages-workspace-settings-and-ops.md) — The tenant-scoped pages for workspace settings, sessions and chat, huddles, threads, schedules, activity, runner invitations, members, sign-offs, health, analytics and network, shareouts, walkthroughs, storyboards and DDD.
- [pages-public-and-personal.md](pages-public-and-personal.md) — The anonymous token-scoped viewers, and the root pages: the public site, getting started, pairing, connections, act-as, home, supervisor, IDE, personal settings, people and invites.

## Server pages (not the SPA)

`config/urls.py`; the SPA catch-all (`spa_view`) excludes `api/`, `admin/`,
`accounts/`, `health/`, `static/`, `auth/`, `assets/`, `oauth/`, `.well-known/`.

- `/api/` — REST (Ninja); `/api/docs/` Scalar, `/api/redoc/` Redoc.
- `/admin/` — Django admin. `/accounts/` — allauth. `/health/` — health check.
- `/auth/cli/authorize/`, `/auth/github/{start,callback}/`,
  `/auth/slack/{install,callback,link}/` — browser legs of sign-in/connect flows.
- `/oauth/*`, `/.well-known/*`, `/api/hcp/oauth/*` — OAuth/HCP documents and
  endpoints ([`mcp-surface.md`](mcp-surface.md), [`hcp-service.md`](hcp-service.md),
  [host grant contract](../../sdk/python/README.md#host-grant-contract-v1)).
- `/embed/chat`, `/embed/widget.js` — the one framable page and the widget
  loader ([`embedding-a-canopy-agent.md`](embedding-a-canopy-agent.md)).
- `/w/<ws>/walkthrough/<id>/content`, `/w/<ws>/marketplace.json`,
  `/w/<ws>/plugins/…` (410) — see above.
- `/plugins/marketplace.json` — your agent catalog for Claude Code, marketplace
  `canopy-agents` (`apps/plugins`, bearer canopy token, 401 never a sign-in
  redirect). [`plugin-marketplace.md`](plugin-marketplace.md).
- `assets/` is deliberately NOT served by the SPA catch-all: a missing hashed
  bundle must 404, not return `index.html` as `200 text/html`.
