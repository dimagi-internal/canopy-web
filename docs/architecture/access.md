# Access — the glossary and the one rule

Every word canopy uses about who may do what has exactly one meaning, and it is
defined here. Code docstrings point at this page instead of re-defining terms
(`apps/agents/access.py`, `apps/harness/caller_context.py`,
`apps/workspaces/permissions.py`, `apps/agents/interface.py`,
`apps/harness/turn_mode.py`). Owner decisions of 2026-10-04: reuse existing words,
one meaning per word, no new terms.

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs,
  invite people, create content, reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/access.decide(agent, who,
*, verified, origin, capability=None)`, which returns a `Decision(role, access,
capability, may_request_auto, may_pin_runner, manual_only, basis, reason)`. Every
door that makes an agent do work asks it.

## Workspace roles

`viewer < editor < admin < owner` (`WorkspaceMembership.ROLE_RANK`). What each may
do is one table, `apps/workspaces/permissions.py`, keyed by capability; nothing
outside `apps/workspaces/` names a role. Full detail: `docs/architecture/roles.md`.

| Role | Holds | In plain words |
|---|---|---|
| **viewer** | `read` | Reads the workspace; talks to an agent through what its interface offers members. |
| **editor** | + `content.write`, `agent.work`, `session.drive`, `events.write` | Makes things; **edits agents and dispatches turns to them** — those turns always run `manual`. |
| **admin** | + `logs.read`, `members.manage`, `integrations` | **Runs the workspace**: every log, members below admin, the integrations. **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own` | **The keys**: the shared vault, the Slack app, connected sites, deleting the workspace — and is **every agent's admin**. |

**Workspace permissions** (the capability names): `read`, `content.write`,
`agent.work`, `session.drive`, `events.write`, `logs.read`, `members.manage`,
`integrations`, `own`.

## Agent roles

