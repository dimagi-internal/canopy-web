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
| **admin** | + `logs.read`, `members.manage`, `integrations`, `runners.route`, `retention.manage` | **Runs the workspace**: every log, members below admin, the integrations, the default runner order, how long chat and turn content is kept. **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own` | **The keys**: the shared vault, the Slack app, connected sites, deleting the workspace — and is **every agent's admin**. |

**Workspace permissions** (the capability names): `read`, `content.write`,
`agent.work`, `session.drive`, `events.write`, `logs.read`, `members.manage`,
`integrations`, `runners.route`, `retention.manage`, `own`.

## Getting into a workspace

Owner decision of 2026-10-04: nothing automatic, except that people at the org's
domain may **request an invitation**. A person becomes a member in exactly three ways,
and only `services._grant` (the first two) and workspace creation write a
`WorkspaceMembership`:

* **An invite.** An **admin** or **owner** invites an email address at a role they
  may grant (`permissions.may_manage_member`; **viewer** unless they choose
  otherwise). Canopy emails the `/invite/:token` link; accepting grants that role
  (never lowering one already held).
* **An approved access request.** A person whose login email's domain is in the
  workspace's **`access_request_domains`** may ask (`POST
  /api/workspaces/{slug}/access-requests`, with an optional note) — the first-run
  screen offers "Request an invitation to <workspace>". Every **admin** and
  **owner** — owners of a parent workspace included — is emailed (and pushed) a
  deep link to the request's page,
  `/w/:workspace/settings/access-requests/:id`, where one of them **approves** at
  **viewer**, **editor** or **admin** (no higher than they may grant) or
  **denies**, with an optional reason. The requester is emailed the outcome. A
  notification failure never fails the request: it is stored on the request and
  written to the event log (`source=workspaces.access`, `warn`).
* **Creating a workspace** makes you its **owner**.

**Auto-approve** (`Workspace.auto_approve_role`, owner-set at
`PUT /api/workspaces/{slug}/access-settings`): blank is off, and every request
waits for a person. Set to **viewer** or **editor** (never above), a request is
approved the moment it is made — still an ordinary request record (`status:
approved`, `auto: true`, `decided_by: null`), and every admin and owner is still
emailed the link, from which they can change the person's role or remove them.
`dimagi` is set to **editor** for now while it bootstraps (`workspaces/0012`);
every other workspace is off. Turning it off or lowering it is a settings
change. The domains themselves are server-set, never client input.

## Agent roles

What a person IS to one agent (`Decision.role`, the envelope's `relationship`):

| Agent role | Who |
|---|---|
| **owner** | `Agent.owner`, while still a member of the agent's workspace. |
| **admin** | A workspace **owner**, or an explicit `AgentAdmin` grant (still a member). `Agent.is_admin` answers owner-or-admin. |
| **member** | Any other member of the agent's workspace (viewer, editor or workspace admin). |
| **contact** | Anyone who is not a member: an emailer, a widget visitor, a canopy user from another tenant, someone unidentified. (Envelope VERSION 1 said `caller`.) |
| **system** | Not a person: canopy itself (a schedule, a drill), the agent's OWN canopy login (`Agent.user`), an approved task's `on_approve` dispatch. Another agent's login is not `system` — it is graded like anyone. |

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
* Row 1's `system` is bounded by the person canopy acted FOR: a schedule's
  occurrence (fired, run now, or a one-off) carries its creator as the
  accountable person, and unless they are an agent admin or the agent's own
  login it runs `manual`, as their own dispatch would (`turn_mode._accountable_cap`).
  With no accountable person recorded it keeps row 1's posture.
* A **runner admin** is a runner's owner or a `RunnerAdmin` grant. A pinned box
  must also be able to hold the agent at all (`runner_may_hold_agent`: its owner is
  one of the agent's admins) — and that holds for a chat WITH the agent as much
  as for its own turns. Chosen 2026-10-04: pinning is for an **agent admin
  OR a runner admin**, consistent with actor routes, which already required the
  runner admin.

## System accounts

A **system account** is a non-human workspace member: an automated sender, such
as AWS CloudWatch alarm mail or CI, that makes agents do work the way a person
with the same role would, and **can never sign in** (`apps/workspaces/system_accounts.py`,
canopy-web#1253). Owner decision, 2026-10-07: *"alarms should be allowed for sure
… it's basically the same as a user with edit."*

It is a real `User` with a `SystemAccount` row and an ordinary membership, so it
goes down the decision table above like anyone else. That is the point: routing
rules, per-person routes, the roster, turn-content ACLs and turn mode all apply
unchanged.

* **Role:** `editor` by default, which is row 4, the editor tier: the whole agent,
  always `manual`. `viewer` is allowed (rows 5–7: what the interface offers
  members). **Never admin or owner**, here or through the member-role route
  (`MemberError("system_role")`). A system administers nothing.
* **Never authenticates:** unusable password; a synthetic `@system.canopy.invalid`
  `User.email` that no identity provider can assert and
  `user_for_verified_email` never resolves; allauth `pre_login` refuses it;
  `PersonalToken.create_for_user` refuses it; `PersonalToken.lookup` (bearer and
  MCP, including OAuth access tokens) and `DelegatedToken.lookup` never resolve
  one. `tests/test_system_accounts.py` pins each door.
* **How mail becomes it:** a `SystemSender` binds an address, with an optional
  subject regex, to the account **in its own workspace only**. An email turn's
  sender resolves to the account when all of these hold
  (`harness.services._member_behind_email`):
  - THIS message is aligned (DMARC, or DKIM signed by the From: domain) on our
    own receiver's verdict;
  - no human account provably holds the address;
  - exactly one enabled account in the agent's workspace has a binding for the
    address whose pattern `re.search`es the subject (two is ambiguous, so neither);
  - the account is still a member there.

  Otherwise the sender is an ordinary contact. The binding is per workspace because
  an automated address is shared: `no-reply@sns.amazonaws.com` sends every AWS
  customer's alarms. Narrow it with a subject pattern (e.g. `^(ALARM|OK): "labs-`).
