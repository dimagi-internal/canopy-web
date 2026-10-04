# Roles — who can do what, and what actually enforces it

> Every term here — workspace roles, agent roles (`owner` / `admin` / `member` /
> `contact` / `system`), access (`full` / `confined` / `none`), the interface,
> turn mode — is defined once, with THE rule that decides an agent turn, in
> [`access.md`](access.md). This page is about enforcement.

There are five tiers. Each contains the one below it.

| Tier | Enforced as | Can |
|---|---|---|
| **Link reader** | *no account* — a link you were sent | Read what was shared: a storyboard, a narrative, a walkthrough, a session transcript, the public explainer. |
| **Viewer** | workspace member, `viewer` | Interact with an agent through what its published interface offers members (none published → nothing): chat, answer a blocked question, decide an item, read the board. Read the turns you started. Cannot change what an agent *is*, or anything else. |
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
`member_role(...)` comparison or a rank check anywhere else. That is the lesson of adding
`admin` (2026-10-02): the same tier had been written five ways, and two of them silently
changed meaning when a role was inserted. The frontend mirrors the table in
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

## Workspaces nest — and only OWNERSHIP flows down

A workspace may have a `parent` (`Workspace.parent`), so an org sits above its divisions:
`dimagi` → `connect`, `strategy`, `operations`, `commcare`, `global-solutions`. The tree grants
exactly one thing: **an owner of a workspace is an owner of every workspace below it.** Nothing
else is inherited. A parent's editors and viewers have no access to a child, and a child's
members have none to its parent or siblings.

The narrowness is the design. `dimagi` is self-join for every `@dimagi.com` address, so if its
*editors* inherited, every employee could read — and, as editors, delete — every division's
agents, which is precisely what a division workspace exists to prevent.

- Resolved in `services.membership` (the sole authorizer): a direct `owner` row wins; otherwise
  an owner of any ancestor gets an **unsaved** membership with `inherited = True` and role
  `owner`, which outranks a weaker direct row. `is_member`, `member_role`,
  `user_workspace_slugs` and the pinned `/api/w/{ws}/` gate all read through it.
- Nesting is an owner's act on the **parent**: `POST /api/workspaces/` with `parent` requires
  owning the parent, and `PUT /api/workspaces/{slug}/parent` requires owning both the workspace
  and its new parent. Cycles are refused on save (422 over the API).
- A workspace with children cannot be deleted (409, naming them).

## The first tier has no account at all

The genuinely read-only role is **not** a membership role — it is having a link and no
account. `/storyboard/:slug`, `/narrative/:slug`, `/walkthrough/:id`, `/share/:token`,
`/ddd-release/:narrative/:runId` and `/about` serve anonymous readers.

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

Four ways, and the fourth is the only one you do not do yourself:

- **An invite.** An owner creates one at `/w/:workspace/settings/members`; you open the
  `/invite/:token` link. Canopy sends no email — the link is copied and sent by a human.
- **Self-join.** If a workspace lists your email domain in `self_join_domains`, you may
  join it yourself: `GET /api/workspaces/joinable` tells you which, `POST
  /api/workspaces/{slug}/join` does it. You land as `editor`. The first-run screen offers
  this when you belong to nothing yet.
- **Creating one.** Subject to `services.can_create_workspace`: an *invite-admitted* user
  holding no membership may not create a workspace, because otherwise invite-admission
  becomes transitively delegable — create a workspace, mint invites, and each new invitee
  clears the login gate too. `/api/me/` reports `can_create_workspace` so the UI never
  offers a button that 403s.
- **App-credential provisioning — GONE (2026-09-22).** An `AppCredential` used to carry a
  `provision_workspace`/`provision_role`, and exchanging a token on it enrolled the resolved
  user there. The whole door went with `/api/auth/token-exchange`: a connected site now
  vouches for a visitor with a *signed assertion*, and canopy resolves them to an account
  they already have or to a **contact** — which is not a membership and grants nothing
  (`apps/contacts/`). **No machine door into a workspace remains.** Existing rows keep
  `WorkspaceMembership.provisioned_by_app` as provenance of how they were created; nothing
  writes it any more.

`tests/test_no_implicit_enrolment.py` asserts these four are the only callers of
`ensure_member` outside the workspaces app, so a fifth cannot appear quietly.

**There is no automatic join.** Until 2026-09-12 there was: `auto_join_workspaces` added you
as `editor` to every workspace matching your email domain, and it ran *as a side effect of a
visibility check* — so merely looking at an agent silently granted write membership of a
tenant. That is why a "viewer" turned out to be an editor. It was removed across 31 call
sites in 19 files; the domain list survives, renamed to `self_join_domains`, and now means
"may join" rather than "is joined".

Nor is creating a row a way in — but it **was**, and that mattered more than auto-join did.
Five create endpoints (projects, shareouts, walkthroughs, reviews, issues) each held their own
copy of `ws = pinned or ensure_default_workspace(); ensure_member(ws, request.user)`. On the
flat `/api/…` mount nothing is pinned, so `ws` was the org default *whoever was calling*, and
`ensure_member` made them an **editor** of it as a side effect of the write. That was strictly
broader than self-join, which at least requires a matching email domain: an **invite-admitted**
user — the one deliberately kept off the domain allowlist, who correctly gets `[]` from
`/joinable` and 404 from `POST /join` — became an editor of `dimagi` by posting one shareout,
and from there passed every `editor` gate on the fleet. All five now resolve through
`wsvc.creation_workspace`, which only ever returns a tenant the caller is already in and
refuses with 422 when there is none.

