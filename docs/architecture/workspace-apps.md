# Workspace apps

A **workspace app** is a self-contained application a workspace installs from canopy's app store,
switches on or off, customizes, and can serve publicly (a careers page). The first is **Hiring**
(`apps/workspace_apps/bundled/hiring/`), an applicant tracker meant to replace the ATS Dimagi uses today.

The design follows what the research found across Shopify, Salesforce, SAP, Power Platform, Odoo
and Figma: platforms whose customers customize *and* keep receiving updates give every part one
owner in advance, keep the customer's changes as **data over declared anchors**, and never let a
change edit the vendor's artifact in place.

## Three layers

| Layer | Model | Who changes it |
|---|---|---|
| **Template** — what an app is | `AppTemplate` + immutable `AppTemplateVersion` (a manifest). canopy's own are synced from `bundled/<key>/` (bump `version` to ship); a workspace's own app is a template it owns. | canopy (PRs), or the workspace for its own apps |
| **Installation** — the app in one workspace | `AppInstallation`: a template + the live `AppVersion`. Never a copy of the template. | admins (`integrations`): install, on/off, public, remove |
| **Version** — the workspace's changes | `AppVersion`: the template version it runs on + an **overlay** (keyed ops). Drafts, the published one, the ones it superseded — append-only. | maintainers: admins, or a member granted the app's `maintainer` role (the head of HR) |

What runs is `compose(template manifest, overlay)` (`overlay.py`), validated again
(`manifest.py`). A template update is the same overlay over the newer version.

## The manifest (an app is data, never server code)

`roles` (each a **workspace capability** via `permissions.can`, or grant-only), `collections`
(typed fields incl. `file`; `read` role; optional `public` projection), `actions` (the only writes;
`effects` the platform performs — email to an address on the record, worded by settings),
`pages` (sandboxed JSX, with named `slots`), `settings`, `fixtures` (sample records for test
copies) and `scenarios` (plain-language checks). Installing one runs nothing on the server.

What an app can say without code, each enforced in `services.act` / `services.query`:

| Primitive | What it does | Hiring uses it for |
|---|---|---|
| `when` on an action | applies only to a record in that state (else 409) | reject only an active candidate; send only an approved offer |
| `actor_field` (+ `override_role`) | only the person the record names may run it | only the named approver approves |
| `params` | values the action uses but never stores | the wording of one rejection email |
| `then` | records the app writes after the action, `unless` some record still matches, `if_target` its state | stage history; the last approval opens the job; mark hired closes the opening |
| `bulk` | one action over up to 200 records, each alone, emails rendered per record | bulk reject with per-candidate names |
| field `read` | readable by that role only — absent from queries, history, emails, links | voluntary self-ID (compliance); private notes |
| `links` + role `link` | a signed private link to ONE record; its holder reads declared fields and runs only the link's actions on it | a candidate picks an interview time; accepts an offer |
| effect `ics`, `to_field: "ref.email"`, `link` | a calendar invite, an address one ref away, the record's private link | interview invites; offer emails |
| `public.feed` | schema.org JobPosting JSON-LD (in the page head) and an XML feed, from public fields only | Google for Jobs, LinkedIn/Indeed XML |
| `retention` | on a clock (`ticks`, twice a day): anonymize or delete records matching a `where` after N days (or a setting), skipping a `hold_field`, deleting `cascade` children and files | rejected candidates by region; expired talent-pool consent |

A workspace's own actions (overlay `add_action`) may not declare `then` or be link actions: both
act beyond the caller, so only a template declares them. Admins also get, for every app, a
retention preview and run, and a data-subject export by email (Customize → Privacy).

## The overlay (`overlay.py`)

The workspace **adds** in its own namespace (`x_` fields/collections/actions/roles, `x-` pages) so
a template addition can never collide, and changes a template element only through narrow doors:
relabel / help / hide / require (never un-require), add options to lists the template marks
`extensible` (between `fixed_first` and `fixed_last`), switch an action off, add its own fields to
an action's input, fill a declared **slot**, set a setting. `fork_page` replaces a page and is
shown as "no longer receives template updates". Every op records the person's words (`intent`).

