# Roles — who can do what, and what actually enforces it

> Every term here — workspace roles, agent roles (`owner` / `admin` / `member` /
> `contact` / `system`), access (`full` / `confined` / `none`), the interface,
> turn mode — is defined once, with THE rule that decides an agent turn, in
> [`access.md`](access.md). This page is about enforcement.

There are five tiers. Each contains the one below it.

| Tier | Enforced as | Can |
|---|---|---|
| **Link reader** | *no account* — a link you were sent | Read what was shared: a storyboard, a narrative, a walkthrough, a session transcript, the public explainer. |
| **Viewer** | workspace member, `viewer` | Interact with an agent through what its published interface offers members (none published → nothing): chat, answer a blocked question, approve or decline a task, read the board. Read the turns you started. Cannot change what an agent *is*, or anything else. |
| **Editor** | workspace member, `editor` | Create and edit agents, run turns (the agent's whole profile, **always `manual`** unless you are its admin), edit schedules, route work (never in `auto`), **delete an agent**; create and change every product surface (projects, walkthroughs, shareouts, reviews, DDD, storyboards, issues). |
| **Admin** | workspace member, `admin` | Run the workspace: read every LOG (the event log, every turn's prompt / ledger / transcript / caller context, runner drills, connected-site health); invite, re-role and remove members **below admin**; the integrations (inbound mailboxes + push config, Slack history + sync, Test connection); the default runner order, retention, admission rules, and starting or declining a proposed project. Holds no keys. |
| **Owner** | workspace member, `owner` | The keys: make admins and owners, the shared vault, every agent's credentials (a workspace owner is every agent's admin), the Slack app itself, registering or changing a connected site, deleting or moving the workspace. |

The membership roles are a total order — `WorkspaceMembership.ROLE_RANK`
(`apps/workspaces/models.py`) is `{viewer: 0, editor: 1, admin: 2, owner: 3}`.

**What each role may DO lives in one table: `apps/workspaces/permissions.py`.** It maps every
capability (`CONTENT_WRITE`, `AGENT_WORK`, `LOGS_READ`, `MEMBERS_MANAGE`, `INTEGRATIONS`,
`OWN`, …) to the lowest role that holds it, and every gate outside `apps/workspaces/` asks
`perms.can(user, workspace, perms.<CAPABILITY>)` — never a role name.
`tests/test_roles_named_only_in_workspaces.py` fails the build on a role constant, a
`member_role(...)` comparison or a rank check anywhere else — a tier written by hand in a
second place silently changes meaning the next time a role is inserted. The frontend mirrors the table in
`frontend/src/lib/workspaceRoles.ts`, and a vitest reads `permissions.py` to keep the two equal.

**Every route declares its gate, at the route.** `@gate(...)` (`apps/api/route_gates.py`, which
holds the closed vocabulary) sits directly above every view's `def` and names the gate it
enforces, e.g. `@gate("agent.work", setting=True)`; `tests/test_every_route_declares_its_gate.py`
fails on an operation in the OpenAPI schema whose view has none — so a new route is a decision
about who may call it, made when it is written, not discovered by an audit. A write a VIEWER may
make must also give its reason (`viewer_write="..."`). Hand-written MCP tools, which are not
routes, declare theirs in `route_gates.HAND_TOOL_GATES`.

An admin manages members only **strictly below themselves** (`permissions.may_manage_member`):
they invite, re-role and remove viewers and editors, see invite links only for those roles,
and can neither touch nor mint an admin or an owner. An admin is not an agent's admin either —
`Agent.is_admin` is the agent's owner, a workspace OWNER, or an explicit `AgentAdmin` grant —
because running a workspace is not holding its agents' keys.

## Workspaces nest — and only OWNER and ADMIN flow down

A workspace may have a `parent` (`Workspace.parent`), so an org sits above its divisions:
`dimagi` → `connect`, `strategy`, `operations`, `commcare`, `global-solutions`. The tree grants
exactly this: **an owner of a workspace is an owner of every workspace below it, and an admin
is an admin of every workspace below it** (`WorkspaceMembership.INHERITS_DOWN`). Nothing else is
inherited. A parent's editors and viewers have no access to a child, and a child's members have
none to its parent or siblings.

Admin flows down by the owner's explicit choice: an org admin is expected to run its divisions,
so one who cannot see a division (`strategy` while administering `dimagi`) is a bug, not a
boundary. Admin still holds no keys (`permissions.OWN`), so an inherited admin runs a division
without holding its agents' keys.

The narrowness is the design. `dimagi` auto-approves an access request from every `@dimagi.com`
address (`auto_approve_role = editor`), so if its
*editors* inherited, every employee could read — and, as editors, delete — every division's
agents, which is precisely what a division workspace exists to prevent.

- Resolved in `services.membership` (the sole authorizer): a direct `owner` row wins; otherwise
  the best owner/admin role held on any ancestor, if it outranks the direct row, comes back as
  an **unsaved** membership with `inherited = True` at that role. `is_member`, `member_role`,
  `user_workspace_slugs` and the pinned `/api/w/{ws}/` gate all read through it.
- Nesting is an owner's act on the **parent**: `POST /api/workspaces/` with `parent` requires
  owning the parent, and `PUT /api/workspaces/{slug}/parent` requires owning both the workspace
  and its new parent. Cycles are refused on save (422 over the API).
- A workspace with children cannot be deleted (409, naming them).

## A read that names no workspace stays inside one organisation

Workspaces must be specified; workspace confusion must not be possible. More than one ROOT
workspace exists (a family workspace, `marshellis`, beside the `dimagi` tree), and a caller in
both must never get the **union** of their memberships from a flat `/api/…` list or an MCP tool
called without `workspace` — that would land one tree's rows in the other's work. Writes follow
the same principle through `creation_workspace`.

**The rule** (`services.read_scope`, reached through `request_workspace_slugs`):

| Request | Scope of a list / search |
|---|---|
| pinned — `/api/w/<ws>/…`, or the MCP `workspace` argument | that workspace **and every workspace below it you belong to** (`pinned_read_scope`) |
| unpinned, every membership in ONE tree (`dimagi` + `connect`) | the union — one org's own data |
| unpinned, memberships in MORE THAN ONE tree (`dimagi` + `marshellis`) | **422** `workspace-required`, naming the caller's workspaces grouped by tree |
| unpinned, a connected site acting for its visitor (delegated token) | the site's own workspace — a site is one tenant's |

A tree is a root and everything under it (`services.tree_roots`; `WorkspaceOut.root` carries
it to clients). The rule fails closed: nothing is guessed.

### A pinned read covers the workspaces below the pin

This is deliberate: *a pinned READ of workspace W returns W plus every DESCENDANT of W that the
caller is a member of* — directly, or by owning an ancestor (inherited ownership). So
`list_agents(workspace="dimagi")` includes every agent living in `connect`, and a client need not
fan one read out per workspace to put the organisation back together.

| Pinned read by… | `/api/w/dimagi/…` | `/api/w/connect/…` |
|---|---|---|
| a member of dimagi and connect | dimagi + connect | connect only — a pin never reaches UP |
| a dimagi member who is not in connect | dimagi only — never a descendant you are not in | 404 (not a member) |
| an owner of dimagi (owns connect by inheritance) | dimagi + connect | connect |

- **Separate roots never join.** A root's subtree cannot contain another root, so a pin to
  `dimagi` never returns `marshellis` rows, whoever asks.
- **Writes pinned to W land in exactly W** — creation is `creation_workspace`, unchanged.
- **Bulk writes pinned to W touch exactly W** (`services.write_scope` / `request_write_slugs`,
  and `perms.request_slugs_with`, which reads it): "clear every shareout" pinned to `dimagi`
  leaves `connect`'s alone, though the pinned read of the same feed shows both. The same scope
  covers a bulk session reset and `purge_session_noise` with `apply`. A capability-gated LIST
  (the event log's `LOGS_READ`) reads `perms.request_read_slugs_with`, which takes the subtree.
- **By-id gates are unchanged** (`member_scope`: pinned → exactly the pin). A row a pinned list
  shows from a child workspace is opened under ITS workspace — the web client links each row to
  its own `/w/<ws>/…` and marks rows from another workspace with a small badge
  (`components/WorkspaceBadge`); list items carry their workspace (`workspace` on agents, tasks,
  projects and sessions, `workspace_slug` on turns).
- **Per-workspace settings and admin pages stay exactly W.** Members, invites, access settings,
  access requests, the shared vault, the runner order, retention and admission rules are read and
  written by `/api/workspaces/<slug>/…` and gated by `membership`/`perms.can` on that one slug —
  none of them reads the list scope.
- **Clients learn it from `GET /api/workspaces/`**: each row carries
  `pinned_read_covers_descendants: true`, and the `workspace-required` refusal names `pins` —
  the fewest pinned reads that cover every one of the caller's workspaces (`covering_pins`; one
  per organisation when the caller is in its root). The canopy CLI and the web client read once
  per pin instead of once per workspace; a client that sees no flag keeps fanning out.

### A user-level view reads each organisation on its own

A page that is about the PERSON rather than one workspace — Home, Supervisor, the IDE, the chat
home — reads a **user-level view**: `/api/me/<list>/`. It is the ordinary list run once per
organisation the caller is in (`wsvc.each_organisation`: one pinned read per `covering_pins` pin,
each under that list's own read rule and gate) and concatenated; every row carries its workspace
(attention rows also their `organisation`), and the view re-sorts. No query ever spans two trees,
and no client fans out, merges or dedupes. Pinned — `/api/w/<ws>/me/<list>/` — it is that one read.

| View | The list it runs per organisation |
|---|---|
| `GET /api/me/attention/` | the attention sources (`apps/attention`) |
| `GET /api/me/sessions/` | `GET /api/canopy-sessions/` |
| `GET /api/me/tasks/` | `GET /api/tasks/` (`?waiting=me` is your inbox everywhere) |
| `GET /api/me/agents/` | `GET /api/agents/` |
| `GET /api/me/projects/` | `GET /api/projects/`, the projects you own |

A new page off any one workspace reads a user-level view, adding one (a thin route over
`each_organisation`) when the list it needs has none — never a client-side loop over pins.

**What it does not touch:**

- **By-id reads.** Opening one object you belong to mixes nothing, so gates of the form
  `obj.workspace_id in …` read `request_member_slugs` / `member_scope` (every membership,
  never refused). A huddle or thread opened by id reads within *its own* tree
  (`tree_scope(user, <its workspace>)`): its members can live anywhere in that organisation.
- **Who you are and where you are** — `/api/me/`, `GET /api/workspaces/` (the switcher),
  invites and access requests, "my memory" (`people/me`, `hcp_collections.contexts`).
- **The fleet's plumbing** — runner heartbeat, claim, schedule sync, mailbox probe
  (`runner_tenant_slugs`, `inbound.runner_mailboxes`), the runner list (`_runner_read_q`),
  routing, the per-user MCP agent-tool registry, and the realtime supervisor socket.

Lists of huddles, threads and the shared-sessions feed take the same rule as every other list
(not `tree_scope`, which reaches UP from a pinned child): pinned to `dimagi` they cover `connect`
too, pinned to `connect` they do not reach `dimagi`. Opening one by id still reads its own tree, as above.

The web client mirrors it (`frontend/src/api/workspaceRetry.ts`): a flat GET refused this way
is answered from PINNED reads. On a tenant page it is retried pinned to the URL's `/w/<ws>/`. On
a root page (`/supervisor`, `/sessions`, `/activity`, `/schedules` — the fleet views, which span
every workspace you belong to) it is looped over the `pins` the refusal names (each covers the
workspaces below it — usually one read per organisation), and the lists merged — so the server never answers a cross-organisation query and
the fleet view still shows everything. Don't retry a root page pinned to one membership: that
hides every other workspace's agents on `/supervisor`.

A stricter rule — *every* unpinned list refused, even inside one tree — is a possible
follow-up; it would break every flat-route client in the Dimagi fleet today.

## The first tier has no account at all

The genuinely read-only role is **not** a membership role — it is having a link and no
account. The scoped viewers under `/w/:workspace/` — `storyboard/:slug`, `narrative/:slug`,
`walkthrough/:id`, `review/:id`, `share/:token`, `ddd-release/:narrative/:runId` — and the
public site serve anonymous readers (`_SCOPED_VIEWER` in `apps/common/middleware.py`).

Two things enforce it together, and **both are required** — this catches people out:

1. `auth=None` on the route (or, for pages, nothing to authenticate).
2. An entry in `PUBLIC_PATH_PREFIXES` in `apps/common/middleware.py`. That middleware is
   **default-deny**, so `auth=None` alone still bounces an anonymous caller to Google.

The failure modes differ and tell you which half you missed: a **page** path without the
allowlist entry gives a **302** to sign-in; an **`/api/`** path gives a JSON **401**.
`tests/test_public_routes_reachable.py` pins each public surface, because
`config/settings/test.py` sets `REQUIRE_AUTH = False` — which makes the login middleware
inert for the whole suite, so without `@override_settings(REQUIRE_AUTH=True)` an allowlist
entry could be deleted with CI still green.

Beside `PUBLIC_PATH_PREFIXES`, the login middleware's artifact exemptions (`LoginRequiredMiddleware`) are
the scoped viewers (`_SCOPED_VIEWER`), the per-artifact read APIs (`_is_artifact_api`) and
the retired flat addresses (`_is_flat_artifact`, which serve only the "links now include the
workspace" 404). The API layer enforces the share token underneath; which pages these are:
[pages.md § Scoped public viewers](pages.md#scoped-public-viewers-anonymous-with-a-token).

## How you get into a workspace

Every door — invite, approved access request, creating a workspace, and an agent's own
login — is described once in [access.md § Getting into a workspace](access.md#getting-into-a-workspace).

`apps/workspaces/tests/test_access_requests.py` asserts the only code that can create a
`WorkspaceMembership` is `services._grant` (invite acceptance + request approval),
`services.ensure_member` (the default-workspace bootstrap and the agent-login door) and
workspace creation, so a new door cannot appear quietly; `tests/test_no_implicit_enrolment.py`
pins the same for `ensure_member` callers.

**Never grant membership as a side effect.** Not of a visibility check, and not of a write:
a create resolves its workspace through `wsvc.creation_workspace`, which only ever returns a
tenant the caller is already in and refuses with 422 when there is none. Calling
`ensure_member` from a create path would let an invite-admitted user become an editor of the
org default by posting one row; `tests/test_no_implicit_enrolment.py` pins it.

**Every member is a person or an agent's own login.** An automated sender (CloudWatch
alarm mail, CI) is not a member: it is one agent's inbound-mail *trigger*, firing with the
authority of whoever set it up. See `docs/architecture/access.md` § "Triggers".

## What enforces the membership tiers

Every surface is gated now, through the table above; this section is about the shapes that
recur and the routes that deliberately sit off the ladder.

**Every agent-by-slug route** (agents, harness, agent runs, capabilities, threads, mail, check-ins) asks
`apps/agents/gates.py` — never a local copy; `tests/test_agent_gates_not_copied.py` fails on an
import of a private `apps.agents.api` helper from another module:

- `agent_for_read` — membership (`wsvc.request_member_slugs`: pinned, the pin and the caller's
  workspaces below it). Interaction and reads. Interaction and reads. A non-member gets 404, never 403,
  so the API never confirms an agent exists to someone who cannot see it.
- `agent_for_work` — `AGENT_WORK` (editor and above). Reshaping: upsert, runner assignment,
  runner rules, turn mode, syncs, turns, skills, project create/patch, task create/patch. Setting
  `auto` — on a routing rule, an actor route or the agent's switch — additionally needs
  `Agent.is_admin` (`_refuse_auto_unless_admin`).
- `agent_for_admin` — `Agent.is_admin`: the agent's owner, a workspace owner, or an explicit
  `AgentAdmin`, each a CURRENT member. Credentials, the vault pointer, the interface, the
  Google mailbox mint, moving the agent to another workspace.
- `allow_own_login=True` (agent runs, run documents, capabilities) admits the agent's own login
  (`Agent.is_own_login`) whatever its membership or role — `Agent.user` is one-to-one with the
  agent, so it reaches only that agent's own rows. Nowhere else.

**A box holds an agent only if its owner is one of the agent's admins**
(`agents.services.runner_may_hold_agent`) — don't let a runner-assignment row be the trust
boundary: editors write those rows. **A box serves only its owner's workspaces it is enrolled
in** (`harness.routing.runner_tenant_slugs`). Both rules, and everything that asks them:
[runners.md § Which workspaces a runner serves](runners.md#which-workspaces-a-runner-serves-the-tenant-gate)
and [§ Which agents a box may hold](runners.md#which-agents-a-box-may-hold).

**Only the box that claimed a turn reports on it** — start, finish, ledger events, transcript
lines (`harness.api.reporting_turn_or_404`). An unclaimed turn takes the editor tier.

**A turn's CONTENT is a log** (`apps/harness/turn_access.py`). Every member sees that a turn
ran, when and how it ended; its prompt, result, ledger, raw transcript, caller context and
public transcript link are read by whoever started it, the agent's admins, the box that ran
it, and workspace admins — and, for a chat turn, by exactly the people who may read that chat.
Lists blank the content and set `content_hidden`; detail routes and the live turn socket 404.

Deliberately off the ladder:

- **`POST /{slug}/tasks/{ref}/actions` branches on `action`.** `approve`, `decline` and
  `reply` answer a task already on the board (the User tier); `nudge` and `done` reshape or
  start work and take `AGENT_WORK` (`_EDITOR_ACTIONS`). `tests/test_agent_acl_gates.py`
  asserts the two tiers partition `AgentTaskAction.ACTION_CHOICES`. Creating a task (which may
  carry `on_approve`) takes `AGENT_WORK` too — or the agent's own login — because approving it
  (the User tier) runs the prompt.
- **`POST /{slug}/bootstrap-report`** is gated on running the agent (`caller_runs_agent`),
  strictly tighter than any role.
- **`POST /api/agents/` (upsert)** takes the slug in the body, so it spells resolve-then-
  authorize out by hand: a non-member of an EXISTING agent's workspace gets 404 (a 403 would
  make it an oracle over every agent name, since `Agent.slug` is globally unique).
- **`GET /{slug}/credentials/resolve`** returns plaintext, bearer-only, to a box that may hold
  the agent (above) — or to the agent's owner or an admin (`Agent.is_admin`)
  resolving for a local Claude Code session. The audit event records which (`via: runner |
  admin`). A browser session only ever sees the masked status.
- **`GET /{slug}/credentials/access`** (member, session or bearer) answers the same gate for
  the caller without returning values: `{agent, may_resolve, via, reason, delegated_withheld}`.

### A view that judges many pairs reads once

A view that asks the same question of many (person, workspace) or (person, agent) pairs — the
agent topology, an agent's roster, a list filtered by what the viewer may do — reads the facts
once and answers every pair from them. It never calls `member_role`, `perms.can`,
`Agent.is_admin` or `access.decide` per pair in a loop: each of those reads memberships and walks
the tree, so a loop over pairs is N² queries (the agent topology was 6,000+ for 24 agents).

- **One person, many workspaces:** `perms.slugs_with(user, capability, within=…)` — one
  `wsvc.member_roles` read.
- **Many people, their roles:** `wsvc.member_roles_of(users)`, then `perms.role_allows`.
- **Many people × agents:** `access.Standings(users, agents)` (every role and every agent grant,
  in a fixed number of queries), passed to `access.decide(…, standing=)`, `Standings.is_admin`,
  `Agent.is_admin(user, role=, granted=)` or `access.roster(…)`. Everyone who administers one agent:
  `Agent.admin_user_ids()`.
- **Many runners:** `routing.runner_tenant_slugs_of(runners)` (the tenant rule — prefetch
  `enrollments`), `runners.administered_runner_ids(user, runners)`.
- **The tree:** `ancestor_chains({slug: parent})` and `wsvc.descendant_map(slugs)`, not
  `ancestor_slugs()` / `descendant_slugs()` per workspace.

Each single check is the bulk read of one (`runner_tenant_slugs(r)` is
`runner_tenant_slugs_of([r])[r.pk]`, `can_administer_runner` likewise), so a rule has one
implementation.

The rule is still one function: a bulk read only supplies its inputs, read per request and dropped
with it — nothing is cached. `tests/test_bulk_authz_parity.py` holds every bulk read to the
one-at-a-time answer; `test_agent_topology.py` and `tests/test_fleet_reads_do_not_grow.py` pin
query counts that do not grow with agents or runners.

### A token can do whatever its person can

A PAT, or an MCP client signed in through OAuth, acts as its user with exactly that user's
role — there are no web-app-only actions. The rule and its two exceptions (`create_token`,
and passkey step-up routes) are in [mcp-surface.md](mcp-surface.md) (the "Exposure is the
default" paragraph).

### Leaving a workspace takes what it carried

Every grant that hangs off membership is checked against **current** membership —
including an agent's owner: once removed they are not its admin (credentials, interface,
transfer), get no unconfined profile when messaging it, lend it no GitHub token and are not
pushed about it. And removal sweeps the rest
(`apps/workspaces/departure.py`): AgentAdmin, RunnerAdmin and chat participant rows in that
workspace, lent GitHub tokens, their box on its agents' runner lists, and agent ownership
(cleared, with a `warn` Event saying so); their open chat sockets there are closed. That is
because `dimagi` auto-approves access requests: without the sweep, a removed person asks again and every
dormant grant wakes up. Removing or demoting an org owner sweeps the divisions they reached
only by inheritance.

Listings read the tree too: members, an agent's roster, push recipients and the last-owner
guard all include owners of a parent workspace (`services.effective_memberships`), and a
direct owner may step down while a parent's owner still owns the workspace. Only a direct
owner may detach a workspace from its parent.

### Which workspace a session lives in — and moving it

A chat session carries its own tenant (`Session.workspace`), and the session ACL
(`apps/canopy_sessions/access.py`) is **tenant-first**: you must be a member of that
workspace before any other rule applies. Runner-discovered emdash sessions are then visible
to every member of it. So *where a session is filed* decides who can see it.

**Filing a new runner-discovered session** (`replace_reported_sessions` →
`apps/canopy_sessions/filing.py`), first match wins:

1. The emdash project names an agent → the **agent's** workspace (it is the agent's work).
2. The runner owner's **default session workspace**, while they are still an editor (or
   better) there — set at Settings → *Session workspace*, or
   `PATCH /api/me/session-workspace/` (MCP `set_session_workspace_preference`,
   `{"workspace": "<slug>"}`; null clears it). Membership is re-checked at filing time, so
   leaving the workspace or being demoted to viewer silently falls back.
3. The runner's own workspace.

Only NEW sessions are filed. The ~10s report loop reuses a session's binding and never
rewrites its workspace, so existing sessions are not re-filed when the default changes, and
a session someone moved stays where they put it.

**Moving a session** (`POST /api/canopy-sessions/{id}/move`, MCP `move_session`, or
*Move to workspace…* in a chat's ⋯ menu): only the person whose session it is — its creator
for a web chat, the owner of the runner it was discovered on for an emdash session — to a
workspace where they are an editor or better. An **agent's** session (`agent` set) cannot be
moved (422): it belongs to the agent's tenant. Moving is tenant-first: afterwards only
members of the target see it, and explicit participants who are not members of the target
lose their participant row (the departure sweep's rule — a kept row would wake up the day
they joined the target; moving back does not restore it). Nothing else under a session
carries its own tenant — messages, attachments, drafts, secrets, keys, transfer requests
and the runner binding hang off the session, and session turns derive theirs from it.
Storyboards, reviews and walkthroughs that merely *link* to a session keep their own
workspace; the link grants nothing.

**The harness** — schedules and turns — is gated too, because it is where the table's
promises actually cash out. A schedule is prompt text a runner later executes *as the agent*,
holding the agent's resolved credentials, and `run-now` means immediately; a turn is the same
thing without the wait. Gated on membership alone, a `viewer` could write a prompt of their
choosing and fire it at the fleet. So:

- Schedule create / update / delete / run-now require `editor`, enforced inside
  `schedule_services._resolve_agent` — **not** in the Ninja handlers, because the MCP tools
  call that service layer directly and a gate in the handlers would be one the MCP surface
  walks around. `list` and `preview` stay at membership: seeing when your team's agent runs
  is interaction.
- `POST /api/harness/turns/` and turn cancel require `editor` on the **agent** branch. Project
  and session turns do not — a session turn is what a chat send produces, and talking to an
  agent is precisely what the interaction tier is for. The carve-out is structural rather than
  conditional: `turn_targets_agent_xor_project_xor_session` means a session turn carries no
  agent FK at all.

**The product surfaces** — walkthroughs, shareouts, reviews, DDD runs and
narratives, storyboards, origin issues, the event log, feedback dispositions — gate every
MUTATION on `editor` in the row's workspace and every READ on membership — a `viewer` must not
approve a DDD gate or wipe a whole feed with `{}`. Three shapes recur, all pinned by `tests/test_product_acl.py`:

- **By-id writes resolve through the read gate first**: a non-member gets 404, a viewer who
  can already see the row gets 403, an editor writes.
- **Bulk writes are scoped by the WRITE set**, `perms.request_slugs_with(request,
  perms.CONTENT_WRITE)`, never by the read set — `clear_shareouts({})`, a narrative delete or visibility flip
  that spans workspaces touches only the rows in workspaces where the caller is an editor.
- **Some rows belong to a person within the tenant.** A walkthrough is changed by its uploader
  *while they are still an editor there*, or by a workspace owner; a shareout is replaced or
  cleared by the person who posted it (`Shareout.created_by`), or by a workspace owner.

The anonymous token reads (`?t=` on walkthroughs and storyboards, `link` reviews, storyboard
feedback) are a property of the link, not of a role, and are unchanged.

**A row with no workspace is visible to nobody.** Reviews, walkthroughs and origin issues fail
closed on a NULL workspace; never give them a `workspace IS NULL ⇒ any signed-in user` leg —
the NULL-means-allow shape (a walkthrough with no workspace is still readable by its share
token).

## What is about to change

`Agent` currently carries **both** an `owner` (a person) and a `workspace` (a tenant), which
is why "who owns this agent" has two answers. The agreed direction is that an agent's
**definition is shared** across tenants — one `echo`, and improving echo improves it
everywhere — while what you own is an **instance** of it. Forking produces a different agent,
not a detached instance.

When that lands, roles attach to the instance and this document's tiers get one meaning each
instead of two. The design, the 60-call-site tenancy risk it carries, and the phasing are in
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`.
