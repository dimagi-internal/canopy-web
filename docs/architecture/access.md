# Access — the glossary and the one rule

Every word canopy uses about who may do what has exactly one meaning, and it is
defined here. Code docstrings point at this page instead of re-defining terms
(`apps/agents/access.py`, `apps/harness/caller_context.py`,
`apps/workspaces/permissions.py`, `apps/agents/interface.py`,
`apps/harness/turn_mode.py`). Reuse existing words, one meaning per word, no new
terms.

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs,
  invite people, create content, reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/access.decide(agent, who,
*, verified, origin, capability=None, turn=None)`, which returns a `Decision(role, access,
capability, may_request_auto, may_pin_runner, manual_only, basis, reason)`. Every
door that makes an agent do work asks it (see "Where it is asked" below);
`tests/test_access_matrix.py` asks it through every door. Don't write a second
predicate for "may this person make this agent do X" — ask `decide`.

## Whose authority an agent acts with

**An agent acts as itself — with its own credentials or ones it borrowed — or as you
only under an act-as-you permission you granted.** Three kinds, one meaning each:

* **Its own credential** — the agent's own login (`Agent.user`, e.g.
  `ace@dimagi-ai.com`) and the secrets in its own vault. A runner turn signs in with a
  login issued for that turn (`TurnAgentToken`); a stored login expires like any PAT
  (`PAT_DEFAULT_TTL_DAYS`, 180 days) and rotation replaces it first
  (`pat_rotation.max_days`).
* **A borrowed credential** — a standing credential from a person's own account given
  to the agent (`AgentDelegation`: its owner's or an admin's GitHub token, shown as
  "GitHub · borrowed from <name>'s account"). It is the agent's to use in any turn
  `decide` lets run in the full profile (never a caller's confined turn), and it leaves
  canopy-web only for ONE turn a runner holds
  (`POST /api/harness/runners/{rid}/turns/{tid}/github-token`) — never from
  `credentials/resolve`, not to an admin's PAT, not to a box outside a turn.
* **An act-as-you permission** — time-limited, granted by the person asking, in the
  moment (a page's host grant; an agent working in their own turn, below).
  Nothing else lets an agent act as a person. In particular:
  - **An agent's caller token only reads.** A full-profile AGENT turn asked by someone
    who is not the agent's owner, an admin or canopy — an editor, a `full:` rule, a
    writer in someone else's chat — reaches canopy as its asker so that it sees what
    they may see (`CallerToken`, `Grant.read_only`), but its tools are the READ tools
    only (MCP readOnlyHint: every GET route, and the hand-written reads) plus driving
    the asker's own open page (`caller_tokens.PAGE_DRIVE_TOOLS` and the page's `page_*`
    tools: they run in the asker's tab, as them, and canopy's own page actions only
    navigate and read — anything that commits waits for their click). A write is
    refused, on MCP (`mcp.turn_scope.READ_ONLY_REFUSAL`) and on REST
    (`tokens.middleware._authenticate_caller_token`), saying the asker has granted no
    act-as-you permission: the agent writes as its own login. Confined caller tokens
    keep their capability's own tool list. A **repo chat** (no agent: a person's own
    Claude Code session on a repo they chose) is that person's own tool, and its token
    keeps every tool, as them.
  - **An agent's canopy login is always the account at its own email** (`Agent.email`),
    never any other (`agent_login.link_by_email`; `services.link_canopy_user` refuses
    any other account). An admin links it, browser or token — gated by role: with no
    account at that email canopy creates one (never signed in interactively) and links
    it; an existing account there that nobody has ever signed in to interactively links
    directly (no person is behind it to accept); when the account is the admin's own,
    after a passkey step-up; when it is anyone else's account that has signed in, canopy
    can't tell an agent's mailbox from a person's, so a
    request (`AgentLoginRequest`, 7 days) waits for the person holding it to accept, in
    their own browser behind a passkey step-up (Settings → "Agents asking to use your
    account", `POST /api/me/agent-login-requests/{id}/accept`; a token is refused). A new
    `Agent.email` unlinks the old account and derives the link again; an agent with no
    email has no login.
  - **An agent's 1Password-held token is watched.** canopy cannot rotate a token it does
    not store, so within 14 days of its expiry (with no automatic rotation) the agent's
    admins get an alert naming how to re-mint it (`alerts_agent_token.py`).
  - **A runner never hands a turn its owner's token.** The cloud runner strips the
    box's `CANOPY_TOKEN` from every turn, agentless repo turns included
    (`cloud_runner._agent_env`).

## Workspace roles

`viewer < editor < admin < owner` (`WorkspaceMembership.ROLE_RANK`). What each may
do is one table, `apps/workspaces/permissions.py`, keyed by capability; nothing
outside `apps/workspaces/` names a role. Full detail: `docs/architecture/roles.md`.

| Role | Holds | In plain words |
|---|---|---|
| **viewer** | `read` | Reads the workspace; talks to an agent through what its interface offers members. |
| **editor** | + `content.write`, `agent.work`, `session.drive`, `events.write` | Makes things; **edits agents and dispatches turns to them** — those turns always run `manual`. |
| **admin** | + `logs.read`, `members.manage`, `integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide` | **Runs the workspace**: every log, members below admin, the integrations, the default runner order, how long chat and turn content is kept, which turns are refused, and starting or declining a proposed project (beside that project's own owner). **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own` | **The keys**: the shared vault, the Slack app, connected sites, deleting the workspace — and is **every agent's admin**. |

**Workspace permissions** (the capability names): `read`, `content.write`,
`agent.work`, `session.drive`, `events.write`, `logs.read`, `members.manage`,
`integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide`, `own`.

## Getting into a workspace

Nothing is automatic: there is no self-join and no domain auto-join. People at a
listed domain may only **request an invitation**. A person becomes a member in
exactly three ways — and an agent's own login in a fourth — and the only code that may
create a `WorkspaceMembership` is `services._grant` (the first two),
`services.ensure_member` (the default-workspace bootstrap, and the agent-login door
below) and workspace creation — `apps/workspaces/tests/test_access_requests.py`
fails on a fourth door, and `tests/test_no_implicit_enrolment.py` pins
`ensure_member`'s callers:

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
  **viewer**, **editor** or **admin** (an owner any of them; an admin only below
  admin — `permissions.may_manage_member`) or
  **denies**, with an optional reason. The requester is emailed the outcome. One
  open request per person per workspace; asking again is idempotent. `GET
  /api/workspaces/requestable` lists where you may ask. Notifications go through
  the invite mechanism (labs SES) plus Web Push and are best-effort: a failure
  never fails the request — it is stored on the request (`notify_result`) and
  written to the event log (`source=workspaces.access`, `warn`).
* **Creating a workspace** makes you its **owner** — subject to
  `login_gate.can_create_workspace`: an *invite-admitted* user holding no membership may
  not, or invite-admission would become transitively delegable (create a workspace, mint
  invites, and each invitee clears the login gate too). `/api/me/` reports
  `can_create_workspace` so the UI never offers a button that 403s.
