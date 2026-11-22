# Workspace apps

A **workspace app** is a self-contained application a workspace installs from canopy's
app store, switches on or off, customizes, and can serve publicly (a careers page). The
bundled one is **Hiring**, an applicant tracker (`apps/workspace_apps/bundled/hiring/`).
`workspace_apps` is a product app (it imports the dynamic-view sandbox; tier in
`ARCHITECTURE.md`). Start reading at `apps/workspace_apps/models.py` (module docstring),
`manifest.py`, `overlay.py`, `services.py` and `api.py`.

**An app is a manifest, never server code.** Installing or changing one, even one a
workspace wrote, runs nothing on the server. Customizations are **data over declared
anchors** (an overlay), never an edit of the vendor's artifact, so a template update
keeps arriving.

## Three layers (`models.py`)

| Layer | Model | Who changes it |
|---|---|---|
| Template: what an app is | `AppTemplate` + immutable `AppTemplateVersion` (a manifest). canopy's own are synced from `bundled/<key>/` (bump `version` to ship); a workspace's own app is a template it owns | canopy (PRs), or the workspace for its own apps |
| Installation: the app in one workspace | `AppInstallation`: a template + the live `AppVersion`. Never a copy of the template | admins (`integrations`) |
| Version: the workspace's changes | `AppVersion`: the template version it runs on + an **overlay** (keyed ops). Drafts, the published one, superseded ones; append-only | maintainers (`access.may_maintain`) |