Ops apply one at a time, validated after each. One whose anchor is gone or that the template made
invalid is **quarantined** with its reason — never dropped, never breaking the app.

Template pages draw what the workspace adds without knowing it: `FieldInputs` / `FieldValues`
render `x_` fields from the composed data model, and `Slot` draws slot fills, each in its own
error boundary.

## The life cycle (for someone who is not a developer)

1. **Draft** (`POST …/draft`): starts as the live app, with a **test copy** seeded from `fixtures` —
   never live data. On the test copy the maintainer holds every app role, and nothing is emailed.
2. **Change**: in plain words to an agent (the Customize page opens a canopy chat; the agent reads
   `…/guide` and POSTs ops to `…/draft/ops` — every route is an MCP tool), or with the page's own
   forms (add a question, add a stage, settings and email wording). Each change is reviewed as a
   plain-English line with its intent; a refused one says why.
3. **Try it**: every page of the draft, public ones as a visitor, on the test copy.
4. **Check**: the scenarios run on a scratch copy; the workspace can add its own.
5. **Publish**: refused while anything is quarantined or a check fails. A change to what the
   public sees or who can do what (`overlay.RISK_REASONS`) needs a workspace admin.
6. **Restore**: any earlier version becomes live again, as a new version. Overlays only add, so an
   earlier version reads today's records.

**Template updates** arrive as "update available" and are taken as a draft (`upgrade=true`): the
same overlay over the new version, the changelog, quarantined ops to look at, test copy, checks,
publish.

## Reused, not rebuilt

- **Access**: app roles are workspace capabilities; managing is `integrations`, granting is
  `members.manage`; every route declares `@gate`. No workspace role is named.
- **Pages**: the dynamic-view sandbox (opaque origin, `connect-src 'none'`, no `allow-forms`),
  compiler, kit and protocol. An app page gets an `app` prop; a plain view still refuses writes.
- **Public pages**: a scoped path (`/w/<ws>/app/<key>/`), `auth=None` routes that 404 alike, a
  honeypot, a DB-counted per-address limit. Files use the chat-attachment bucket.
- **Agents**: changes are made through canopy's own chat and MCP tools — canopy-web holds no model key.

## Security, said plainly

- A **visitor** reads only public projections of enabled, public apps, creates only through
  public actions (declared, typed, size-limited inputs; refs only to allowed records; files only
  for a public form's file field; rate-limited) and gets back `{id}`.
- **Test copies** are separate spaces of records; nothing in one reaches live or sends email.
- An **app author or agent** cannot run server code, write access rules, or read what its viewers
  could not; page code can see what its page shows.

## Before the switch

Hiring 2.0 covers an ATS's daily work, built from public ATS research (Hal's research report has the
checklist): requisitions with approvals and openings, pay ranges, the application
portal at its canopy link (`/w/<ws>/app/<key>/careers`; no custom domain), privacy notices and
self-ID, an application-review queue, the pipeline with bulk actions, interview plans and kits,
scorecards, pick-a-time links with calendar invites, offers with approvals and an accept link,
referrals, prospects, templates, reports and retention, plus an importer for the outgoing ATS's export API
(`manage.py import_ats`). Still to do: per-job access (a hiring team sees only its jobs;
confidential jobs hidden from other interviewers), Google Calendar free/busy and Meet links,
Gmail-threaded email, Slack nudges, e-signature, a hire webhook to the HR system, SSO-driven
interviewer groups, legal sign-off on the retention defaults, and turning on `WORKSPACE_APPS_SEND_EMAIL` with an SES sender.

## Open decisions

- **Tier**: `workspace_apps` is product (it imports the dynamic-view sandbox). Move the sandbox
  down first if apps become framework.
- **Publishing to the store**: a workspace's own template is unlisted; listing it for other
  workspaces (and Ashby-style promotion of common overlays into a template) is next.
- **Intent replay**: on a quarantined slot fill, re-asking the agent with the recorded intent is the
  untested hypothesis from the research — measure before automating it.
