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

Do not let a workspace declare what acts beyond the caller: overlay `add_action` refuses
`then` and link actions (`overlay.py`, `add_action` branch); only a template declares them.

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
   version; overlays only add, so it reads today's records.

A template update is taken as a draft (`upgrade=true`), then the same steps.

## Reused, not rebuilt

- **Access**: every route declares `@gate`.
- **Pages**: the dynamic-view sandbox (`apps/dynamic_views/frame.py`: opaque origin,
  `connect-src 'none'`, no `allow-forms`), compiler, kit and protocol. An app page gets an
  `app` prop; a plain view still refuses writes.
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
- **Test copies** are separate spaces of records; nothing in one reaches live or sends email.
- An **app author or agent** cannot run server code, write access rules, or read what its
  viewers could not; page code can see what its page shows.