* **An agent's own login** (not a person). `POST /api/agents/{slug}/canopy-token`, run by
  `canopy agent vault`, creates the login an agent signs in to canopy as when it has none:
  a brand-new, password-less **editor** from the agent's mailbox address, linked to that
  one agent. Only an agent admin who is also a workspace **admin** or **owner** may — who could
  have invited it — and
  an address that is already a canopy user is refused, never adopted
  (`apps/agents/agent_login.py`). Moving the agent (`POST /api/agents/{slug}/move`)
  brings that login into the destination as an editor on the same bar — only when the
  mover holds `members.manage` there; otherwise the move reports it and adds nobody.

There is no other way in. A connected site's visitor is a **contact**, never a member
(`WorkspaceMembership.provisioned_by_app` is provenance on old rows only), and
`access_request_domains` means "may ask", never "may join". A request cannot probe
tenants: a workspace whose domains do not match yours and one that does not exist return
the **same 404**. Approval is upgrade-only, like an invite, so it never demotes a member.

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
* **`editors:`** — `full` (default, omitted) or `confined`: whether a workspace editor who
  is not an agent admin gets the editor tier (row 4) or is decided like a viewer member.
* **The default interface** (`interface.default_interface()`): no `full:` rule, no capability,
  `callers_default: none`. **Every agent has an interface** — a new agent is created with the
  default (a field default on `Agent.interface`), `GET /interface` never returns `{}`, and
  "unpublish" is now **reset to default**. The default behaves exactly as an absent interface
  always did (rows 1, 4 and 5 below): the owner, admins and editors, and nobody else. The page
  edits it as a form (Settings → Channels → Callers) with the YAML under "Advanced"; a draft is
  checked by `POST /interface/validate`, which asks `interface.parse` per field.

## The rule (the decision table)

Asked in this order; the first row that matches decides.

| # | Who | Access | Mode | May request `auto` | May pin a runner |
|---|---|---|---|---|---|
| 1 | **owner / admin / system** | `full` | the routing ladder | owner/admin, on a verified credential | owner/admin: any runner that may hold the agent |
| 2 | a writer in an admin's own session (`session_writer`) | `full` | the routing ladder | no | only a runner they administer |
| 3 | anyone a **`full:`** rule names | `full` | the routing ladder | no | only a runner they administer |
| 4 | **workspace editor or above** who is not an agent admin, unless the interface says `editors: confined` | `full` | **always `manual`** | no | only a runner they administer |
| 5 | viewer / contact, **no published interface** | `none` | — | — | — |
| 6 | viewer / contact (or, under `editors: confined`, an editor) whose class a capability lists | `confined` to it | the routing ladder | no | — |
| 7 | anyone else | `none` | — | — | — |

* **Over the table: the sensitive-data rule** (`access._sensitive_rule`, spec
  `2026-10-22-sensitive-data-agents` §5). A capability its interface says
  handles sensitive data (`data: sensitive`, `apps/agents/sensitive_data.py`) — the one a turn names, or the
  one its conversation's site or page selects — is **confined for everyone**: whoever
  the table gave FULL (owner, admin, `system`, a `full:` rule, an editor, a session
  writer) runs that capability's profile instead, keeping their role, mode and pin
  rights (`basis: sensitive:<capability>`). Any turn in a sensitive conversation (one
  that requires sensitive-data runners) must end in that capability; anything else — full, another capability, or a
  caller the door does not list — is refused. The setting is an interface setting,
  saved by the agent's owner or an admin, browser or token — as YAML (`publish_interface`)
  or with the capability's sensitive-data switch, which edits that YAML (`set_capability_sensitive`) — and audited
  like any setting change (`docs/architecture/settings-audit.md`; the agent's Settings
  → Interface shows its history); such a capability that holds a shell, writes, or any
  tool but the gateway's site tools, `current_page`, `who_is_asking` and read-only
  local tools is a 422. The turn runs the agent's current code; no commit is pinned.
  A runner in sensitive-data mode refuses any unconfined envelope (`sensitive_data_unconfined`).
* Row 3 before row 4 keeps an owner's chosen domain-wide setup intact:
  Eva's members get her whole through `full: [member]`, in her routing's mode.
