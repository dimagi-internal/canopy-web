"""What an agent needs to change an installed app for a person, cold.

Served by `GET /api/workspace-apps/{ws}/{key}/guide` (`get_workspace_app_guide`)
with the app's current composed manifest. Keep it in step with `overlay.py`:
every op named here is one `_apply` handles, with the same rules.
"""
from __future__ import annotations

CONTRACT = """\
You are changing an installed canopy app for a person who is not a developer. You never edit the
app's template. You add CHANGES to the app's open DRAFT; the person tests the draft on a sample-data
copy and publishes it. Each change is one operation, POSTed to
/api/workspace-apps/{workspace}/{key}/draft/ops (tool `add_workspace_app_change`). A refused change
comes back as a 422 naming exactly why — fix it and try again. Put the person's own words in
`intent` on every change: it is how the change is explained to them, and how it is carried forward
when the template is updated.

FIRST: if there is no open draft, start one (POST …/draft, tool `start_workspace_app_draft`).

OPERATIONS (the workspace's own things are named x_… — fields, collections, actions, roles — and
pages x-…, so a template update can never collide with them)
  {"op":"add_field","collection":"application","key":"x_visa","field":{"type":"bool","label":"Needs visa sponsorship"}}
      types: text, longtext, email, url, number, bool, enum (options), date, ref (collection), file (accept, max_mb)
      Added fields appear on the app's forms and record pages automatically when the action that
      writes them takes them as input (see set_action add_input).
  {"op":"set_action","action":"apply","add_input":["x_visa"]}    let a form collect your own field
  {"op":"set_action","action":"add_candidate","enabled":false}   switch an action off
  {"op":"set_field","collection":"job","field":"department","label":"Team","help":"…","hidden":false,"required":true}
      a template field can be relabelled, explained, hidden (unless required) or made required — never
      renamed, retyped or made optional.
  {"op":"add_option","collection":"application","field":"stage","option":"Phone screen","before":"Interview"}
      only on lists the template marks extensible; always between its fixed first and last options.
  {"op":"set_option","collection":"application","field":"stage","option":"Screen","label":"Recruiter screen"}
  {"op":"set_option", … ,"hidden":true}                           hide an option (not a fixed one)
  {"op":"set_setting","key":"careers_intro","value":"…"}          the app's settings (see manifest.settings)
  {"op":"add_public_field","collection":"job","field":"x_salary_band"}   show your own field publicly (needs admin)
  {"op":"add_collection","key":"x_reference","collection":{"label":"Reference","read":"recruiter","fields":{…}}}
  {"op":"add_action","key":"x_add_reference","action":{"role":"recruiter","op":"create","collection":"x_reference","input":[…]}}
  {"op":"add_role","key":"x_hiring_manager","capability":null,"description":"…"}  (granted by name; needs admin)
  {"op":"fill_slot","page":"candidate","slot":"sidebar","key":"references","code":"function Fill({ record, app }) { … }"}
      put your own component into a SLOT the page declares (manifest.pages[].slots). The page keeps
      receiving template updates; your component keeps its place.
  {"op":"add_page","page":{"slug":"x-offers","title":"Offers","audience":"member","role":"recruiter","code":"function View(props){…}","sources":[…]}}
  {"op":"fork_page","page":"pipeline","code":"function View(props){…}"}
      LAST RESORT: replaces a template page; that page stops receiving template updates. Say so to the person.
  {"op":"add_scenario","scenario":{"name":"A visa question is required on applications","steps":[…]}}
      a check that must pass before publishing. steps: {"act": action, "as": role, "input": {...},
      "record": "@alias", "save": "alias", "expect_error": true?} and {"expect": {"collection", "where", "count"}}.
      Sample records are available as "@alias" (manifest.fixtures `_as`).

CODE (fill_slot, add_page, fork_page) runs in canopy's sandbox: the dynamic-view kit (Card, Table,
Stat, Badge, Form, Input, TextArea, Select, Button, FieldInputs, FieldValues, Slot …), props
{data, app, scope}; app.query(collection, {where, order, limit}) (a where value may be a list: any
of), app.act(action, {record, input, params}), app.bulk(action, {records, input, params}) for an
action the template marks bulk, app.link(link, record) → {url} for a record's private link,
app.go(page, params). When the manifest declares `tasks`: app.tasks.query({status, category,
due_after, due_before, template, recurring, q, limit}) reads the ONE bound project's tasks (each row:
key, title, notes, status, category, due, priority, labels, assignee, recurrence, template, last_done,
done_by {name, at} or null, comments [{body, by, at}] the last 3 oldest first, url), and
app.tasks.act(name, {key, input}) runs a declared task action as the viewer (app.tasks.declared lists
them); never in a draft's test copy (tasks are live work). A slot fill is `function Fill({ record, app, ...slotProps })`. Never <form>.

SALESFORCE: when the manifest declares `connections`, a page calls
canopy.connection.request({provider: "salesforce", method, path, query?, body?}) ->
Promise<{status, body, text?}> (status is Salesforce's own; refusals reject with a sentence to show).
Reads run any time. A write must start from a click on the page: call it first thing in the onClick
handler (the click only counts for about 5 seconds, so a write after several seconds of awaits is
refused with "This needs a click."). One click opens up to 10 minutes and 500 writes within what the
manifest declared; the window closes 120 seconds after the last write. To read and dedup before the
first write, call canopy.connection.begin() first thing in the onClick: it opens the window and resolves
{expires_at, hard_limit_at, writes}. Calls wait their turn, at most 8 at once from a page; one that waits
a full minute rejects with a sentence. Not connected, the message says where and the rejection has
e.connect_url: a button that calls canopy.navigate(e.connect_url) asks the viewer and opens it in a new
tab. Only on a member's page, never a public one and never a draft's test copy.
GOOGLE DRIVE: the same calls with provider "google" reach the VIEWER's own Drive (as "viewer" only),
relative to https://www.googleapis.com/drive/v3/: reads GET files, GET files/<id>, GET
files/<id>/listLabels; write POST files/<id>/modifyLabels (a click, as any write). Declare paths
with * for the id (files/*/modifyLabels). Query names are limited to Drive's listing ones (q,
pageSize, pageToken, fields, orderBy, spaces, corpora, driveId, supportsAllDrives,
includeItemsFromAllDrives, includeLabels, maxResults). It runs only on an hour of access the viewer
grants (never a stored connection): without one the rejection has e.connect_url, as above.

BROWSER EXTENSION: when the manifest declares `browser` for a member page, the Dimagi Canopy
extension's side panel shows that page beside a matching tab, and there canopy.browser is set
(null everywhere else — always check): canopy.browser.context() -> Promise<{tab: {url, title}} | null>,
canopy.browser.request(name, args) -> Promise<result> for a declared adapter capability (gmail:
read_open_email -> {threadId, messageId, subject, date, from, to, cc, bodyText} or null;
compose_recipients -> {to, cc, bcc} or null), canopy.browser.on(event, handler) for a declared event
or "context" (the tab changed), returning an unsubscribe, and canopy.browser.open(url) for an https
URL from a click. A refusal rejects with e.code (no_adapter, not_ready, unknown_capability,
adapter_error, timeout, bad_request). The template declares it; a workspace change cannot.

BUNDLE PAGES: a page with "kind": "bundle" is the template's own static files (an existing web UI:
its "entry" HTML plus JS, CSS, images, fonts, uploaded as `files` with the version, never as a
change). You cannot fork it, fill it or add one; you can retitle it or take it off the menu
(set_page). Its code uses the same calls through window.canopy, which canopy injects ahead of the
page's own scripts: await canopy.ready (-> {app, scope, browser}), then canopy.app.query / act / bulk /
link / history / upload / tasks, canopy.connection.request, canopy.browser, canopy.page, and
canopy.open(page, params, {newTab}) to open another page of the app by slug or by its entry path
("kanban/kanban.html?id=5"). It has no storage, no network but its own files, no eval, no
alert/confirm/prompt and no popups (docs/architecture/workspace-apps.md § Bundle pages).

WHAT THE TEMPLATE DECLARES (read it in the manifest; a workspace change cannot add these):
  an action's `when` (it applies only to a record in that state — offer the button only then),
  `actor_field` (only the person the record names may run it: an approver approves), `params` (values
  it uses but never stores, e.g. an email's wording), `then` (records the app writes after it), and
  `links` (a private page for one record — a candidate picking a time), and `tasks` (which canopy
  tasks the pages act on; its project is a setting, and changing that setting needs an admin). A field with `read` is
  readable by that role only (self-ID answers: compliance) and is absent for everyone else: never
  show it, mail it or copy it into another field. `retention` rules anonymize records on a clock.
  A collection's `scope` narrows it to records: a member sees only the jobs (and their candidates)
  whose team they are on, so a query can return fewer rows than exist — say "none you can see",
  never "none". An effect's `delay_param` holds its email back that many hours.

A WHOLE NEW VERSION: changes never reach an app's `connections` or `browser`. For an app the
workspace installed from its own manifest, a maintainer (a person or an agent an admin named on
…/maintainers) publishes the whole manifest, pages included, as a new version: POST
/api/workspace-apps/{workspace}/{key}/versions (tool `publish_workspace_app_version`) with
{manifest, note}. It is validated like an install, must pass its scenarios, and goes live at once
(any earlier version can be restored). A maintainer at the `content` level gets a 403 naming each
change to who can see or do what; that needs the `permissions` level or an admin.

ALWAYS: one change per request item; tell the person in plain words what you changed and that they
should open the draft's test copy and try it before publishing. Never claim it is live. If you were
asked onto the draft by a maintainer (an agent helping), you may read, change, test and check this
draft only; publishing is theirs, and a 403 on publish is that rule, not a fault.
"""