Self-join cannot be used to escalate: it goes through `ensure_member`, which is create-only,
so an existing `viewer` who calls `join` stays a `viewer`. A workspace whose domains do not
match, and a workspace that does not exist, return the **same 404** — a 403 on the first
would let any signed-in user enumerate tenants and learn which domains they trust.

## What enforces the membership tiers

Every surface is gated now, through the table above; this section is about the shapes that
recur and the routes that deliberately sit off the ladder.

**The agents surface** (`apps/agents/api.py`):

- `_get_agent_or_404` — membership. Interaction and reads. A non-member gets 404, never 403,
  so the API never confirms an agent exists to someone who cannot see it.
- `_agent_for_write` — `AGENT_WORK` (editor and above). Reshaping: upsert, runner assignment,
  runner rules, turn mode, syncs, turns, work products, skills, task create/patch. Setting
  `auto` — on a routing rule, an actor route or the agent's switch — additionally needs
  `Agent.is_admin` (`_refuse_auto_unless_admin`, 2026-10-04).
- `_agent_for_admin` — `Agent.is_admin`: the agent's owner, a workspace owner, or an explicit
  `AgentAdmin`, each a CURRENT member. Credentials, the vault pointer, the interface, the
  Google mailbox mint, moving the agent to another workspace.

**A box holds an agent only if its owner is one of the agent's admins** (or the agent's own
login) — `agents.services.runner_may_hold_agent`. Claiming an agent turn, resolving its
credentials, the per-turn GitHub token, pinning a turn to a box (which also needs the pinner
to be an agent admin or that runner's admin — `access.may_pin_runner`), and every routing write
(runner list, source rules, actor routes) all ask it. Before 2026-10-02 an editor paired a
box, listed it (disabled) on an agent and read every secret, both vault keys and the owner's
GitHub token through `/credentials/resolve`; an assignment row was the whole trust boundary,
and the editor tier wrote it. A teammate whose own laptop runs an agent's work therefore needs
an admin grant on that agent.

**Only the box that claimed a turn reports on it** — start, finish, ledger events, transcript
lines (`harness.api._reporting_turn_or_404`). An unclaimed turn takes the editor tier.

**A turn's CONTENT is a log** (`apps/harness/turn_access.py`). Every member sees that a turn
ran, when and how it ended; its prompt, result, ledger, raw transcript, caller context and
public transcript link are read by whoever started it, the agent's admins, the box that ran
it, and workspace admins — and, for a chat turn, by exactly the people who may read that chat.
Lists blank the content and set `content_hidden`; detail routes and the live turn socket 404.

Deliberately off the ladder:

- **`POST /{slug}/tasks/{id}/commands` branches on `kind`.** `comment`, `accept` and
  `decline` decide an item already on the board (the User tier); `edit`, `reassign`, `done`
  and `dispatch` reshape or queue work and take `AGENT_WORK`. `tests/test_agent_acl_gates.py`
  asserts the two tiers partition `KIND_CHOICES`. Raising an ITEM that carries a `dispatch`
  spec takes `AGENT_WORK` too, because deciding it (the User tier) runs the prompt.
- **`POST /{slug}/bootstrap-report`** is gated on running the agent (`caller_runs_agent`),
  strictly tighter than any role.
- **`POST /api/agents/` (upsert)** takes the slug in the body, so it spells resolve-then-
  authorize out by hand: a non-member of an EXISTING agent's workspace gets 404 (a 403 would
  make it an oracle over every agent name, since `Agent.slug` is globally unique).
- **`GET /{slug}/credentials/resolve`** returns plaintext, bearer-only, to a box that may hold
  the agent (above). A browser session only ever sees the masked status.

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
because `dimagi` is self-join: without the sweep, a removed person clicks Join and every
dormant grant wakes up. Removing or demoting an org owner sweeps the divisions they reached
only by inheritance.

Listings read the tree too: members, an agent's roster, push recipients and the last-owner
guard all include owners of a parent workspace (`services.effective_memberships`), and a
direct owner may step down while a parent's owner still owns the workspace. Only a direct
owner may detach a workspace from its parent.

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

**The product surfaces** — projects + insights, walkthroughs, shareouts, reviews, DDD runs and
narratives, storyboards, origin issues, the event log, feedback dispositions — gate every
MUTATION on `editor` in the row's workspace and every READ on membership (2026-10-02; until
then a `viewer` could do all of it, including approving a DDD gate and wiping the insights
feed with `{}`). Three shapes recur, all pinned by `tests/test_product_acl.py`:

- **By-id writes resolve through the read gate first**: a non-member gets 404, a viewer who
  can already see the row gets 403, an editor writes.
- **Bulk writes are scoped by the WRITE set**, `perms.request_slugs_with(request,
  perms.CONTENT_WRITE)`, never by the read set — `clear_insights({})`, a narrative delete or visibility flip
  that spans workspaces touches only the rows in workspaces where the caller is an editor.
- **Some rows belong to a person within the tenant.** A walkthrough is changed by its uploader
  *while they are still an editor there*, or by a workspace owner; a shareout is replaced or
  cleared by the person who posted it (`Shareout.created_by`), or by a workspace owner.

The anonymous token reads (`?t=` on walkthroughs and storyboards, `link` reviews, storyboard
feedback) are a property of the link, not of a role, and are unchanged.

**A row with no workspace is visible to nobody.** Projects, reviews, walkthroughs and origin
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