* Row 4 (the **editor tier**): an editor may EDIT the agent (its definition and
  settings, as `agent.work` always allowed) and DISPATCH work to it ("improve
  yourself"), but every turn they start runs `manual` — outbound or irreversible
  actions wait for an admin — and they may not set `auto` anywhere: not on a
  dispatch, not on a routing rule or actor route, not on the agent's own switch.
* **`editors: confined`** (an interface key; default `full`, which is row 4 and is
  never written) skips row 4: an editor who is not an agent admin is decided exactly
  like a viewer member — row 3 if a `full:` rule names them, else row 6 (the
  capability listing `member`, in the routing ladder's mode with no manual cap, as
  any confined turn), else row 7. For an agent meant for everyone, such as a support
  agent, where neither "every editor chat waits for an admin" nor "`full: [member]`
  hands editors the whole agent in auto" fits. Owner and admins (row 1) are
  unaffected. Unknown values are refused at publish, as is `confined` on an
  interface with no capability and no `full:` rule.
* Row 1's `system` is bounded by the person canopy acted FOR: a schedule's
  occurrence (fired, run now, or a one-off) carries its creator as the
  accountable person, and unless they are an agent admin or the agent's own
  login it runs `manual`, as their own dispatch would (`turn_mode._accountable_cap`).
  With no accountable person recorded it keeps row 1's posture.
* A **runner admin** is a runner's owner or a `RunnerAdmin` grant. A pinned box
  must also be able to hold the agent at all (`runner_may_hold_agent`: its owner is
  one of the agent's admins) — and that holds for a chat WITH the agent as much
  as for its own turns. Pinning is for an **agent admin OR a runner admin**,
  consistent with actor routes, which require the runner admin — and for the
  **agent's own login** (`Agent.user`) on a box its admins already route it to:
  an enabled box in its default order (its own or the workspace's it follows)
  that no strict rule for the agent's own address keeps its work off
  (`access._routed_to`). That lets an agent hand its next chat to another box it
  runs on without adding reach its routing did not already give.
* A **project chat** (no agent, only a repo) has no agent admins, so choosing its
  box — `runner_id` at creation, a placement on send, `/place`, a move — is for a
  **runner admin** of that box only
  (`canopy_sessions.placement.may_place`, the one placement rule). Unplaced, it runs on a box
  its workspace's runner order shares or one its sender administers
  (`runners.md` → "Project chats"), never on a teammate's box by default.
  It acts as the person who asked (see "Projects run as the person who asked").

## System accounts

A **system account** is a non-human workspace member: an automated sender, such
as AWS CloudWatch alarm mail or CI, that makes agents do work the way a person
with the same role would, and **can never sign in** (`apps/workspaces/system_accounts.py`).
This is the owner's explicit choice: an alarm sender is basically the same as a user with edit.

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
  (`harness.channels.email.member_behind_email`):
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
  that, if it is automated, a workspace admin can bind it as a system account,
  so an automated sender is never cancelled at enqueue without anyone being told.

## Runner roles

A runner has its own two-step ladder, separate from both of the above. Being a
workspace member never makes you either (the `dimagi` workspace auto-approves
access requests from a whole email domain).

| Runner role | Who | May |
|---|---|---|
| **runner admin** | the runner's owner, or a `RunnerAdmin` grant (`can_administer_runner`) | Set its credentials and browser sign-in, declare its flags (`sensitive_data`), read its admin list, **list every session open on the box as facts only and close an idle one** (`GET /runners/{id}/sessions/`, `POST …/sessions/{sid}/close`: who started it, how, its turns, last activity, `open_reason` — never a title, prompt or message, so a person's web chat the admin cannot open is still accounted for; a close refuses one with a turn running, a question waiting, or mid-transfer), **start readiness drills and read their results**, and **route agents' work onto the box**: add it to an agent's default runner list, a source rule or a person's route (`_runners_for_routing`). That means a runner admin can direct which agents' work a box takes — and so whose Claude subscription it spends; that is the owner's explicit choice, not a side effect. The agent side is unchanged: the caller needs the agent's write tier, its **admin** for any `turn_mode: auto`, and the box must still be able to hold the agent (`runner_may_hold_agent`). Routing decides what the box runs, never who it speaks as — heartbeat, claim and the credential fetch stay the owner's. A drill on an agent also needs that agent's **admin** (`Agent.is_admin`) unless the caller is the runner's owner — a drill turn runs as `system` in the agent's routing mode, so letting an editor start one would lift the editor tier's always-`manual` rule; the owner's box can only claim it when the owner is the agent's admin anyway (`runner_may_hold_agent`). |
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
`granted_by`); the extended A2A card (`offered_to`); the access roster and the
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

## Local use of an agent

A person may run an agent in their **own** Claude Code, with the agent's plugin and
skills, and canopy keeps the record (`apps/harness/local.py`):

* A **private runner** (`PrivateRunner`) is a person's machine: plain Claude Code with
  the canopy plugin, no runner app. It is registered once with the person's own
  token (`canopy private-runner register`) and belongs to the PERSON — one serves
  every agent they use. On its own it never claims a turn and nothing routes to it.
* When the machine also runs the runner app, the two are **one private runner**:
  `PrivateRunner.runner` links the registration to that `Runner`, and the runner is
  marked **`private`** (`PUT /api/harness/runners/{id}/private`, owner only). A
  private runner takes no fleet work — no routed turn, no workspace order, no source
  rule (`Runner.directed_only`) — only what its owner pins or binds to it, plus the
  local sessions on it. Its device key is what those sessions prove themselves
  with, and it may read an agent's secrets while its owner holds an active license
  (`device_keys.private_runner_licensed`), for that owner's own turns only.
* A machine **without** the runner app still gets its own key for reading the
  agent's vault: registering it (`POST /api/private-runners/`) gives it a
  `computer` runner (`Runner.COMPUTER`, `local.ensure_computer_runner`, returned as
  `runner_id`) to enrol a device key on. The key is owner-approved like a private
  runner's and serves exactly the agents its owner administers or holds an active
  license for; its token reaches only the owner's local turns and license reads
  (`runner_auth._COMPUTER_RULES`), and the computer appears in no fleet runner list.
* **Acting as a lower role.** `acts_as` (`editor` or `viewer`) is a property of
  ONE turn or of one client's requests — never a setting left on a machine:
  - a turn started with it (`TurnIn.acts_as`, `canopy agent dispatch --as editor`;
    `LocalTurnIn.acts_as` for a local session) is judged at that role from start to
    finish — whether it may start at all, its mode (`auto` becomes `manual`), its
    envelope, and every read made during it (`X-Canopy-Turn-Id` by its initiator,
    or the turn of the caller token it calls MCP with — REST and MCP alike).
    The server stamps `origin_ref.acts_as`; a client's own `origin_ref` cannot.
  - a client puts ITSELF in a lower mode with `X-Canopy-Acts-As` on each request —
    the canopy CLI and the runner send it when started with `CANOPY_ACTS_AS`. A
    runner claiming in that mode stamps the cap on its owner's turns it claims
    (never loosening one a turn was started with); someone else's turn keeps its own.
  The cap rides on the user object (`apps/workspaces/role_cap.py`) and is honoured
  where roles are read — `member_role`/`member_roles`/`membership`,
  `Agent.is_admin`, `relationship_for_user`, `caller_runs_agent` — so `decide` places
  the person in the tier the cap names. It only ever lowers, so anyone may ask for it
  with no proof. It is how anyone, an agent included, runs the non-admin path for real.
* **No PAT on a private runner.** The owner's local sessions there sign in with the
  box's own device key (its runner token), which on a private runner also reaches the
  local-turn routes (`runner_auth._PRIVATE_RULES`) — never `/api/private-runners`,
  which takes the person's own token — and reads credentials under the owner's
  license. Such a turn's initiator assurance is `private-runner-key` (verified, like
  a PAT). A shared runner's token opens no local session and reports on none, and a
  private runner's reaches only its local-turn routes, for its own registration.
  Only the owner registers or approves a device key on a private runner
  (`runners.may_trust_device_keys`); its runner admins still see and may revoke them.
* A **local turn** (`Turn.origin = local`) is one session there with one agent,
  opened by the canopy plugin's SessionStart hook
  (`POST /api/harness/local-turns/`). It is a real turn: the person is its
  initiator (verified, `via=local`), it is RUNNING from the start and never
  claimed, and it carries the same caller envelope a runner turn gets
  (`trigger.private_runner` names the machine; `trigger.runner` is null).
* **Who may.** Exactly whom `decide` gives the WHOLE agent (rows 1–4): its owner
  or an admin, a `full:` rule, a workspace editor. A viewer or contact is refused
  (403) and keeps the confined `ask` path through canopy.
* **Mode.** `manual`, always — except an agent admin who asks for `auto`. Stamped
  at open, like a dispatch's request; a `full:`-rule member does not get the
  routing ladder here, because nothing about a session on their own machine is a
  channel the owner set a posture for.
* **It never holds the agent.** A local turn is excluded from
  `one_executing_turn_per_agent` and `routing.busy_agent_ids`: it is the person's
  session, not the agent's one continuous session, so it neither waits behind the
  agent's runner turns nor delays them, and two people may use the agent at once.
* A **license** (`AgentLicense`, `apps/agents/credentials/licenses.py`) is person × agent: the
  record that someone uses the agent locally. The caller list decides who MAY
  (`decide`, above); the license is who DOES. Requested on the first local turn and
  issued at once when `decide` gives the whole agent (basis `owner`, `admin`,
  `full:<rule>`, `editor`); anyone else gets a `requested` row the agent's admins
  see, and a refusal. An agent admin **revokes** it per person without editing the
  rule, and a revoked license is never re-issued automatically; **approve**
  reinstates it, re-asking `decide`, so it cannot license someone the caller list
  does not give the whole agent. `GET /api/agents/{slug}/licenses` (any member)
  lists holders, their private runners, local turns, credential reads and last use;
  the agent's Settings → People → Local use shows it.
* **Credentials.** `/credentials/resolve` has a third principal beside `runner` and
  `admin`: **`license`** — an ACTIVE license (re-checked against `decide` on every
  read) AND one of the caller's OWN live turns on that agent named in
  `X-Canopy-Turn-Id`: their running local turn, or — on their private runner's
  token — a turn of theirs that runner claimed. No turn, no secrets. A license read
  never gets the agent's canopy login, the owner's GitHub delegation, lent or shared
  credentials, or the agent's 1Password service-account key (`op_sa_token`), which
  reads the whole vault, canopy login included (`LICENSE_WITHHELD_EXTRAS`) — so a
  non-admin using a 1Password-backed agent locally gets no secrets from it. Each read is logged with `via=license`,
  the turn and the license. Secrets read this way reach the person's machine;
  nothing brokers each call through canopy-web.
