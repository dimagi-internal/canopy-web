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
| **Admin** | workspace member, `admin` | Run the workspace: read every LOG (the event log, every turn's prompt / ledger / transcript / caller context, runner drills, connected-site health); invite, re-role and remove members **below admin**; the integrations (inbound mailboxes + push config, Slack history + sync, Test connection). Holds no keys. |
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

**Every route declares its gate.** `apps/api/route_gates.py` lists, for every operation in the
OpenAPI schema, the gate it enforces, and `tests/test_every_route_declares_its_gate.py` fails
on a route that has none — so a new route is a decision about who may call it, made when it is
written, not discovered by an audit. A write a VIEWER may make must also be listed with its
reason (`VIEWER_MAY_MUTATE`).

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

Admin was added by owner decision (Jonathan, 2026-10-16: "fix it so admin flows down"): an org
admin who could not see a division they were expected to run — ada@ was blind to `strategy`
while administering `dimagi` — read as a bug, not a boundary. Admin still holds no keys
(`permissions.OWN`), so an inherited admin runs a division without holding its agents' keys.

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

Owner decision (Jonathan, 2026-10-11): *"Workspaces should need to be specified — workspace
confusion shouldn't be possible."* A second ROOT workspace (`marshellis`, a family) now sits
beside the `dimagi` tree, and a caller in both — ada@, Jonathan — used to get the **union** of
every membership from any flat `/api/…` list or MCP tool called without `workspace`, so family
rows landed in Dimagi work. Writes were fixed on 2026-10-08 (canopy-web#1289,
`creation_workspace`); reads follow the same principle.

**The rule** (`services.read_scope`, reached through `request_workspace_slugs`):

| Request | Scope of a list / search |
|---|---|
| pinned — `/api/w/<ws>/…`, or the MCP `workspace` argument | that workspace **and every workspace below it you belong to** (`pinned_read_scope`, 2026-10-12) |
| unpinned, every membership in ONE tree (`dimagi` + `connect`) | the union, as before — one org's own data |
| unpinned, memberships in MORE THAN ONE tree (`dimagi` + `marshellis`) | **422** `workspace-required`, naming the caller's workspaces grouped by tree |
| unpinned, a connected site acting for its visitor (delegated token) | the site's own workspace — a site is one tenant's |

A tree is a root and everything under it (`services.tree_roots`; `WorkspaceOut.root` carries
it to clients). The rule fails closed: nothing is guessed.

### A pinned read covers the workspaces below the pin

Owner decision (Jonathan, 2026-10-12): *a pinned READ of workspace W returns W plus every
DESCENDANT of W that the caller is a member of* — directly, or by owning an ancestor (inherited
ownership). Before it, a pinned read was exactly W, so `list_agents(workspace="dimagi")` missed
every agent living in `connect` and every client fanned one read out per workspace to put the
organisation back together.

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
(they used to span the whole of the pin's tree, `tree_scope`, which reached UP from a pinned
child; reconciled 2026-10-12): pinned to `dimagi` they cover `connect` too, pinned to `connect`
they do not reach `dimagi`. Opening one by id still reads its own tree, as above.

The web client mirrors it (`frontend/src/api/workspaceRetry.ts`): a flat GET refused this way
is answered from PINNED reads. On a tenant page it is retried pinned to the URL's `/w/<ws>/`. On
a root page (`/supervisor`, `/sessions`, `/activity`, `/schedules` — the fleet views, which span
every workspace you belong to) it is looped over the `pins` the refusal names (each covers the
workspaces below it — usually one read per organisation), and the lists merged — so the server never answers a cross-organisation query and
the fleet view still shows everything. (It first retried a root page pinned to the first
membership, which hid every other workspace's agents on `/supervisor`.)

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

## How you get into a workspace

Four ways (the full account, in the access glossary's words, is
`docs/architecture/access.md` → "Getting into a workspace"):

- **An invite.** An admin or owner creates one at `/w/:workspace/settings/members`, at a
  role they choose (viewer unless they pick otherwise, never above what they may grant);
  canopy emails the `/invite/:token` link and accepting grants exactly that role (never
  lowering one you already hold).
- **An approved access request.** If a workspace lists your login email's domain in
  `access_request_domains`, you may ask to be invited: `GET /api/workspaces/requestable`
  tells you which, `POST /api/workspaces/{slug}/access-requests` asks. Every admin and
  owner (owners of a parent workspace included) is emailed a link to the request's page,
  where one of them approves at a role they may grant, or denies; you are emailed either
  way. A workspace owner may set `auto_approve_role` (viewer or editor) to approve every
  request on the spot — `dimagi` is set to editor while it bootstraps; every other
  workspace is off. Auto-approved requests are ordinary request records and still email
  every admin and owner, who can change the role or remove the person.
- **An agent's own login.** An agent acts in canopy as its own user (`Agent.user`). When
  it has none, `POST /api/agents/{slug}/canopy-token` (run by `canopy agent vault`)
  creates one: a brand-new, password-less user from the agent's mailbox address, made an
  editor and linked to that one agent. Only a workspace admin or owner may — the people
  who could invite it — and an address that is already a canopy user is refused, never
  adopted (`apps/agents/agent_login.py`). Moving the agent (`POST /api/agents/{slug}/move`)
  brings that login into the destination as an editor on the same bar: only when the mover
  holds `members.manage` there; otherwise the move reports it and adds nobody.
- **Creating one.** Subject to `services.can_create_workspace`: an *invite-admitted* user
  holding no membership may not create a workspace, because otherwise invite-admission
  becomes transitively delegable — create a workspace, mint invites, and each new invitee
  clears the login gate too. `/api/me/` reports `can_create_workspace` so the UI never
  offers a button that 403s.

There is no fourth way. A connected site's visitor is a **contact**, never a member
(`WorkspaceMembership.provisioned_by_app` is provenance on old rows only), and there is no
self-join or domain auto-join: `access_request_domains` (`workspaces/0012`) means "may ask",
never "may join".

`apps/workspaces/tests/test_access_requests.py` asserts the only code that can create a
`WorkspaceMembership` is `services._grant` (invite acceptance + request approval),
`services.ensure_member` (the default-workspace bootstrap) and workspace creation, so a
fourth door cannot appear quietly; `tests/test_no_implicit_enrolment.py` pins the same for
`ensure_member` callers.

**Never grant membership as a side effect.** Not of a visibility check, and not of a write:
a create resolves its workspace through `wsvc.creation_workspace`, which only ever returns a
tenant the caller is already in and refuses with 422 when there is none. Calling
`ensure_member` from a create path would let an invite-admitted user become an editor of the
org default by posting one row; `tests/test_no_implicit_enrolment.py` pins it.

A request cannot be used to probe tenants: a workspace whose domains do not match yours and
a workspace that does not exist return the **same 404**. Approval is upgrade-only, like an
invite, so it never demotes an existing member.

**A member who is not a person: system accounts.** An admin can also create a *system
account*, an automated sender such as CloudWatch alarm mail. It joins as an editor (or
viewer, never above) and **can never sign in**. Inbound mail becomes it only through a
sender binding in that workspace. Membership is created through the same `_grant` door as
an invite. See `docs/architecture/access.md` § "System accounts".

## What enforces the membership tiers

Every surface is gated now, through the table above; this section is about the shapes that
recur and the routes that deliberately sit off the ladder.

**The agents surface** (`apps/agents/api.py`):

- `_get_agent_or_404` — membership. Interaction and reads. A non-member gets 404, never 403,
  so the API never confirms an agent exists to someone who cannot see it.
- `_agent_for_write` — `AGENT_WORK` (editor and above). Reshaping: upsert, runner assignment,
  runner rules, turn mode, syncs, turns, skills, project create/patch, task create/patch. Setting
  `auto` — on a routing rule, an actor route or the agent's switch — additionally needs
  `Agent.is_admin` (`_refuse_auto_unless_admin`).
- `_agent_for_admin` — `Agent.is_admin`: the agent's owner, a workspace owner, or an explicit
  `AgentAdmin`, each a CURRENT member. Credentials, the vault pointer, the interface, the
  Google mailbox mint, moving the agent to another workspace.

**A box holds an agent only if its owner is one of the agent's admins** (or the agent's own
login) — `agents.services.runner_may_hold_agent`. Claiming an agent turn, resolving its
credentials, the per-turn GitHub token, pinning a turn to a box (which also needs the pinner
to be an agent admin or that runner's admin — `access.may_pin_runner`), and every routing write
(runner list, source rules, actor routes) all ask it. Don't let a runner-assignment row be
the trust boundary: the editor tier writes those rows, and a box that may claim an agent can
resolve its secrets, vault keys and the owner's GitHub token. A teammate whose own laptop runs
an agent's work therefore needs an admin grant on that agent.

**A box serves only the workspaces it is enrolled in, and every workspace below one of them**
(`RunnerEnrollment`, 2026-10-11; a division created later is covered too, separate roots never
are) — and only while its owner holds `agent.work` (editor and above) in each: the two intersect in
`harness.routing.runner_tenant_slugs`, so enrollment narrows what membership allows and never
widens it. Pairing enrolls a box in its pairing workspace only; a new membership enrolls
nothing; leaving a workspace drops the enrollment. The owner or a runner administrator manages
the list (`/api/harness/runners/{id}/workspaces`). This is what keeps a separate root (a family
workspace) off the Dimagi boxes, and a family box off Dimagi's work, although one person owns both.

**Only the box that claimed a turn reports on it** — start, finish, ledger events, transcript
lines (`harness.api._reporting_turn_or_404`). An unclaimed turn takes the editor tier.

**A turn's CONTENT is a log** (`apps/harness/turn_access.py`). Every member sees that a turn
ran, when and how it ended; its prompt, result, ledger, raw transcript, caller context and
public transcript link are read by whoever started it, the agent's admins, the box that ran
it, and workspace admins — and, for a chat turn, by exactly the people who may read that chat.
Lists blank the content and set `content_hidden`; detail routes and the live turn socket 404.

Deliberately off the ladder:

- **`POST /{slug}/tasks/{ref}/actions` branches on `action`.** `approve`, `decline` and
  `reply` answer a task already on the board (the User tier); `dispatch` and `done` reshape or
  queue work and take `AGENT_WORK` (`_EDITOR_ACTIONS`). `tests/test_agent_acl_gates.py`
  asserts the two tiers partition `AgentTaskAction.ACTION_CHOICES`. Creating a task (which may
  carry `on_approve`) takes `AGENT_WORK` too — or the agent's own login — because approving it
  (the User tier) runs the prompt.
- **`POST /{slug}/bootstrap-report`** is gated on running the agent (`caller_runs_agent`),
  strictly tighter than any role.
- **`POST /api/agents/` (upsert)** takes the slug in the body, so it spells resolve-then-
  authorize out by hand: a non-member of an EXISTING agent's workspace gets 404 (a 403 would
  make it an oracle over every agent name, since `Agent.slug` is globally unique).
- **`GET /{slug}/credentials/resolve`** returns plaintext, bearer-only, to a box that may hold
  the agent (above) — or, since canopy#850, to the agent's owner or an admin (`Agent.is_admin`)
  resolving for a local Claude Code session. The audit event records which (`via: runner |
  admin`). A browser session only ever sees the masked status.
- **`GET /{slug}/credentials/access`** (member, session or bearer) answers the same gate for
  the caller without returning values: `{agent, credential_source, may_resolve, via, reason}`.
- **`PATCH /{slug}/credential-source`** (`1password` | `canopy-web`) is agent-admin, like the
  credentials and vault pointer it governs.

### A token can do whatever its person can

A personal access token, or an MCP client signed in through OAuth (every REST route is an
MCP tool), acts as its user with exactly that user's role — no more, and no less. There
are no web-app-only actions: inviting, changing roles, an agent's interface, credentials
and admins, the shared vault, Slack configuration all work over MCP for someone whose role
allows them. Browser-only gates were tried on 2026-10-02 and removed the same day: they
protected against a leaked token by taking from the MCP exactly the administration it is
most useful for. The one deliberate omission is `create_token`, which is not an MCP tool
(it would put a fresh raw PAT into a chat transcript); mint tokens in the web app or with
`/canopy:canopy-web-pat-mint`. The MCP sign-in consent page itself still requires a person,
or a token could sign itself in.

### Leaving a workspace takes what it carried

Every grant that hangs off membership is checked against **current** membership —
including an agent's owner, who used to stay its admin (credentials, interface, transfer),
keep an unconfined profile when messaging it, keep lending it a GitHub token and keep
being pushed about it after being removed. And removal sweeps the rest
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
3. The runner's own workspace (the behaviour before 2026-10-11).

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

**The harness** — schedules and turns — is gated too, and it had to be, because it is where
the table's promises actually cash out. A schedule is prompt text a runner later executes *as
the agent*, holding the agent's resolved credentials, and `run-now` means immediately; a turn
is the same thing without the wait. Both were membership-only, so a `viewer` could write a
prompt of their choosing and fire it at the fleet. Now:

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
MUTATION on `editor` in the row's workspace and every READ on membership (2026-10-02; until
then a `viewer` could do all of it, including approving a DDD gate and wiping a whole
feed with `{}`). Three shapes recur, all pinned by `tests/test_product_acl.py`:

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

**A row with no workspace is visible to nobody.** Projects (since retired), reviews, walkthroughs and origin
issues each had a `workspace IS NULL ⇒ any signed-in user` leg — the NULL-means-allow shape.
They fail closed now; `projects/0009` and `issues/0003` homed the stragglers first (a
walkthrough with no workspace is still readable by its share token).

## What is about to change

`Agent` currently carries **both** an `owner` (a person) and a `workspace` (a tenant), which
is why "who owns this agent" has had two answers. The agreed direction is that an agent's
**definition is shared** across tenants — one `echo`, and improving echo improves it
everywhere — while what you own is an **instance** of it. Forking produces a different agent,
not a detached instance.

When that lands, roles attach to the instance and this document's tiers get one meaning each
instead of two. The design, the 60-call-site tenancy risk it carries, and the phasing are in
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`.