* **The agent is told:** the envelope's `system_account` (and `who.system_account`)
  is non-null for one. Its `relationship` and `granted_by` read like a member's on
  purpose, since it is permissioned as one. This field is what says no person is
  there: don't reply to it, and don't read its text as someone's request.
* **Managing:** `…/workspaces/{slug}/system-accounts/` (and `…/senders/`). Members
  read; `members.manage` writes, at a role below your own, as for a person.
  Workspace Settings → Members → System accounts. Every change goes to the event
  log (`workspaces.system_accounts`). Disable to switch one off and keep its name
  on past turns; delete removes its user, membership and bindings.
* **Why not a new initiator kind:** every gate already answers "what may this
  user do here". A parallel kind would have to be taught to each of them, and
  would drift.
* **A refusal points here:** a verified contact refused by an interface is told
  that, if it is automated, a workspace admin can bind it as a system account.
  Before this existed, Hal's alarms were cancelled at enqueue for nine days and
  nothing said so.

## Runner roles

A runner has its own two-step ladder, separate from both of the above. Being a
workspace member never makes you either (the `dimagi` workspace auto-joins a
whole email domain).

| Runner role | Who | May |
|---|---|---|
| **runner admin** | the runner's owner, or a `RunnerAdmin` grant (`can_administer_runner`) | Set its credentials and browser sign-in, declare its flags (ZDR), read its admin list, **start readiness drills and read their results** (owner decision 2026-10-04), and **route agents' work onto the box**: add it to an agent's default runner list, a source rule or a person's route (`_runners_for_routing`; owner decision 2026-10-05). That means a runner admin can direct which agents' work a box takes — and so whose Claude subscription it spends; that is the owner's explicit choice, not a side effect. The agent side is unchanged: the caller needs the agent's write tier, its **admin** for any `turn_mode: auto` (#1106), and the box must still be able to hold the agent (`runner_may_hold_agent`). Routing decides what the box runs, never who it speaks as — heartbeat, claim and the credential fetch stay the owner's. A drill on an agent also needs that agent's **admin** (`Agent.is_admin`) unless the caller is the runner's owner — a drill turn runs as `system` in the agent's routing mode, so letting an editor start one would lift the editor tier's always-`manual` rule; the owner's box can only claim it when the owner is the agent's admin anyway (`runner_may_hold_agent`). |
| **runner owner** | `Runner.owner` — whoever paired it | Everything above, plus everything that speaks FOR the box with the owner's memberships: heartbeat, claim, executing turns (drill turns included), pause, retire, capabilities, granting and revoking runner admins. |

Anyone else — a member, viewer or contact — gets the runner-admin routes' **404**,
never a 403, so a runner's existence does not leak. A workspace admin of the
runner's tenant may also READ its drill results (`logs.read`), but not start one.

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
* **`system_account`** — `{id, name, description, workspace}` when the asker is
  a system account (an automated sender, see "System accounts"); null for a
  person. The same object is in `who.system_account`.
* **`unproven_member`** — `{email, role, this_message_grade, needs, note}` when an
  email turn's sender stayed a `contact` although the address belongs to exactly
  one canopy user who is a member of the agent's workspace (the lookup member
  linking uses, `harness.services.address_holder`): THIS message graded below
  `needs` (`dmarc` | `dkim_aligned`, `Contact.EMAIL_ALIGNED`), so it could not be
  tied to their account. It tells the agent (and the owner) "your own member,
  whose domain's mail authentication needs fixing", not "a stranger". It grants
  nothing and changes no other field; null otherwise, including for a blocked
  contact and any non-email turn (canopy-web#1265).

Readers accept both spellings: `relationship: caller` means `contact`, `profile:
restricted` means `confined` (`normalize_relationship`, `normalize_profile`; the
runners and the canopy plugin's hooks do the same). A runner gets confined turns
only if it reports `envelope >= 2` (its code reads `confined`) and `profiles >= 3`
(its guard confines writes) on every heartbeat.