* **Reporting.** Only its initiator may renew, finish or append its transcript —
  there is no box. The lease is 8 hours, renewed by the session's prompts; a
  session that dies is swept `lost` like any turn.

## What a caller's turn tells the owner

A confined turn can act on nothing, so a caller's procedure ends its final
message with one line, `For <Agent>'s owner: <the ask>`, and tells the caller
the request is noted where the owner reviews it. The runner lifts that line out
(the laptop from the `cx-` session's `Stop` hook, whose profile names the turn;
the cloud box from the finished reply) and sends it as an `owner_ask` turn
event. canopy-web re-parses it and appends it to the turn's `result_note`, for a
confined turn only and once per line (`apps/harness/owner_ask.py`). The note is what every view of the turn shows the people who
may read it; the transcript stays the record.

## Admission rules

Some combinations of the above can never complete, and canopy refuses them
before any session starts instead of spending one (`apps/admission`). The worked example: an email from a **confined** caller, in
**manual** mode, claimed by a **cloud** runner. The ask procedure drafts a reply
and waits for a human yes, and a cloud session that ends cannot receive one.

* **What a rule is.** A row on a workspace (`AdmissionRule`), edited by its
  admins (`admission.manage`) at `/w/:workspace/settings/admission-rules`. Its
  conditions are any subset of: agent, origin (the `Turn.origin` vocabulary),
  principal (`member`/`contact`/`agent`/`system`, from who asked), access
  (`confined`/`full`), capability, turn mode, runner kind
  (`emdash`/`cloud`/`remote`), and reach (`paused`/`offline`, below). A blank
  condition means any. A rule needs at
  least one. Its action is `reject`, `allow` or `auto` (below). It also carries a reason and two
  notice switches (sender, owner), and it can be enabled or disabled.
* **Where it is evaluated.** At enqueue, beside `_apply_capability`, for rules
  that need no runner (no turn-mode or runner-kind condition). And in
  `claim_next_turn` right after `turn_mode.for_turn`, the one point where the
  runner's kind and the resolved mode are both known; both claim channels pass
  through it. Rules apply to agent turns (an agent's own, or its chat's), and
  only the agent's own workspace's rules apply.
* **Precedence.** An enabled `allow` that matches exempts the turn from every
  `reject`. That is how one agent opts out of a workspace-wide rule. A disabled
  rule matches nothing, so a disabled agent-narrowed copy of a reject does NOT
  exempt anyone. At enqueue, a condition not known yet counts as matching for an
  `allow` and as not matching for a `reject`, so the claim decides. Among
  matching rejects, the most specific wins, then the oldest.
* **Run in auto mode** (`action: auto`). This is the third action, and it is
  decided at claim before the rejects (`evaluate.auto_rule`). A matching turn
  whose mode resolved **manual** runs **auto** instead. The basis reads
  `admission rule <id>: <reason>`, the rule's fired count rises, and the event
  log gets `admission.auto` once the claim lands. It raises only the agent's
  STANDING posture (`turn_mode.Resolved.raisable`: a routing rule or
  `Agent.turn_mode`). It never raises an editor's cap, a dispatch that asked for
  manual, or auto withheld from an unverified message. A session that started
  manual is raised only when nothing about THIS turn holds it back: its own rungs
  resolve to the standing posture, the message is verified, and the caller is not
  confined (`turn_mode._carried`). Otherwise a thread first answered manually on a
  laptop runs manual on the cloud, where nobody can approve its reply. It cannot
  be about refused or unclaimed turns. The use: `ace,
  cloud → auto`. A runner list puts the laptops first, so the cloud only takes
  the turn when no watched laptop is free, and there it runs instead of waiting
  for an approval nobody will give.
* **What a rejection does.** The turn closes **CANCELLED**, not FAILED, so no
  debugger turn starts. `result_note` reads `rejected: admission rule <id>
  (<conditions>) — <reason>`, the ledger's status event carries the rule id, the
  workspace event log gets `admission.rejected`, and the rule's fired count and
  last-fired time move. No runner ever holds the turn.
* **The notices** (`AdmissionNotice`, one row per notice, sent or not). They
  are fixed text and never model-written:
  * The **sender** gets an automatic reply on their email thread, from the
    agent's own mailbox. canopy-web sends it with the agent's `gog-token`
    credential when that token was minted on canopy-web's own Google client;
    the thread is then marked read. When it cannot — most agents' tokens are on
    the desktop `canopy` client, which only a box with gog can refresh, or
    Google refuses the refresh or the send — the notice is left `pending`, and
    the runner that reads the agent's mailbox leases it on its next mailbox poll
    (`POST /api/harness/runners/{id}/admission-notices`, only for agents it may
    hold and only from the mailbox the sender wrote to), sends the same fixed
    text through its own gog client with the same threading and
    `Auto-Submitted: auto-replied`, and reports it
    (`apps/admission/runner_notices.py`; the runner side is
    `runner/canopy_runner/canopy_runner/admission_notices.py`). A runner
    failure leaves it pending for the next lease (15 minutes); one that no
    runner manages within 24 hours ends `failed`, saying why. A send that may
    have gone out (a timeout) is never retried. There is no reply to a machine
    (Auto-Submitted, Precedence bulk/list, list mail), a no-reply address, the
    agent's own mailbox, a system sender, or canopy/another agent. At most one
    reply goes per thread per 24 hours. It is **off** in each workspace until
    one of its admins turns it on, on the Admission rules page
    (`AdmissionSettings`, `PUT /api/workspaces/{slug}/admission-settings`,
    logged to the event log); while it is off, the notice is recorded as
    `held`.
  * The **owner** gets, off the thread: an email (SES), plus a review ask on
    the agent's board parked on them (Waiting on you). Both name who wrote, the
    subject and thread, the rule and its reason, what the sender was told (a
    `Reply:` line: sent, queued for the runner, off in this workspace, or why
    not), and links to fix it: the rule
    on the settings page (`?rule=<id>`, highlighted), the agent's turn mode and
    runners, its interface callers, and its turns.
* **Defaults.** Every workspace is seeded with two rows (migration
  `admission/0002`, and at creation for every new one). The first rejects email + confined + manual + cloud, with both
  notices, and is **on**. The second is the same on a laptop (`emdash`) runner
  and is **off**. From then on they are ordinary rows. Disable one, narrow it
  with an `allow`, or delete it (a deleted default is not re-seeded).
* **Refusals as rules.** canopy refuses two
  kinds of turn on its own, before any rule: mail from a contact the workspace
  **blocked** (`channels.email.refused_email_turn`), and a person the agent's interface
  **offers nothing** (`_apply_capability`, access `none`). Those are access
  decisions, so they stand whatever the rules say: no `allow` can cover them,
  and disabling their rule does not let them run. Each is reported through a
  rule instead (`evaluate.at_refusal`): conditions `sender: blocked` or
  `access: none`, which also takes origin, principal, capability and agent but
  never a runner or a mode (a refusal comes before either). A matching enabled
  rule records the ledger event and the fired count and sends its notices, while
  the turn keeps the refusal's own `result_note`. Migration `admission/0005`,
  and every new workspace, seed `blocked-sender` and `offered-nothing`, each
  with **every notice off**, so by default these refusals stay silent. A blocked
  sender gets no reply unless an admin turns one on.

