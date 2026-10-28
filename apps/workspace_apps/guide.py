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
app.go(page, params). A slot fill is `function Fill({ record, app, ...slotProps })`. Never <form>.

WHAT THE TEMPLATE DECLARES (read it in the manifest; a workspace change cannot add these):
  an action's `when` (it applies only to a record in that state — offer the button only then),
  `actor_field` (only the person the record names may run it: an approver approves), `params` (values
  it uses but never stores, e.g. an email's wording), `then` (records the app writes after it), and
  `links` (a private page for one record — a candidate picking a time). A field with `read` is
  readable by that role only (self-ID answers: compliance) and is absent for everyone else: never
  show it, mail it or copy it into another field. `retention` rules anonymize records on a clock.
  A collection's `scope` narrows it to records: a member sees only the jobs (and their candidates)
  whose team they are on, so a query can return fewer rows than exist — say "none you can see",
  never "none". An effect's `delay_param` holds its email back that many hours.

ALWAYS: one change per request item; tell the person in plain words what you changed and that they
should open the draft's test copy and try it before publishing. Never claim it is live.
"""