What runs is `overlay.compose(template manifest, overlay)`, validated again by
`manifest.py`. A template update is the same overlay over the newer version. Who may do
what: `access.py` (manage = `integrations`, maintain = managers or a member granted the
app's `maintainer` role, grant = `members.manage`).

## Maintainers: who publishes new versions (`access.py`)

An admin (`integrations`) names who may publish new versions of ONE installed app without
an admin each time: a person, or an agent's own login, either way a member of the workspace
(`…/maintainers`; the grant is the `maintainer` `AppRoleGrant`, with a `level`). The level
is the admin's decision, made ahead of time:

- **`content`** publishes only versions that change nothing about who can see or do what.
- **`permissions`** may also publish ones that do: `connections`, `browser`, roles, who
  reads a collection or field, public and link surfaces, emails' recipients, retention,
  `tasks`. The admin pre-approved widening this one app.

The one judge of "changes who can see or do what" is `template_diff.changes(live manifest,
the manifest going live)` (with `overlay.is_risky` for a draft's own ops), on every way a
version goes live: a draft, a new version, a rollback (going back is judged like going
forward). A finding without `access.may_widen` is a 403 naming each one in plain words.
Every published version records what it changed (`AppVersion.reach_changes`), who published
it and under which right (`published_under`: `admin`, `maintainer:content`,
`maintainer:permissions`); grants, level changes and revocations are settings, audited as
`SettingChange` rows. Revoking ends the right at once; versions already live stay.

**A new version from a repo** (`POST …/versions`, `services.release`): for an app whose
template the workspace owns (installed from its own manifest), a maintainer or admin sends
the whole manifest, pages included, in the shape install takes. It is validated exactly as
an install is (`manifest.validate`, so `browser.py` and the connection declarations too),
the workspace's live overlay is carried onto it (refused if any op no longer fits), its
scenarios must pass, and it goes live as a new `AppTemplateVersion` plus a new `AppVersion`
(`origin` `release`), so rollback reaches every earlier one. This is how an app's
`connections` and `browser` change after install: overlays never reach them. Refused while a
draft is open, for a version string already used, and for canopy's own templates (theirs
arrive as updates). Nothing is kept when it is refused.

## The manifest (`manifest.py`)

Its module docstring lists what an app can declare: `roles` (each a **workspace
permission** via `permissions.can`, or grant-only; no workspace role is named, pinned by
`tests/test_roles_named_only_in_workspaces.py`), `collections`, `actions` (the ONLY
writes), `pages`, `settings`, `fixtures`, `scenarios`, `links`, `retention`, and the
guards on actions (`when` is a 409 on a record in the wrong state, `actor_field`,
`params`, `then`, `bulk`) and on fields (`read`: absent from queries, history, emails and
links). Record-level access is `scope.py`. An optional top-level `connections` declaration
(same shape as a dynamic view's, composed through overlays unchanged) lets member-only
pages call Salesforce through canopy-web; public pages never do. A shared agent connection
is approved per app on the agent's Accounts tab ([`vault-connections.md`](vault-connections.md) § Calls from screens).

An optional top-level `browser` declaration says which member pages the browser extension's
side panel shows beside which tabs, and what each may ask of the tab's adapter
([`browser-extension.md`](browser-extension.md)).

Do not let a workspace declare what acts beyond the caller: overlay `add_action` refuses
`then` and link actions (`overlay.py`, `add_action` branch); only a template declares them.

## Bundle pages (`apps/workspace_apps/bundles.py`)

A member page may be an existing web UI instead of JSX: `{"slug": "kanban", "kind": "bundle",
"entry": "kanban/kanban.html"}`, drawn from static files (HTML, JS, CSS, JSON, images, fonts)
uploaded with the version. The module docstring holds the rules; the ones to keep:

- **Upload is a plain list, `[{path, content_base64 | text | sha256}]`**, given as `files` with the
  manifest (`install_workspace_app`). Every route that releases a template version passes it through
  `bundles.attach`, which drops any `bundle` the author wrote and stores the server's own index
  (`{id, size, files}`); `manifest.py::_bundle_page` checks each `entry` is one of those files. A
  bundle over one request body goes first to `upload_workspace_app_bundle_blobs` in batches and is then
  named by `sha256`; a blob no bundle names is swept after a day (`bundles.sweep_unused_blobs`, on the
  retention tick). Limits are `bundles.MAX_*` and the type table `bundles.TYPES`.
- **Template-only.** A bundle page has no code, slots or sources; overlay `fork_page` and `fill_slot`
  refuse one and `add_page` cannot make one (`overlay.py::_not_bundle`). `set_page` (title, menu) works.
- **Content-addressed and immutable.** `AppBundleBlob` holds bytes once by sha256; `AppBundle` is the
  {path: sha256} mapping, its id the hash of it. Files are served at `/app-bundles/<id>/<path>` with a
  year's `immutable` caching (HTML excepted: each carries a fresh nonce).
- **The same sandbox, one step wider.** `bundles.BundleMiddleware` answers before sessions, like the
  dynamic-view frame, so no cookie is read or set; every file carries `bundles.csp`: `sandbox
  allow-scripts allow-forms` (opaque origin; `allow-forms` only so a form's submit event fires, while
  `form-action 'none'` keeps anything from being submitted), scripts only from the bundle's own path
  and canopy's client (no inline script but canopy's nonce'd config, no `eval`), `connect-src` only the
  bundle's own files (it may fetch its JSON or templates; data still goes only through `canopy.*`),
  images, fonts and media from the bundle or `data:`/`blob:`, no workers, frames or plugins, and the
  same `frame-ancestors` as the frame (canopy's origins plus the browser extension's), so a bundle page
  also renders in `/ext/panel/`. The host frames it with the same two flags
  (`DynamicViewFrame.tsx::BUNDLE_SANDBOX`), never `allow-same-origin`.
- **canopy's client is injected into every HTML file**, first in `<head>`: `bundle_client.js`, a classic
  script, so `window.canopy` exists before the page's own scripts run. It speaks the dynamic-view
  protocol (`frontend/src/dynamicViews/protocol.ts`), so the canopy page answers a bundle page exactly
  as it answers a JSX one: `canopy.app` (query, act, bulk, link, history, upload, tasks),
  `canopy.connection`, `canopy.browser`, `canopy.page`, `canopy.read`, `canopy.navigate`. Because its
  code runs before canopy has spoken, it adds `canopy.ready` (a promise of `{app, scope, browser}`).
- **Moving between pages** is `canopy.open(page, params, {newTab})`, by slug or by a bundle path
  (`"kanban/kanban.html?id=5"` opens the page whose `entry` that is, with `id` as a param); a click on a
  link to another file of the bundle does the same, any other link leaves through `canopy.navigate`.
  It is a full canopy page each time; the frame never navigates itself (a frame that loads a second
  document is no longer answered, as for any view). Page params also ride in the file's query string,
  for code that reads `location.search`. A new tab needs a click on the page.
- **What a bundle cannot do**, so a port plans its shim: no `localStorage`, `sessionStorage`,
  IndexedDB or cookies (an opaque origin has none: keep state in a collection through `app.act`); no
  network but its own files (outside calls go through `canopy.connection`); no remote scripts, fonts
  or images (vendor them, or turn a fetched image into a `blob:` URL); no `alert`/`confirm`/`prompt`
  (no `allow-modals`: they return without asking); no popups (`window.open` is routed to
  `canopy.open`/`canopy.navigate`); no workers; no inline `<script>` or `on*=` attributes; and no
  assignment to `location` (use `canopy.open`).

## Acting on one project's tasks (`apps/workspace_apps/tasks.py`)

An app may declare `tasks` (`manifest.py::_validate_tasks`): the app role that reads, and named
actions, each one `do` (create, done, reopen, comment, spawn, patch) by one role over a fixed field
allowlist (`TASK_FIELDS`: title, notes, due, priority, labels; a create also recurrence and
`assignee_email`). Never agent, assignee, status, project, parent, template or `on_approve`.

- **The project is bound, not named.** The template names a text setting (`project_setting`);
  its value is a project handle in the app's workspace. `tasks` is template-only (an overlay op
  carrying it is refused, one that changes it is quarantined), and rebinding the setting is risky
  (`overlay.is_risky`), as is any template update to either (`template_diff.changes`): an admin's.
- **The app role replaces the workspace task gates, for exactly that.** The admin's install is the
  consent: a holder of the action's role runs it AS THEMSELVES through the task writers, recorded
  under their name. Everything outside the bound project or the declared actions is a 404; reads are
  `task_read_q` AND the project, so a personal project stays its lead's.
- **Never a turn.** Done and reopen go through `task_actions.finish_or_reopen` (shared with a
  dynamic view's task actions), comment through `task_actions.act`; reopen and patch are field
  writes; create files with no agent assignee (a person assignee must hold the `read` role); spawn
  copies a template. Status moves only through done and reopen.
- **Live work only.** A draft's test copy has no tasks: the routes refuse `?draft` (409), the page
  host refuses there too, and a scenario names only the app's own actions.
- **An agent for a person.** `tasks.act_task(..., on_behalf_by_agent=)` runs as the asker's own app
  role and records "<agent> for <person>" (`TaskEvent.by_user` and `by_agent`). Its caller verifies
  the asker.

Pages call `app.tasks.query(filters)` and `app.tasks.act(name, {key, input})`; `app.tasks.declared`
says what exists (the data model's `_tasks`). A row is `tasks.task_out`; a query's rows also carry
`done_by` (who last moved a done task to done, a person named over the agent acting for them) and
`comments` (the newest `ROW_COMMENTS`, oldest first), read for the whole page in two queries
(`tasks.annotate_activity`). An action's response leaves them null / empty.

## The overlay (`overlay.py`)

The module docstring holds the rules. The ones to keep: the workspace **adds** only in
its own namespace (`x_` fields/collections/actions/roles, `x-` pages) so a template
addition can never collide; it changes a template element only through narrow openings
(never un-require; options only into lists the template marks `extensible`); `fork_page`
is a fork and is shown as one; ops apply one at a time, validated after each, and one whose
anchor is gone is **quarantined** with its reason, never dropped, never breaking the app.
Template pages draw what the workspace adds without knowing it (`FieldInputs` /
`FieldValues` in `frontend/src/dynamicViews/frame/kit.tsx`, and `Slot`).

## The life cycle (`services.py`, routes in `api.py`)

1. **Draft** starts as the live app with a **test copy** seeded from `fixtures`: never
   live data, nothing is emailed, the maintainer holds every app role.
2. **Change** into the same draft by an agent the maintainer lets onto THIS draft
   (`AppVersion.helpers`: read the guide, add/remove ops, open the test copy, run checks;
   never publish, discard, roll back or start a draft; publishing or discarding ends
   the access), by the person's own MCP client (`get_workspace_app_guide` →
   `start_workspace_app_draft` → `add_workspace_app_change` → `check_workspace_app_draft`,
   under the person's own rights), or by the page's own forms.
3. **Check**: the scenarios run on a scratch copy; the workspace can add its own.
4. **Publish** (`services.publish`) is refused while anything is quarantined or a check
   fails. A change to what the public sees or who can do what needs a workspace admin:
   the workspace's own changes judged op by op (`overlay.is_risky`), a template UPDATE by
   comparing the two template versions (`template_diff.changes`).
5. **Restore** (`services.rollback`): an earlier version becomes live again as a new
   version; overlays only add, so it reads today's records. Judged like a publish.

A template update is taken as a draft (`upgrade=true`), then the same steps.

## Reused, not rebuilt

- **Access**: every route declares `@gate`.
- **Pages**: the dynamic-view sandbox (`apps/dynamic_views/frame.py`: opaque origin,
  `connect-src 'none'`, no `allow-forms`), compiler, kit and protocol. An app page gets an
  `app` prop; a plain view still refuses writes. A bundle page is the same protocol from
  its own files (§ Bundle pages).
- **Seeing a page as an agent**: an agent QAs an app page it builds by opening
  `/w/<ws>/apps/<key>/<page>` in a headless browser that sends `Authorization: Bearer
  <its own PAT>` on every request (Playwright `extraHTTPHeaders`, gstack `browse` headers);
  never a token in the URL, which signs nobody in. It sees exactly what its login reads
  through the REST routes (its app roles, guest scope and gates). The sandbox frame's
  scripts are then preflighted cross-origin requests (opaque origin plus the header);
  `config/static_middleware.py::asset_preflight` answers that preflight for `/assets/`
  only, which are public files, so it grants nothing.
- **Public pages**: scoped path `/w/<ws>/app/<key>/`, `auth=None` routes that 404 alike
  when the app is off or not public, a honeypot (`api.py`), a DB-counted per-address
  limit (`services._rate_limit`). Files use the chat-attachment bucket.
- **Agents**: changes go through canopy's own chat and MCP tools; canopy-web holds no
  model key.
- **Email**: sent only when `WORKSPACE_APPS_SEND_EMAIL` is on (off by default); scheduled
  and retention ticks are registered in `apps.py`.

## Security, said plainly

- A **visitor** reads only public projections of enabled, public apps, creates only
  through public actions (declared, typed, size-limited inputs; refs only to allowed
  records; files only for a public form's file field; rate-limited) and gets back `{id}`.
- A **guest** of the workspace (`access.md` § Guests) holds, in the app their scope names, exactly that app
  role plus `public` (`access.roles_for`), and reaches only the routes that use an app (`_installation(guest_ok=True)`:
  read it, query, act, history, files, the declared project's tasks), never its drafts, setup, grants or any other
  app; everything else is the stranger's 404.
- **An agent in the app.** A live member page is the page canopy's panel is on (`AppFrame` → `pagePanel`): the panel
  offers the agents with an entry for the app and holds the conversations it starts in it; such an agent reaches the
  app only through `app_query` / `app_act` / `app_tasks_query` / `app_tasks_act`, as the person asking
  (`agent_host.py`, [host-sites.md](host-sites.md) § App entries). A guest scope may name only a role the app declares (never `maintainer`, `member`, `public`),
  given by someone who may maintain the app (`access.validate_guest_apps`).
- **Test copies** are separate spaces of records; nothing in one reaches live or sends email.
- An **app author or agent** cannot run server code, write access rules, or read what its
  viewers could not; page code can see what its page shows.