What a person IS to one agent (`Decision.role`, the envelope's `relationship`):

| Agent role | Who |
|---|---|
| **owner** | `Agent.owner`, while still a member of the agent's workspace. |
| **admin** | A workspace **owner**, or an explicit `AgentAdmin` grant (still a member). `Agent.is_admin` answers owner-or-admin. |
| **member** | Any other member of the agent's workspace (viewer, editor or workspace admin). |
| **contact** | Anyone who is not a member: an emailer, a widget visitor, a canopy user from another tenant, someone unidentified. (Envelope VERSION 1 said `caller`.) |
| **system** | Not a person: canopy itself (a schedule, a drill), the agent's OWN canopy login (`Agent.user`), an approved board item's dispatch. Another agent's login is not `system` — it is graded like anyone. |

An agent's **admins** (owner + admin) hold its keys: credentials, the vault
pointer, GitHub delegation, the interface, its admin list, and routing in `auto`.

## Access levels

What a turn may reach (`Decision.access`; the roster's "Can reach"):

* **full** — the agent's whole profile.
* **confined** — one capability of its published interface, and nothing else
  (the envelope's `profile: confined`; VERSION 1 said `restricted`).
* **none** — refused. The turn is written `cancelled` with the reason and what the
  asker CAN do, or (over HTTP) the request gets a 403 saying so.

## The interface

An agent's **published interface** (`apps/agents/interface.py`, live state on
canopy-web, never a repo file) says what it offers people who are not its admins:

* **`full:`** — caller classes that get the whole agent, e.g. Eva's
  `full: [member, contact@dimagi.com:verified]`.
* **`capabilities:`** — named doors, each with its **`callers:`** (which classes
  may use it) and its profile (`tools`, `bash`, `read_paths`, `write_paths`,
  `entry`, `pages`, `sites`, `ceiling`). `ask` is the free-form door every channel
  falls back to.
* **caller classes** — `member` (a workspace member who is not an agent admin),
  `contact` (known, not a member), `unknown` (nobody established who); narrowed by
  `@domain.tld` (exactly that domain) and `:verified` (THIS message is verified).
* `callers_default` is always `none`.

## The rule (the decision table)

Asked in this order; the first row that matches decides.

| # | Who | Access | Mode | May request `auto` | May pin a runner |
|---|---|---|---|---|---|
| 1 | **owner / admin / system** | `full` | the routing ladder | owner/admin, on a verified credential | owner/admin: any runner that may hold the agent |
| 2 | a writer in an admin's own session (`session_writer`) | `full` | the routing ladder | no | only a runner they administer |
| 3 | anyone a **`full:`** rule names | `full` | the routing ladder | no | only a runner they administer |
| 4 | **workspace editor or above** who is not an agent admin | `full` | **always `manual`** | no | only a runner they administer |
| 5 | viewer / contact, **no published interface** | `none` | — | — | — |
| 6 | viewer / contact whose class a capability lists | `confined` to it | the routing ladder | no | — |
| 7 | anyone else | `none` | — | — | — |

* Row 3 before row 4 keeps an owner's chosen domain-wide setup exactly as it was:
  Eva's members get her whole through `full: [member]`, in her routing's mode.
* Row 4 (the **editor tier**): an editor may EDIT the agent (its definition and
  settings, as `agent.work` always allowed) and DISPATCH work to it ("improve
  yourself"), but every turn they start runs `manual` — outbound or irreversible
  actions wait for an admin — and they may not set `auto` anywhere: not on a
  dispatch, not on a routing rule or actor route, not on the agent's own switch.
* A **runner admin** is a runner's owner or a `RunnerAdmin` grant. A pinned box
  must also be able to hold the agent at all (`runner_may_hold_agent`: its owner is
  one of the agent's admins). Chosen 2026-10-04: pinning is for an **agent admin
  OR a runner admin**, consistent with actor routes, which already required the
  runner admin.

**Where it is asked.** `harness.services.enqueue_turn` (`_apply_capability`) for
every door — `POST /api/harness/turns/`, a canopy-web chat send, Slack intake and
email intake (after the existing sender triage: a blocked contact never reaches
the rule; a member is recognised by mail only on THIS message's DMARC); the
harness API also asks it up front to answer 403 with a reason; `turn_mode.for_turn`
at every claim (the editor tier's `manual`); the caller envelope (`profile`,
`granted_by`); the MCP capability tools (`offered_to`); the access roster and the
agent topology.

## Turn mode

* **`manual`** — every outbound or irreversible action (send, publish, push,
  merge, deploy) needs the agent's owner/admin first. The default.
* **`auto`** — the agent acts and reports.

Decided at claim, top rung first (`apps/harness/turn_mode.py`): a dispatch's
requested mode (`auto` only from an agent admin) → the editor tier (`manual`) →
an actor routing rule (an `auto` naming a person needs a verified message) → a
source routing rule → the agent's own switch (`Agent.turn_mode`). Setting `auto`
on any rung is for the agent's admins.

## The caller envelope

What the runner hands the agent beside each turn (`apps/harness/caller_context.py`,
VERSION 2): `who`, `verified` (THIS message), `relationship` (the agent role),
`profile` (`full` | `confined`), `granted_by` (`owner` | `admin` | `system` |
`editor` | `session:<role>` | `full:<rule>` | `capability:<name>` | `refused` |
`no-interface` for a turn with no agent), `capability` (the confined profile),
`turn_mode` (`{mode, basis}`), and:

* **`ship_grant`** — when ANOTHER agent's verified login that is this agent's
  owner or admin dispatched the turn at it, push / PR / merge in this agent's OWN
  repo are pre-approved even in `manual`. Nothing else is.

Readers accept both spellings: `relationship: caller` means `contact`, `profile:
restricted` means `confined` (`normalize_relationship`, `normalize_profile`; the
runners and the canopy plugin's hooks do the same). A runner gets confined turns
only if it reports `envelope >= 2` (its code reads `confined`) and `profiles >= 3`
(its guard confines writes) on every heartbeat.