* **Reach rules: no runner will take it.** `reach: paused` matches a turn when
  every runner that could take it (`harness.claim.turn_reach`, the claim's own
  coverage) is paused with no `unpause_at`; `reach: offline` when none is
  reachable and the turn has waited the rule's `wait_hours`. A pause WITH a
  resume time matches neither: the turn waits, and its status says
  `waiting_runner` with `parked` and `held_until`. Reach is asked at enqueue
  when such a rule is in scope; again for the queued turns a runner could take
  the moment it is paused open-ended (`harness.services.pause` →
  `evaluate.recheck_stranded`); and by `evaluate.sweep_unrunnable` on the
  off-request clock (every 5 minutes at most) for the offline wait. A claim
  never matches one. A reach rule cannot be an `allow` or name a runner kind or
  mode. The rejection's `result_note` and the owner's notice (a `Runners:`
  line) name each runner's situation (`claim.runner_situation`): who paused it
  (`Runner.paused_by`), when, its note and its resume time, or since when it was
  last heard from. A chat, Slack or MCP asker reads that on the turn
  (`turn_status.rejection` → `TurnStatus.detail` on a `cancelled` turn).
  Migration `admission/0008`, and every new workspace, seed `no-runner-paused`
  and `no-runner-offline` (24h): ordinary rules, **on**, owner notice only. An
  admin who disables them gets turns that sit queued instead.

## The caller envelope

What the runner hands the agent beside each turn (`apps/harness/caller_context.py`,
VERSION 3 — 3 only added `person` and `trigger.kind`, so a v2 reader is unaffected): `who`, `verified` (THIS message), `relationship` (the agent role),
`profile` (`full` | `confined`), `granted_by` (`owner` | `admin` | `system` |
`editor` | `session:<role>` | `full:<rule>` | `capability:<name>` | `refused` |
`no-interface` for a turn with no agent), `capability` (the confined profile),
`turn_mode` (`{mode, basis}`), and:

* **`ship_grant`** — when ANOTHER agent's verified login that is this agent's
  owner or admin dispatched the turn at it, push / PR / merge in this agent's OWN
  repo are pre-approved even in `manual`. Nothing else is.
* **`sender_trust`** — `{email, reason, expires_at, active}` when an email turn's
  sender was tied to a member although THIS message is not aligned, by a temporary
  `SenderTrust` an agent admin set for that address (`verified` stays false); null
  otherwise.
* **`system_account`** — `{id, name, description, workspace}` when the asker is
  a system account (an automated sender, see "System accounts"); null for a
  person. The same object is in `who.system_account`.
* **`unproven_member`** — `{email, role, this_message_grade, needs, note}` when an
  email turn's sender stayed a `contact` although the address belongs to exactly
  one canopy user who is a member of the agent's workspace (the lookup member
  linking uses, `harness.channels.email.address_holder`): THIS message graded below
  `needs` (`dmarc` | `dkim_aligned`, `Contact.EMAIL_ALIGNED`), so it could not be
  tied to their account. It tells the agent (and the owner) "your own member,
  whose domain's mail authentication needs fixing", not "a stranger". It grants
  nothing and changes no other field; null otherwise, including for a blocked
  contact and any non-email turn. canopy also LOGS it, once, on
  the turn's event ledger at enqueue: a `TurnEvent` of kind `unproven_member`
  whose payload is this same object, read by `read_turn_events` and shown (with
  the note) in the activity drill-down. canopy writes it; a runner cannot post
  that kind.
* **`person`** (v3) — what canopy knows about the HUMAN asking:
  `{id, display_name, email, workspace, facts[], see_all}` (plus `hcp`, `grant`, `recall`, `record`, `projects`).
  null only when the asker is not a human (canopy, another agent's login, a
  system account, unknown). See "What agents know about a person" below.
* **`trigger.kind`** (v3) — what canopy-initiated work the turn is
  (`origin_ref.trigger`); null otherwise.
* **`conversation`** — `{session_id, thread_id, subject, slack}`. `slack` (v3,
  additive) is where in Slack the conversation is, whenever its session holds a
  Slack thread (born there, or bound by a share): `{team_id, channel_id,
  channel_name, thread_ts, permalink, is_dm}`; null otherwise. Read from the
  session's cache (`apps/slack/location.py`) — building the envelope never calls
  Slack. `channel_name` needs the install's `channels:read` / `groups:read`
  unless a slash command named it, and is null
  for a DM; `thread_ts` is null for a DM; `permalink` falls back to the web-client
  link. With `turn_id`, the agent may act on that conversation: see "Agents and
  their own conversation" in `api-surface.md` (`start_new_slack_thread`).

Readers accept both spellings: `relationship: caller` means `contact`, `profile:
restricted` means `confined` (`normalize_relationship`, `normalize_profile`; the
runners and the canopy plugin's hooks do the same). A runner gets confined turns
only if it reports `envelope >= 2` (its code reads `confined`) and `profiles >= 3`
(its guard confines writes) on every heartbeat.

## How confinement is enforced

Who asked is recorded on every turn (`initiator_*`), and the caller envelope
(above) reaches the agent three ways: in the claim response, which the runner
writes to `~/.canopy/caller/<turn_id>.json`; through the `who_is_asking` MCP
tool; and beside every delivered prompt — the runner leaves
`~/.canopy/caller/pending/<task>.json` before the send and the canopy plugin's
`caller_context` UserPromptSubmit hook adds a summary next to the person's words
(cloud: `CANOPY_CALLER`).

* **Default deny.** The default interface (nothing published) means only the agent's admins and its
  workspace's editors (manual) reach it; every viewer, contact or unidentified
  caller is refused (rows 5 and 7). The interface is live state on canopy-web —
  never a file in the agent's repo; owner-or-admin edit it on the agent. It
  confines callers who reach the agent through canopy (web chat, Slack, email,
  embedded sites). canopy's MCP server offers NO per-agent tools: someone who
  wants to use an agent installs its plugin and runs it in their own Claude
  Code, as themselves (`/get-started`).
* **An agent's own login is the agent.** `Agent.user` grades as `system`;
  another agent's login grades like any person, or agent A could steer all of B.
* **Three layers, each failing closed.** (1) canopy stamps `Turn.capability` at
  enqueue (`harness.services._apply_capability`) or refuses the turn; (2) only a
  runner reporting `profiles` (and `envelope >= 2`) may claim such a turn, and it
  runs it in its own `cx-` session with the profile written to
  `~/.canopy/profiles/<task>.json` first; (3) the plugin-wide `profile_guard`
  PreToolUse hook refuses anything the profile does not list — Bash
  argument-for-argument, `{thread_id}`-pinned, paths realpath'd.
* **Native deny rules under the guard.** The runner also writes the capability as
  Claude Code's own DENY rules (`runner/canopy_runner/canopy_runner/native_permissions.py`
  → the `cx-` worktree's `.claude/settings.json`; `--settings` on cloud). Fleet
  sessions run with `--dangerously-skip-permissions`, so only deny rules take
  effect natively: whole tools, MCP servers and high-risk programs the capability
  never grants. Exact-argument scoping stays the hook's job.
* **A confined session never holds the runner owner's PAT.** It reaches canopy's
  MCP with a **caller token** (`apps/harness/caller_tokens.py`, issued by
  `harness.claiming.issue_credentials`, `cct_…`), minted per confined turn at claim
  and bound to its conversation; the canopy plugin's `headersHelper` sends it from
  the session's profile. It resolves to the conversation's CURRENT turn: tools run as
  that turn's caller (no canopy user at all for an outside contact), and only the
  tools the capability lists exist (`apps/mcp/turn_scope.py`, `agent ∩ caller`).
  Expired, long-finished, or a conversation whose current turn is not confined → it
  authenticates nothing; `who_is_asking` answers only about its own conversation.
* **A confined turn holds no credentials it does not need.** canopy refuses it
  the agent's borrowed GitHub token (`agents.credentials.delegations.github_token_for_turn` → 409).
  The cloud runner builds its env from an allowlist (`_confined_env` in
  `runner/ec2/cloud_runner.py`: Claude's login, locale/paths/TLS, which agent and
  turn) — no `CANOPY_TOKEN`, no agent `.env`, no 1Password key (it never calls
  `credentials/resolve`), no chat key; gog's keyring password only when the
  capability's own bash rules run the mail path (`canopy email …` /
  `bin/<slug>-email`).
