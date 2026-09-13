# Roles — who can do what, and what actually enforces it

There are four roles. Each contains the one below it.

| Role | Enforced as | Can |
|---|---|---|
| **Viewer** | *no account* — a link you were sent | Read what was shared: a storyboard, a narrative, a walkthrough, a session transcript, the public explainer. |
| **User** | workspace member, `viewer` | Interact with an agent: chat, answer a blocked question, decide an item, read the board. Cannot change what an agent *is*. |
| **Author / executor** | workspace member, `editor` | Create and edit agents, run turns, publish work products, edit schedules, assign runners. |
| **Administrator** | workspace member, `owner` | Members and invites, agent credentials, the shared vault, inbound configuration, deleting an agent or the workspace. |

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

Three ways, and there is deliberately no fourth:

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

**There is no automatic join.** Until 2026-09-12 there was: `auto_join_workspaces` added you
as `editor` to every workspace matching your email domain, and it ran *as a side effect of a
visibility check* — so merely looking at an agent silently granted write membership of a
tenant. That is why a "viewer" turned out to be an editor. It was removed across 31 call
sites in 19 files; the domain list survives, renamed to `self_join_domains`, and now means
"may join" rather than "is joined".

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
  rules, turn mode, syncs, turns, work products, skills, tasks, bootstrap reports.
- `_agent_for_admin` — `owner` only. Credentials, the vault pointer, credential deletion.

Both gated helpers resolve through `_get_agent_or_404` **first**, so the 404-not-403 property
survives the role check. There is a test pinning exactly that; it is the property most likely
to be lost in a refactor.

Credentials are owner-only for a specific reason: they are the keys a runner resolves
everything else from, so writing one is equivalent to controlling the agent end to end. Note
that *reading* them was never possible through the API — `AgentCredentialStatusOut` is a
masked view, "booleans and timestamps, NEVER values" — so the exposure this closed was
substitution and denial, not disclosure.

**Workspace administration** (`apps/workspaces/api.py`) is `owner`-only throughout, via
`_require_role`: members, invites, the shared vault, deletion.

**Elsewhere, a write generally requires only membership.** Projects, walkthroughs, shareouts
and reviews check that you are in the tenant, not what role you hold. So today the
`user`/`author` distinction in the table above is real on the agents surface and aspirational
on the rest. That is a known gap, not a claim — do not read the table as uniformly enforced.

## What is about to change

`Agent` currently carries **both** an `owner` (a person) and a `workspace` (a tenant), which
is why "who owns this agent" has had two answers. The agreed direction is that an agent's
**definition is shared** across tenants — one `echo`, and improving echo improves it
everywhere — while what you own is an **instance** of it. Forking produces a different agent,
not a detached instance.

When that lands, roles attach to the instance and this document's tiers get one meaning each
instead of two. The design, the 60-call-site tenancy risk it carries, and the phasing are in
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`.
