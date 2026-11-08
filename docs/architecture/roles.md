# Roles — who can do what, and what actually enforces it

> Every term here — workspace roles, agent roles (`owner` / `admin` / `editor` /
> `read_write` / `read_only`, plus `system`), the access list, access (`full` / `confined` /
> `refused`), read mode and host sites, turn mode — is defined once, with THE rule that decides an
> agent turn, in [`access.md`](access.md). This page is about enforcement.

## The tiers

Six tiers, each containing the one below it. What each workspace role may do is one table, `apps/workspaces/permissions.py::MINIMUM_ROLE` (every permission's comment says what it protects); the order is `models.py::WorkspaceMembership.ROLE_RANK`.

- **Link reader** is not a membership: a link you were sent and no account (see "The first tier has no account at all").
- **Viewer** reads the board and the turns they started and answers a blocked question. They talk to an agent only as far as its access list lets them (a `read_only` line opens read mode, a line naming a host site opens that one); being in the workspace opens nothing.
- **Editor** sends turns to an agent only if the agent's access list gives them `write` (`POST /api/harness/turns/` needs the workspace role **and** `turn_context.resolve_turn_context` giving them the agent; a line may make every such turn `manual`).
- **Workspace admin** runs the workspace (reads every LOG, manages members, integrations) and **holds no keys**: no credentials, no shared vault, no host-site registration (`permissions.OWN`).
- **Owner** holds the keys (`OWN`) and the workspace kill switch (`CONTROL`), and is the one told about the workspace's alerts and access requests (`services.py`, owners only, never superusers).
- **Superuser** is everything an owner is, in the workspace and every one below it, as oversight, and is not emailed. Owners and superusers manage each other (`permissions.may_manage_member`). The system superuser (`User.is_superuser`) is the same idea for the whole fleet, with no workspace access.

Membership may be lowered by an acting cap (`apps/workspaces/role_cap.py`): a turn or request run `acts_as` a lower role is judged at that role everywhere `services.membership` / `member_role` is read. A cap never raises.

**Agent roles and workspace roles share one engine, not one vocabulary.** `apps/common/rolesets.py::RoleSet` is the mechanism (a vocabulary of permissions, a table of named roles, implication closed when the table is built, `allows`, `holds`, rank). Agent roles are rows a workspace defines (`AgentRole`, [access-agent-roles-and-access-list.md § Configurable roles](access-agent-roles-and-access-list.md#configurable-roles); declared in `apps/agents/roles.py`); workspace roles stay fixed in code (`permissions.ROLES`, built from `MINIMUM_ROLE`). `tests/test_rolesets.py` holds both to the engine.

**Gates ask for a permission, never a role.** Every gate outside `apps/workspaces/` asks `perms.can(user, workspace, perms.<PERMISSION>)`. `tests/test_roles_named_only_in_workspaces.py` fails the build on a role constant, a `member_role(...)` comparison or a rank check anywhere else: a tier written by hand in a second place silently changes meaning the next time a role is inserted. The frontend mirrors the table in `frontend/src/lib/workspaceRoles.ts`, and a vitest reads `permissions.py` to keep the two equal.

**Every route declares its gate, at the route.** `@gate(...)` (`apps/api/route_gates.py`, which holds the closed vocabulary) sits above every view's `def`; `tests/test_every_route_declares_its_gate.py` fails on an operation in the OpenAPI schema whose view has none, so a new route is a decision about who may call it. A write a VIEWER may make must also give its reason (`viewer_write="..."`). Hand-written MCP tools, which are not routes, declare theirs in `route_gates.HAND_TOOL_GATES`. A route that changes the agent itself declares `@gate("agent-edit")` and asks `gates.agent_for_edit` (→ `access.may_edit_agent`): the agent's own `agent_edit` permission, not the workspace's `agent.work`.

A workspace admin manages members only **strictly below themselves** (`permissions.may_manage_member`): they neither touch nor mint a workspace admin or an owner. A workspace admin is not an agent admin either: `Agent.is_admin` (`apps/agents/models/agent.py`) is the agent's owner, a workspace OWNER, or an explicit `AgentAdmin` grant, each a CURRENT member, because running a workspace is not holding its agents' keys.

## Workspaces nest — and only SUPERUSER, OWNER and ADMIN flow down

A workspace may have a `parent` (`Workspace.parent`), so an org sits above its divisions. **A superuser, owner or admin of a workspace holds that role in every workspace below it** (`WorkspaceMembership.INHERITS_DOWN`). Nothing else is inherited: a parent's editors and viewers have no access to a child, and a child's members have none to its parent or siblings. Admin still holds no keys (`permissions.OWN`), so an inherited admin runs a division without holding its agents' keys.

The narrowness is the design: an org workspace may auto-approve access requests for an email domain (`Workspace.auto_approve_role`, `access_request_domains`), so if *editors* inherited, every such employee could read, and as editors delete, every division's agents, which is precisely what a division workspace exists to prevent. Do not widen `INHERITS_DOWN`.

- Resolved in `services.membership` (the sole authorizer; `tests/test_workspace_authorizer_is_sole_gate.py`): a direct `superuser` row is final; otherwise the best inheriting role on any ancestor, if it outranks the direct row, comes back as an **unsaved** membership with `inherited = True` at that role. Anything that would persist or delete such a row must check `inherited` first. `is_member`, `member_role`, `user_workspace_slugs` and the pinned `/api/w/{ws}/` gate all read through it.
- Nesting is an owner's act on the **parent**: creating with `parent` requires owning the parent, and `PUT /api/workspaces/{slug}/parent` requires owning both ends (`apps/workspaces/api.py`). Only a DIRECT owner may make a workspace a root, and an inherited owner may move it only where every current owner stays one. Cycles are refused on save (422). A workspace with children cannot be deleted (409).

## A read that names no workspace stays inside one organisation

Workspaces must be specified; workspace confusion must not be possible. More than one ROOT workspace exists (a family workspace beside the `dimagi` tree), and a caller in both must never get the **union** of their memberships from a flat `/api/…` list or an MCP tool called without `workspace`: that would land one tree's rows in the other's work. Writes follow the same principle through `creation_workspace`.

**The rule** is `services.read_scope`, reached through `request_workspace_slugs`:

- pinned (`/api/w/<ws>/…`, or the MCP `workspace` argument): that workspace **and every workspace below it you belong to** (`pinned_read_scope`);
- unpinned, every membership in ONE tree: the union, one org's own data;
- unpinned, memberships in MORE THAN ONE tree: **422** `workspace-required`, naming the caller's workspaces by tree and the `pins` that cover them (`covering_pins`). It fails closed: nothing is guessed;
- unpinned, a host site acting for its visitor (delegated token): the site's own workspace (`_read_pin`), because a site is one tenant's.

A tree is a root and everything under it (`services.tree_roots`; `WorkspaceOut.root`).

### A pinned read covers the workspaces below the pin

A pinned READ of workspace W returns W plus every DESCENDANT of W that the caller is a member of, directly or by inherited ownership. A pin never reaches UP, never reaches a descendant you are not in, and never joins separate roots (a pin to `dimagi` never returns another root's rows, whoever asks).

- **Writes pinned to W land in exactly W** (`creation_workspace`).
- **Bulk writes pinned to W touch exactly W** (`services.write_scope` / `request_write_slugs`, and `perms.request_slugs_with`, which reads it): "clear every shareout" pinned to `dimagi` leaves `connect`'s alone, though the pinned read of the same feed shows both. A permission-gated LIST (the event log's `LOGS_READ`) reads `perms.request_read_slugs_with`, which takes the subtree.
- **By-id gates** read `member_scope` (pinned: the pin and the caller's workspaces below it), the same span a pinned list has, so a row a pinned list shows is one the by-id gate admits; each object's role check is its own. A row from a child workspace is opened under ITS workspace (list items carry `workspace` / `workspace_slug`).
- **Per-workspace settings and admin pages stay exactly W.** Members, invites, access settings and requests, the shared vault, the runner order, retention and admission rules are read and written by `/api/workspaces/<slug>/…` and gated by `membership` / `perms.can` on that one slug; none reads the list scope.
- **Clients learn the rule** from `pinned_read_covers_descendants` on `GET /api/workspaces/` and from the `pins` of the refusal; a client that sees no flag keeps fanning out.

### A user-level view reads each organisation on its own

A page that is about the PERSON rather than one workspace (Home, Supervisor, the IDE, the chat home) reads a **user-level view**, `/api/me/<list>/`: the ordinary list run once per organisation the caller is in (`services.each_organisation`: one pinned read per `covering_pins` pin, each under that list's own read rule and gate), concatenated and re-sorted. No query ever spans two trees, and no client fans out, merges or dedupes. The routes are the callers of `each_organisation` (`apps/agents/api_fleet.py`, `apps/attention/api.py`, `apps/canopy_sessions/api_read.py`). A new page off any one workspace reads a user-level view, adding one (a thin route over `each_organisation`) when its list has none — never a client-side loop over pins.

**What the read rule does not touch:**

- **By-id reads.** Opening one object you belong to mixes nothing, so gates of the form `obj.workspace_id in …` read `request_member_slugs` / `member_scope` (never refused). A huddle or thread opened by id reads within *its own* tree (`tree_scope`); `tree_scope` reaches UP from a pinned child, so lists never use it.
- **Who you are and where you are**: `/api/me/`, `GET /api/workspaces/`, invites and access requests, "my memory".
- **The fleet's plumbing**: runner heartbeat, claim, schedule sync, mailbox probe (`runner_tenant_slugs`, `inbound.runner_mailboxes`), the runner list (`_runner_read_q`), routing, the per-user MCP agent-tool registry, and the realtime supervisor socket.

The web client mirrors the refusal in `frontend/src/api/workspaceRetry.ts`: a flat GET refused this way is answered from PINNED reads, so the server never answers a cross-organisation query. Do not retry a root page (`/supervisor`, `/sessions`, `/activity`, `/schedules`: the fleet views) pinned to one membership: that hides every other workspace's agents.

## The first tier has no account at all

The genuinely read-only role is **not** a membership role: it is having a link and no account. The scoped viewers under `/w/:workspace/` (`apps/common/middleware.py::_SCOPED_VIEWER` lists them) and the public site serve anonymous readers.

Two things enforce it together, and **both are required**:

1. `auth=None` on the route (or, for pages, nothing to authenticate).
2. An entry in `PUBLIC_PATH_PREFIXES` in `apps/common/middleware.py`. That middleware is **default-deny**, so `auth=None` alone still bounces an anonymous caller to Google.

The failure modes tell you which half you missed: a **page** path without the allowlist entry gives a **302** to sign-in; an **`/api/`** path gives a JSON **401**. `tests/test_public_routes_reachable.py` pins each public surface, because `config/settings/test.py` sets `REQUIRE_AUTH = False`, which makes the login middleware inert for the whole suite; without `@override_settings(REQUIRE_AUTH=True)` an allowlist entry could be deleted with CI still green.

Beside `PUBLIC_PATH_PREFIXES`, the login middleware exempts the scoped viewers, the per-artifact read APIs (`_is_artifact_api`) and the flat artifact addresses (`_is_flat_artifact`, which serve only a 404). The API layer enforces the share token underneath; which pages these are: [pages-public-and-personal.md § Scoped public viewers](pages-public-and-personal.md#scoped-public-viewers-anonymous-with-a-token).

## How you get into a workspace

Every way in (invite, approved access request, creating a workspace, an agent's own login) is described once in [access.md § Getting into a workspace](access.md#getting-into-a-workspace). `apps/workspaces/tests/test_access_requests.py` asserts the only code that can create a `WorkspaceMembership` is `services._grant`, `services.ensure_member` and workspace creation, so a new way in cannot appear quietly; `tests/test_no_implicit_enrolment.py` pins the same for `ensure_member` callers.

**Never grant membership as a side effect.** Not of a visibility check, and not of a write: a create resolves its workspace through `wsvc.creation_workspace`, which only ever returns a tenant the caller is already in and refuses with 422 when there is none. Calling `ensure_member` from a create path would let an invite-admitted user become an editor of the org default by posting one row.

**Every member is a person or an agent's own login.** An automated sender (CloudWatch alarm mail, CI) is not a member: it is one agent's inbound-mail *trigger*, firing with the authority of whoever set it up ([access.md § Triggers](access.md#triggers-what-starts-an-agent-on-its-own)).

## What enforces the membership tiers

Every surface is gated through the permission table; this section is the shapes that recur and the routes that deliberately sit off the ladder.

**Every agent-by-slug route** (agents, harness, agent runs, capabilities, threads, mail, check-ins) asks `apps/agents/gates.py`, never a local copy (`tests/test_agent_gates_not_copied.py` fails on an import of a private `apps.agents.api` helper from another module):

- `agent_for_read`: membership (`wsvc.request_member_slugs`). A non-member gets 404, never 403, so the API never confirms an agent exists to someone who cannot see it.
- `agent_for_work`: `AGENT_WORK` (editor and above): reshaping, runner assignment, runner rules, turn mode, syncs, turns, skills, project and task create/patch. Setting `auto` on a routing rule, an actor route or the agent's switch additionally needs `Agent.is_admin` (`route_refusals.refuse_auto_unless_admin`).
- `agent_for_admin`: `Agent.is_admin`. Credentials, the vault pointer, read mode and host sites, the Google mailbox mint, moving the agent to another workspace.
- `allow_own_login=True` (agent runs, run documents, capabilities) lets the agent's own login (`Agent.is_own_login`) stand in for the editor role. It is still a member: `agent_for_read` runs first and has no own-login exception. `Agent.user` is one-to-one with the agent, so it reaches only that agent's own rows.

**A box holds an agent only if its owner is one of the agent's admins** (`agents/holding.py::runner_may_hold_agent`): do not let a runner-assignment row be the trust boundary, because editors write those rows. **A box serves only its owner's workspaces it is enrolled in** (`harness/routing_orders.py::runner_tenant_slugs`). Both rules: [runners.md § Which workspaces a runner serves](runners.md#which-workspaces-a-runner-serves-the-tenant-gate) and [§ Which agents a box may hold](runners.md#which-agents-a-box-may-hold).

**Only the box that claimed a turn reports on it** (`harness/api.py::reporting_turn_or_404`). An unclaimed turn takes the tier of cancelling it (`harness/api_turns.py::_cancel_refusal`).

**A turn's CONTENT is a log** (`apps/harness/turn_access.py`, whose docstring lists who reads it). Every member sees that a turn ran, when and how it ended; its prompt, result, ledger, raw transcript and caller context are read only by the people the docstring names, and for a chat turn by exactly the people who may read that chat. A sensitive turn's content is nobody's through these routes. Lists blank the content and set `content_hidden`; detail routes and the live turn socket 404. Every route asks `can_read_turn_content`.

Deliberately off the ladder:

- **`POST /{slug}/tasks/{ref}/actions` branches on `action`** (`apps/agents/api_board.py::act_and_record`). `approve`, `decline` and `reply` answer a task already on the board (the User tier); `nudge` and `done` take `AGENT_WORK` (`_EDITOR_ACTIONS`), with `done` also open to the task's assignee. `tests/test_agent_acl_gates.py` asserts the two tiers partition `AgentTaskAction.ACTION_CHOICES`. Creating a task (which may carry `on_approve`) takes `AGENT_WORK` too, because approving it (the User tier) runs the prompt.
- **`POST /{slug}/bootstrap-report`** is gated on running the agent (`caller_runs_agent`), strictly tighter than any role.
- **`POST /api/agents/` (upsert)** takes the slug in the body, so it spells resolve-then-authorize out by hand: a non-member of an EXISTING agent's workspace gets 404 (a 403 would make it an oracle over every agent name, since `Agent.slug` is globally unique).
- **`GET /{slug}/credentials/resolve`** (`apps/agents/api_credentials.py`) returns plaintext, bearer-only, to a box that may hold the agent, or to the agent's owner or an admin resolving for a local Claude Code session; the audit event records which. A browser session only ever sees the masked status. **`GET /{slug}/credentials/access`** answers the same gate without returning values.

### A view that judges many pairs reads once

A view that asks the same question of many (person, workspace) or (person, agent) pairs (the agent topology, an agent's roster, a list filtered by what the viewer may do) reads the facts once and answers every pair from them. It never calls `member_role`, `perms.can`, `Agent.is_admin` or `turn_context.resolve_turn_context` per pair in a loop: each reads memberships and walks the tree, so a loop is N² queries.

- **One person, many workspaces:** `perms.slugs_with(user, permission, within=…)`.
- **Many people, their roles:** `wsvc.member_roles_of(users)`, then `perms.role_allows`.
- **Many people × agents:** `access.Standings(users, agents)`, passed to `turn_context.for_asker(…, standing=)`, `Standings.is_admin`, `Agent.is_admin(user, role=, granted=)` or `access.roster(…)`. Everyone who administers one agent: `Agent.admin_user_ids()`.
- **Many runners:** `routing.runner_tenant_slugs_of(runners)` (prefetch `enrollments`), `runners.administered_runner_ids(user, runners)`.
- **The tree:** `ancestor_chains({slug: parent})` and `wsvc.descendant_map(slugs)`, not `ancestor_slugs()` / `descendant_slugs()` per workspace.

Each single check is the bulk read of one (`runner_tenant_slugs(r)` is `runner_tenant_slugs_of([r])[r.pk]`), so a rule has one implementation. A bulk read only supplies inputs, read per request and dropped with it: nothing is cached. `tests/test_bulk_authz_parity.py` holds every bulk read to the one-at-a-time answer; `apps/workspaces/tests/test_agent_topology.py` and `tests/test_fleet_reads_do_not_grow.py` pin query counts that do not grow with agents or runners.

### A token can do whatever its person can

A PAT, or an MCP client signed in through OAuth, acts as its user with exactly that user's role: there are no web-app-only actions. The rule and its exceptions are in [mcp-surface.md](mcp-surface.md) (the "Exposure is the default" paragraph).

### Leaving a workspace takes what it carried

Every grant that hangs off membership is checked against **current** membership, including an agent's owner: once removed they are not its admin (credentials, read mode and host sites, transfer), get no unconfined profile when messaging it, lend it no GitHub token and are not pushed about it. Removal also sweeps the rest from one registry (`apps/workspaces/departure.py`; each app registers a revoker from `ready()` in its own `departure.py`; `tests/test_departure_revokers.py` lists the expected names): AgentAdmin, RunnerAdmin and chat participant rows in that workspace, lent GitHub tokens, their box on its agents' runner lists, agent ownership (cleared, with a `warn` Event), and open chat sockets there. Register a revoker when you add a grant that hangs off being in a workspace. The reason: an org workspace can auto-approve access requests, so without the sweep a removed person asks again and every dormant grant wakes up. Removing or demoting an org owner sweeps the divisions they reached only by inheritance.

Listings read the tree too: members, an agent's roster, push recipients and the last-owner guard include owners of a parent workspace (`services.effective_memberships`), and a direct owner may step down while a parent's owner still owns the workspace.

### Which workspace a session lives in — and moving it

A chat session carries its own tenant (`Session.workspace`), and the session ACL (`apps/canopy_sessions/access.py`) is **tenant-first**: you must be a member of that workspace before any other rule applies, and then be one of the people the session belongs to (`visible_session_q`); being in the same workspace shows you nothing. So *where a session is filed* decides which members could ever be added to it. How a new runner-discovered session is filed, who may set the default and who may move a session (never an agent's: 422) are in the `apps/canopy_sessions/filing.py` module docstring. Moving is tenant-first: afterwards only members of the target see it, and participants who are not members of the target lose their row (moving back does not restore it). Rows in other products that merely *link* to a session keep their own workspace; the link grants nothing.

**The harness** — schedules and turns — is gated because it is where the table's promises cash out. A schedule is prompt text a runner later executes *as the agent*, holding the agent's resolved credentials, and `run-now` means immediately; a turn is the same without the wait. Gated on membership alone, a `viewer` could write a prompt of their choosing and fire it at the fleet. So:

- Schedule create / update / delete / run-now require the `AGENT_WORK` tier (an inbound-mail or `auto` trigger is an agent admin's), enforced inside `schedule_services._resolve_agent`, **not** in the Ninja handlers, because the MCP tools call that service layer directly and a gate in the handlers would be one the MCP surface walks around. `list` and `preview` stay at membership.
- `POST /api/harness/turns/` and turn cancel require `AGENT_WORK` on the **agent** branch. Project and session turns do not: a session turn is what a chat send produces, and talking to an agent is what the interaction tier is for. The carve-out is structural: `turn_targets_agent_xor_project_xor_session` (`apps/harness/models/turn.py`) means a session turn carries no agent FK at all.

**The product surfaces** (walkthroughs, shareouts, reviews, DDD runs and narratives, storyboards, origin issues, the event log, feedback dispositions) gate every MUTATION on `CONTENT_WRITE` in the row's workspace and every READ on membership: a `viewer` must not approve a DDD gate or wipe a whole feed with `{}`. Three shapes recur, all pinned by `tests/test_product_acl.py`:

- **By-id writes resolve through the read gate first**: a non-member gets 404, a viewer who can already see the row gets 403, an editor writes.
- **Bulk writes are scoped by the WRITE set**, `perms.request_slugs_with(request, perms.CONTENT_WRITE)`, never by the read set: `clear_shareouts({})`, a narrative delete or visibility flip that spans workspaces touches only rows in workspaces where the caller is an editor.
- **Some rows belong to a person within the tenant.** A walkthrough is changed by its uploader *while they are still an editor there*, or by a workspace owner (`apps/walkthroughs/api.py::_may_write`); a shareout is replaced or cleared by the person who posted it (`Shareout.created_by`), or by a workspace owner.

The anonymous token reads (`?t=` on walkthroughs and storyboards, `link` reviews, storyboard feedback) are a property of the link, not of a role.

**A row with no workspace is visible to nobody.** Reviews, walkthroughs and origin issues fail closed on a NULL workspace; never give them a `workspace IS NULL ⇒ any signed-in user` leg (a walkthrough with no workspace is still readable by its share token).