* **A colleague's FULL turn reads as the colleague.** A full-profile turn not
  asked by the agent's owner, an admin or canopy (a `full:` rule, a workspace
  editor, a writer in an admin's session — `caller_tokens.is_caller_scoped`)
  gets a **scoped** caller token (`CallerToken.scoped`): every READ tool exists, plus
  driving the asker's open page (`all_tools` + `read_only`; a write is refused — the
  asker granted no act-as-you permission), and each runs as the conversation's current asker, so
  `turn_access.can_read_turn_content` and the session ACL decide what
  `harness_list_turns` / `read_turn_messages` / `read_turn_transcript` return.
  The cloud runner leaves it at `~/.canopy/scoped/turn/<turn id>.token` and sets
  `CANOPY_SCOPED_TURN` (`_scoped_token_env`); the laptop runner leaves
  `~/.canopy/scoped/task/<emdash task>.token` from `execute._confine`
  (`scoped_token.py`) and clears it for a later turn with none. The plugin's
  `canopy-web-mcp-headers.js` sends it and never falls back to a PAT once it has
  one; a write failure refuses the turn. A scoped token does not serve a
  confined turn's conversation, and a confined token does not serve a full turn's.
  **REST takes a scoped token too, but only where MCP would** (canopy-web#1782,
  `tokens.middleware._authenticate_caller_token`): re-resolved on every request
  as above, it signs in as the asker on a route the MCP server serves as a tool
  (`api_tools.is_tool_route`) and is refused (403) on every other one. The routes
  MCP leaves out (minting a PAT, the person's own memory switches and grants, the
  runner protocol) stay out of reach of a session acting for someone. A confined
  token is refused on REST (401): its capability's tool list is an MCP filter, and
  REST has no equivalent to apply. This is what lets the canopy CLI in a scoped
  session read as the asker instead of failing for want of a PAT; like MCP, REST
  refuses an agent turn's token every write but driving the page (non-GET → 403);
  a repo chat's token keeps its writes.
