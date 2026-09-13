# Roles — who can do what, and what actually enforces it

There are four roles. Each contains the one below it.

| Role | Enforced as | Can |
|---|---|---|
| **Viewer** | *no account* — a link you were sent | Read what was shared: a storyboard, a narrative, a walkthrough, a session transcript, the public explainer. |
| **User** | workspace member, `viewer` | Interact with an agent: chat, answer a blocked question, decide an item, read the board. Cannot change what an agent *is*. |
| **Author / executor** | workspace member, `editor` | Create and edit agents, run turns, publish work products, edit schedules, assign runners, **delete an agent**. |
| **Administrator** | workspace member, `owner` | Members and invites, agent credentials, the shared vault, inbound configuration, deleting the workspace. |

The three membership roles are a real total order in the code —
`WorkspaceMembership.ROLE_RANK` (`apps/workspaces/models.py`) is `{viewer: 0, editor: 1,
owner: 2}` and is the single place that ordering lives.

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

- **An invite.** An owner creates one at `/w/:workspace/members`; you open the
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
- **App-credential provisioning.** An owner configures an `AppCredential`
  (`apps/tokens/models.py`) with a `provision_workspace` and `provision_role`; exchanging a
  token on it enrols the resolved user there. This is the machine door — it exists so a
  trusted integration can admit its own users — and it is deliberately *not* a way a caller
  can let themselves in: the workspace comes **only** from the credential's server-side row,
  never from the request, an owner set it up on purpose, and every grant it makes is audited.
  A credential with no `provision_workspace` grants nothing at all.

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

Role checks are **not** uniform across the app, and it is worth knowing where they are real.

**The agents surface** (`apps/agents/api.py`) is the surface with a genuine three-tier gate:

- `_get_agent_or_404` — membership. Interaction and reads. A non-member gets 404, never 403,
  so the API never confirms an agent exists to someone who cannot see it.
- `_agent_for_write` — `editor` or `owner`. Reshaping: upsert, runner assignment, runner
  rules, turn mode, syncs, turns, work products, skills, task create/patch.
- `_agent_for_admin` — `owner` only. Credentials, the vault pointer, credential deletion.

Two endpoints on that surface sit deliberately off the ladder:

- **`POST /{slug}/tasks/{id}/commands` branches on `kind`.** `comment`, `accept` and
  `decline` are *deciding an item already on the board*, which is what the User tier is for;
  `edit`, `reassign`, `done` and `dispatch` reshape or queue work and require `editor`. The
  split matters because `kind: "edit"` reaches the same mutation as the `editor`-gated
  `PATCH /tasks/{id}/` — gating one and not the other left the gate with a door beside it.
  `tests/test_agent_acl_gates.py` asserts the two tiers partition `KIND_CHOICES`, so a new
  kind cannot default into the viewer tier unnoticed.
- **`POST /{slug}/bootstrap-report` is gated on pairing a live runner, not on a role**
  (`services.caller_runs_agent`), which is strictly tighter. A role check on top would add
  no security and would let readiness reporting start failing because a runner's pairing
  human happens to hold `viewer` — and a machine saying "I could not materialize this" is
  the last signal you want to lose.

Both gated helpers resolve through `_get_agent_or_404` **first**, so the 404-not-403 property
survives the role check. There is a test pinning exactly that; it is the property most likely
to be lost in a refactor.

`POST /api/agents/` (upsert) is the one route that cannot resolve through that helper — the
slug arrives in the **body** and may name nothing yet — so it spells the ordering out by hand.
It gates on the *existing* agent's own workspace, which is what stops a caller reshaping
another tenant's agent by defaulting into their own; and a non-member of that workspace gets
**404**, not 403, because `Agent.slug` is globally unique and a 403 would turn the route into
an oracle over every agent name in the fleet (201 = free, 403 = exists somewhere you cannot
see). On a genuine create the 403 stands: there is nothing to leak, and the caller's own role
in the target workspace is something they are entitled to learn.

Credentials are owner-only for a specific reason: they are the keys a runner resolves
everything else from, so writing one is equivalent to controlling the agent end to end. The
exposure this closed was substitution and denial rather than disclosure — but be precise
about why, because "credentials cannot be read" is not true. They cannot be read **from a
browser session**: `AgentCredentialStatusOut` is a masked view, "booleans and timestamps,
NEVER values". `GET /{slug}/credentials/resolve` does return plaintext, and it is gated on a
different axis entirely — bearer-token only, and only for a caller pairing a live runner the
agent's routing could actually send work to (`services.caller_runs_agent`). The trust
boundary there is "the box that runs this agent", not "a senior enough human".

**Workspace administration** (`apps/workspaces/api.py`) is `owner`-only via `_require_role`:
members, invites, the shared vault, deletion. Two routes in that file are deliberately not,
and both are load-bearing: `GET /joinable` and `POST /{slug}/join` are how a non-member gets
in at all (owner-gating them would make self-join unreachable), and inbound configuration is
owner-gated through its own `_owner_workspace_or_404` rather than `_require_role`.

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

**Elsewhere, a write requires only membership.** Projects, walkthroughs, shareouts and reviews
check that you are in the tenant, not what role you hold — creating one is not a way to *join*
a tenant any more (above), but a `viewer` who is already in one can still create there. So the
`user`/`author` distinction is enforced on the agents and harness surfaces and aspirational on
the product surfaces. That is a known gap, not a claim — do not read the table as uniformly
enforced, and if you are granting someone `viewer` on the strength of it, that is the sentence
to read.

## What is about to change

`Agent` currently carries **both** an `owner` (a person) and a `workspace` (a tenant), which
is why "who owns this agent" has had two answers. The agreed direction is that an agent's
**definition is shared** across tenants — one `echo`, and improving echo improves it
everywhere — while what you own is an **instance** of it. Forking produces a different agent,
not a detached instance.

When that lands, roles attach to the instance and this document's tiers get one meaning each
instead of two. The design, the 60-call-site tenancy risk it carries, and the phasing are in
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`.