* **Projects run as the person who asked.** Agents and projects are both first
  class, and both act FOR the person who asked; they differ only in WHO the
  session is. An agent's turn acts as the agent, bounded by the asker (the rule
  above). A project turn or project chat (a repo, no agent) has no agent to act
  as, so it acts as its asker: a verified, active human initiator gets the same
  scoped caller token (`caller_tokens._project_asker`), so its session acts on
  canopy with exactly that person's access — nothing more, and never the box's (the
  cloud runner strips its own `CANOPY_TOKEN` from the turn). Unlike an agent's caller
  token it may write: a repo chat is the person's own tool.
  The token follows the conversation's current asker and voids when a later turn
  has none verified. A project turn with no person behind it (a schedule, a
  drill) gets no token and no canopy identity. GitHub is the one thing a project
  borrows from an agent: its repo's `RepoIdentity` agent, for project dispatches
  and project chats alike (`delegations.turn_agent`; `runners.md` → "The GitHub
  identity a turn gets"). The cloud runner deletes the token when the turn ends.
* **Known limit.** On a laptop every session is the same OS user and emdash
  spawns it, so the runner cannot strip its environment; on any box, credential
  FILES (agent `.env`s, gog's keyring, ace's Drive key) stay readable to an
  escaped session. The guard and the native deny layer are what keep a confined
  session off them.

Spec: `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-and-access-design.md`.

## Acting on behalf of the person who asked

An agent's turn does the work of the person who started it, and canopy-web carries that
person without being told (`apps/harness/turn_asker.py`). A request authenticated with
the login canopy-web issued for a turn (`TurnAgentToken`) works for that turn's person;
the bearer middleware works them out on every such request, and nothing the agent sends
can add or remove them. The person is found only when the turn that login serves now
(its own, or a session login's newest turn) is executing, unconfined and runs for this
agent, and was started by a verified, active human who is not an agent — or was itself
started on such a person's behalf, so a chain of agents keeps them. The request must not
have lowered itself (`acts_as`), and the person's own turn cap applies to them. Anything
else — a stored PAT, a turn canopy itself started, an unverified asker — is the agent
acting for itself.

    X-Canopy-On-Behalf-Of: <the person's email>

still works as an assertion. It must name that same person; any mismatch, and any other
credential sending it (a browser session included), is refused with a 403 that says why.

On someone's behalf, these are judged as that person:

| Decision | Where | Their access needed |
|---|---|---|
| Pin a turn, or place a chat, on a runner | `access.may_pin_runner` (dispatch pins, chat placement, moving queued turns) | the target agent's admin, or the runner's administrator |
| Ask for `turn_mode: auto` | `turn_gates.check_requested_turn_mode`; the claim re-checks `requested_turn_mode_by`, which is them | the target agent's admin |

Reads are unchanged: a turn already reads as its asker where that matters (scoped caller
tokens, above). Each decision made on someone's behalf is recorded as an
`agent.on_behalf.decided` event. Every turn the agent starts from there, through any door
(a dispatch, a send into another session, a new session), carries
`origin_ref.on_behalf_of` (`{user_id, email, turn, agent, decided}`), stamped by
`services.enqueue_turn` alone; a client's own is dropped. Its initiator stays the agent:
the agent made the call, for that person. The receiving agent's envelope says so in
`who.on_behalf_of` (`{user, relationship, via_agent, turn}`, the relationship being the
person's to the receiving agent), which every turn an agent's login started carries:
`null` means the agent acted on its own.

## Acting as the person on a connected site (act-as-you permissions)

The **act-as-you permission** is the person's word for a **host grant**
(`tokens.HostGrant`): a time-limited permission, given in the moment, that lets an
agent call a connected site's tools as that person, within their own access on that
site, for the page they are on. It is the only way an agent acts as a person *on a
host* (the header above is canopy-web's own decisions):

- **Given by acting, never by loading.** The panel asks the site for one only when
  the person sends from it (or opens a conversation whose live card needs it), and
  then shows "<agent> can act as you on this page until <time> · Stop". 30 minutes.
- **Read by one lookup, with no fallback.** `host_grants.lookup`: a turn (gateway) or
  a View in a conversation reads only the grant of the page that last acted in that
  conversation as the person who started the turn / is viewing — expired or stopped
  is a refusal ("…has ended — send again to grant it for another 30 minutes"), never
  another page's grant or the agent's own credential.
- **A chat on a page reaches that site only as the person, under their act-as-you
  permission — owners included.** The gateway (`site_tools` / `site_call` /
  `site_read`) serves every turn in a conversation held on a site's page (the
  session's `embed_app`) whose agent is set up for that site — some capability of its
  interface lists it in `sites:` (`interface.page_site_of`) — whatever the turn's
  profile: a confined visitor's (its capability must list the site), an editor's
  (scoped token), the owner's or an admin's (found from the chat key the runner gave
  only that session). Every call uses the SENDER's grant for that page. A full-profile
  turn is narrowed by the widest ceiling the owner set on that site
  (`interface.site_ceiling`).
- **The agent's own login at that site is withheld from such a turn.** A capability
  names it per site: `own_login: {connect-labs: [LABS_MCP_TOKEN]}` — credential names,
  never values. The claim carries `page_site: {site, withheld}`
  (`interface.page_turn`); a cloud runner leaves those names out of the turn's
  environment and its per-turn env file and sets `CANOPY_PAGE_SITE`; a laptop runner
  cannot set an emdash session's environment, so it leaves a by-task marker
  (`~/.canopy/page/task/<task>.json`) for the agent's own MCP headers helper — the
  agent's `.env` is still on that disk, so there the boundary is the helper and the
  instructions. The caller envelope's `site` block tells the agent it is on a page,
  reaches the site only through the gateway as the person, and has no account of its
  own there in this conversation.
- **The person sees and stops theirs**: Settings → Act-as-you permissions
  (`/api/me/host-grants/`), and Stop in the panel (`/api/embed/host-grant`, and
  `/api/contact/host-grant` for a contact). Stop takes effect on the next call.

Mechanics: [`embedding-internals.md`](embedding-internals.md) § Act-as-you
permissions; the wire contract: `sdk/python/README.md` § Host grant contract v1.

## Who can see a session

A session is visible to the people who were **in the conversation where it
happened**, plus whoever they explicitly add — never to someone merely for being
in the same workspace. Multiplayer is always an explicit choice (a participant
row, added by the session's owner). Agents get no implicit access: an agent's own
login reads a session only if it was made a participant, it is running a live
turn in it (`access.own_turn_session`), or the person granted it review (a
coach). Canopy only operates on what a machine has already **sent**; it asks a
machine for more (live streaming, history) only when its owner, or someone they
explicitly added, asks (`access.may_pull`). Adding someone is the session owner's
(`access.can_share`), with one more door: the owner of the agent a member's
conversation is with (`Agent.owner`, not its admins), once in it, may bring someone
new in — a person or an agent — but owns nothing, reads nothing new, and changes or
removes nobody; never a contact's or a sensitive one (`access.can_add_people`). Sensitive conversations stay sealed to
their own people (`tenancy.md` § Sensitive conversations). The rule is
`apps/canopy_sessions/access.py`; enforcement detail is `tenancy.md` § The
chat-session ACL.

| Where it happened | Who sees it (read + write unless noted) | Who may pull from the machine |
|---|---|---|
| **Laptop** (a runner-discovered session) | the box's owner (`Runner.owner`), + people they add, + a project's team if they shared that project's repo with it (`ProjectSessionShare`, opt-in per person × project, `/api/me/session-sharing/`); the box's runner admins read only (to move it off the box) | the box owner, + people they add as editor, + that project's team |
| **Web chat** | its creator + people they add | the same |
| **Email** | everyone on the thread who is a verified member (§ Who is in a session) | the agent's admins, + the thread's people |
| **Slack** | everyone in the Slack channel (a DM: its people), read + write, as in Slack | the agent's admins, + the channel's people |
| **Panel** (an embedded widget) | the visitor (a contact: `contact_session_q`) | the visitor |
| **An agent's own work** (an alarm, a scheduled turn) | the agent's admins (`Agent.is_admin`) | the agent's admins |
| **Work an agent starts for you** (a dispatched session) | you + the parent conversation's people (`services._inherit_participants`) | the same |
| **Coaching** | + the agent the person granted (`SessionReviewGrant`), read-only, never a sensitive one | no — it reads what is already uploaded |

**Every session says who can see it.** `GET /api/canopy-sessions/{id}` carries `audience`
(`{label, extras, count}`, `apps/canopy_sessions/audience.py`) — e.g. "Only you", "You and 2
people you added", "Everyone on this email thread (4)", "Members of #channel", "Hal's admins
(the agent's own work)", plus extras like "The Supply project team" or "Trellis may review
(coaching)". It is built from the same leg pieces as the ACL, and
`tests/test_session_audience.py` holds its readers equal to `access.can_read` for every
session shape. The chat page and the embedded panel show it as one line at the top.

The email and Slack rows describe the doors those channels open (participant rows
written when a person is on the thread or in the channel); the session ACL itself
only honours the rows.

## Who is in a session

A session is visible to the people who were in the conversation where it happened, plus
whoever they add. A `SessionParticipant` row (EDITOR) is the grant, made at the channel's
door; the ACL itself stays `apps/canopy_sessions/access.py`. Never a non-member, a contact,
an agent's login or a system account, and never into a sensitive conversation.

* **Email: everyone on the thread** (`harness.channels.email.admit_thread_people`) — the
  members among the From, To and Cc of each inbound message whose From is aligned (DMARC,
  or DKIM signed by the From: domain), joined as each message arrives; an unaligned
  message admits nobody, so a forged From cannot Cc someone into a thread's history. The
  agent's own replies admit their To/Cc (`mail.agent_mail._admit_recipients`).
* **Slack: everyone in the channel, full access — as in Slack** (`apps/slack/audience.py`) —
  every linked canopy member (`SlackUserLink`, or auto-linked by verified email) in the
  conversation the session lives in (channel members; a DM's or group DM's people) is an
  editor, synced from `conversations.members` (cached 5 min) on each inbound message and
  by a 10-minute sweep of sessions active in the last 14 days; one the channel brought in
  (`SlackAudienceMember`) leaves when they leave the channel, never on an answer Slack did
  not give. Contacts and guests see it in Slack only.

## Slack threads

Who may write into a Slack thread's conversation is an access decision, so two
Slack behaviours live here (`apps/slack/`, pinned in `tests/test_slack.py`).

* **A confined reply in someone else's thread gets a thread of its own**
  (`services._fork_confined`). When a message in an existing thread would run
  confined — asked of a probe turn through `access.decide_for_turn`, so it cannot
  disagree with the real stamp — and the thread's session is not the sender's,
  canopy posts a new top-level message, opens a session keyed to THAT thread
  (owned by the sender, `metadata.slack_forked_from` / `slack_forked_session`,
  seeded with the thread they were reading), and links it from the original
  thread. Sharing the owner's session would queue behind the owner's turn
  (`routing.blocking_turn`) and answer in their thread from a `cx-` session
  nobody can find. One fork per (thread, person); writing in the original thread
  again continues it.
* **`@canopy branch <ask>` splits a conversation into a new thread**
  (`apps/slack/branch.py`). Inside a thread with an agent session, anyone who may
  write into that conversation (its owner or an editor — a member in the
  channel is one — or an admin of its agent) gets a new top-level message, a
  child `Session` (`parent_session`, `parent_claude_session` from the parent
  binding's `transcript_id`, `metadata.slack_branched_from`) keyed to the new
  thread, and a first turn seeded with the parent conversation, condensed by
  `canopy_sessions.exports.build_branch_seed` (tool output dropped, capped at
  `BRANCH_SEED_CHARS` = 30k, keeping the first ask and the end). The original
  thread gets a link and carries on; branch as often as you like. The seed is a
  prompt, not a Claude fork — emdash creates tasks through its UI and cannot pass
  `--fork-session`. A viewer cannot branch (they could not write into it), and a
  confined reply never branches (it forks, above).

## What agents know about a person (the fleet brain)

`apps/contacts/people.py` + `people_api.py` (`/api/people/…`). One
`Person` per human — a member (`Person.user`, joined to their correspondent row by
a VERIFIED address only) or a contact (`services.person_for(user=…|contact=…)`).

* **What may be kept: work context only.** `PersonFact.kind` is closed —
  `role | project | instance | preference | correction | terminology` — by a DB
  CHECK constraint as well as the API (anything else is a 400). No health,
  personal life, performance judgements or sentiment. Facts are append-only:
  superseded (same person, same workspace) or retracted, never edited.
* **Per workspace (v1).** A fact is written in one workspace and served
  only there — the envelope carries the facts of the turn's agent's workspace
  and no other. Reading or writing needs membership of that workspace (an agent's
  login is a member), AND the person must be someone that workspace deals with
  (`people.known_in`: a member, a contact, an initiator of a turn there, or the
  subject of a fact there) — otherwise 404, so an id or an address confirms
  nothing across tenants.
* **Raw conversations stay private.** The people API's one read of turn content
  is `GET /api/people/{id}/conversations/?agent=<slug>`: the turns
  that person started with THAT agent, for that agent's OWN login only. An admin
  of the agent may call it but sees only turns the turn ACL already shows them.
  No other agent, and no plain member, can read them. A fact's `source_turn` is
  a link, readable only by whoever can already read the turn.
* **The person decides what agents get — per session, per agent.** Two switches,
  record (gates writes) and use (gates reads), each canopy-level `available` +
  `default` with a per-session override; they are policy, never a grant — an agent is
  served only under a grant the person gave THAT agent, and only for people the
  `HCP_PEOPLE` rollout gate names. Switches, grants, the envelope's `hcp` block and
  the rollout gate: [`hcp-service.md`](hcp-service.md) § "The person's record/use
  switches", § "canopy's own agents: grants per agent, only by the person's act" and
  § "Who has memory at all — the rollout gate".
* **The subject sees everything.** `GET /api/people/me/` (the page
  `/people/me/`): every live fact in every workspace, and the last
  50 reads. Every read — the envelope's and the API's — is a `PersonAccess` row.
* **Who may retract**: the person, whoever asserted it (the user, or the asserting
  agent's login), or a workspace admin (`members.manage`).
* **Who writes: the session that learned it.** The agent records a fact in the
  turn where the person said it (HCP `addPreference`, `turn` = that turn), prompted
  by the envelope's person block and canopy's turn checklist. There is no
  follow-up digest turn and no stored per-person digest — don't add a second pass
  over a transcript; the session doing the work has the context.
* **Retention** (`apps/retention`) scrubs turn content, not facts; a
  fact whose source turn was scrubbed keeps its statement.

### Project participants, coverage, and contact notes

* **Project participants.** `AgentProject.participants` (through
  `ProjectParticipant`: `role` free text ≤ 80, `source` fact | turn | manual | summary |
  review | backing | reply | checkin,
  `created_at`; one row per project and person, the FIRST source wins). Filled
  automatically (`apps/agents/participants.py`): any fact filed against a project
  makes its subject a participant (`people.record_fact`), and a task in a project
  whose `raised_by` turn a HUMAN started makes that human one (task create, patch,
  `set_task_project`). Served only inside the agent's workspace: on the project
  detail (`participants`: id, display_name, email, role, source, since — same gate
  as the project), at `GET /api/people/{id}/projects/?workspace=` (same gates as
  `GET /api/people/{id}/`, logged), and in the envelope `person.projects` (id,
  ext_id, name, agent — at most 5, most recently active first, archived left out;
  additive, envelope stays v3). Backfill: migration `agents/0041` (facts) and
  `manage.py backfill_project_participants` (facts and task raisers).
* **Coverage — a dead brain must be loud.** `GET /api/people/coverage/?workspace=
  &days=7` and `manage.py people_coverage --workspace <slug> [--days N] [--json]`
  (exit 1 when unhealthy), both from `apps/contacts/coverage.py`. Per agent:
  human-started turns, how many were handed a NON-EMPTY person block (≥ 1 live
  fact), facts the agent recorded IN-SESSION (asserted by it, sourced from one of
  those human turns), and how many people. "Non-empty"
  is recorded when the envelope is BUILT, as `PersonAccess.had_context` on the
  envelope read — not re-derived later from facts that have since changed.
  **Rule:** `healthy` = with ≥ 10 human turns, ≥ 1 fact recorded in-session; the
  workspace is healthy when every agent is. Counts only, no person named. Members of the
  workspace; an agent admin who is not a member (an inherited owner) sees only the
  agents they administer; anyone else 404.
* **`Contact.notes` is mirrored into a fact.** Each non-empty `notes` is ONE live
  `role` fact (declared, asserted by nobody, statement = the first 500 characters
  flattened to one line), marked `PersonFact.source_contact`. `PATCH
  /api/contacts/{id}/` with `notes` supersedes the previous mirror (clearing the
  notes retracts it), so the brain is the one place agents read. The `notes`
  field is kept. Backfill: migration `contacts/0011` (historical models,
  idempotent) and `manage.py mirror_contact_notes`.

### The HCP service

The fleet brain is each person's Human Context Protocol instance
(`apps/contacts/hcp/`, `hcp_api.py`, discovery `/.well-known/hcp-configuration`),
and apps canopy does not operate may ask a person for parts of it through
canopy's own OAuth 2.0 authorization server (`hcp_oauth.py`, issuer
`<origin>/api/hcp` — separate from the `/oauth/*` MCP sign-in server). `hcpat_`
tokens open `/api/hcp/v1/` and nothing else, and the person's own record/use
settings bound every client whatever it was granted. It is internal-only behind
one gate, `HCP_SERVICE_AUDIENCE` (default `internal`). Clients, consent, tokens,
revocation and every judgment call: `docs/architecture/hcp-service.md`; the
self-audit against the spec: `docs/architecture/hcp-conformance.md`.
