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
*, verified, origin, capability=None)`, which returns a `Decision(role, access,
capability, may_request_auto, may_pin_runner, manual_only, basis, reason)`. Every
door that makes an agent do work asks it (see "Where it is asked" below);
`tests/test_access_matrix.py` asks it through every door. Don't write a second
predicate for "may this person make this agent do X" — ask `decide`.

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

Nothing is automatic: there is no self-join and no domain auto-join. People at a
listed domain may only **request an invitation**. A person becomes a member in
exactly three ways, and the only code that may create a `WorkspaceMembership` is
`services._grant` (the first two), `services.ensure_member` (the default-workspace
bootstrap) and workspace creation — `apps/workspaces/tests/test_access_requests.py`
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
  **viewer**, **editor** or **admin** (no higher than they may grant) or
  **denies**, with an optional reason. The requester is emailed the outcome. One
  open request per person per workspace; asking again is idempotent. `GET
  /api/workspaces/requestable` lists where you may ask. Notifications go through
  the invite mechanism (labs SES) plus Web Push and are best-effort: a failure
  never fails the request — it is stored on the request (`notify_result`) and
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
  as for its own turns. Pinning is for an **agent admin OR a runner admin**,
  consistent with actor routes, which require the runner admin.

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
workspace member never makes you either (the `dimagi` workspace auto-approves
access requests from a whole email domain).

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
  least one. Its action is `reject` or `allow`. It also carries a reason and two
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
  manual, a session that started manual, or auto withheld from an unverified
  message. It cannot be about refused or unclaimed turns. The use: `ace,
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
* **Defaults.** Every workspace is seeded with two rows: by migration
  `admission/0002` for the workspaces that existed then, and at creation for
  every new one. The first rejects email + confined + manual + cloud, with both
  notices, and is **on**. The second is the same on a laptop (`emdash`) runner
  and is **off**. From then on they are ordinary rows. Disable one, narrow it
  with an `allow`, or delete it (a deleted default is not re-seeded).
* **Refusals as rules.** canopy refuses two
  kinds of turn on its own, before any rule: mail from a contact the workspace
  **blocked** (`_refused_email_turn`), and a person the agent's interface
  **offers nothing** (`_apply_capability`, access `none`). Those are access
  decisions, so they stand whatever the rules say: no `allow` can cover them,
  and disabling their rule does not let them run. Each is reported through a
  rule instead (`evaluate.at_refusal`): conditions `sender: blocked` or
  `access: none`, which also takes origin, principal, capability and agent but
  never a runner or a mode (a refusal comes before either). A matching enabled
  rule records the ledger event and the fired count and sends its notices, while
  the turn keeps the refusal's own `result_note`. Migration `admission/0005`,
  and every new workspace, seed `blocked-sender` and `offered-nothing`, each
  with **every notice off**, which is what these refusals did before. A blocked
  sender still gets no reply unless an admin turns one on.

* **Reach rules: no runner will take it.** `reach: paused` matches a turn when
  every runner that could take it (`harness.claim.turn_reach`, the claim's own
  coverage) is paused with no `unpause_at`; `reach: offline` when none is
  reachable and the turn has waited the rule's `wait_hours`. A pause WITH a
  resume time matches neither: the turn waits, and its status says
  `waiting_runner` with `parked` and `held_until`. Reach is asked at enqueue
  when such a rule is in scope; again for the queued turns a runner could take
  the moment it is paused open-ended (`harness.services.pause` →
  `evaluate.recheck_stranded`); and by `evaluate.sweep_unrunnable` on the
  heartbeat clock (every 5 minutes at most) for the offline wait. A claim
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
  contact and any non-email turn (canopy-web#1265). canopy also LOGS it, once, on
  the turn's event ledger at enqueue: a `TurnEvent` of kind `unproven_member`
  whose payload is this same object, read by `read_turn_events` and shown (with
  the note) in the activity drill-down. canopy writes it; a runner cannot post
  that kind.
* **`person`** (v3) — what canopy knows about the HUMAN asking:
  `{id, display_name, email, workspace, facts[], see_all}` (plus `grant`, `recall`, `projects`).
  null only when the asker is not a human (canopy, another agent's login, a
  system account, unknown). See "What agents know about a person" below.
* **`trigger.kind`** (v3) — what canopy-initiated work the turn is
  (`origin_ref.trigger`); null otherwise.

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

* **Default deny.** No published interface means only the agent's admins and its
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
  `harness.claiming.issue_credentials`): tools run as the caller (no canopy user
  at all for an outside contact), and only the tools the capability lists exist
  (`apps/mcp/turn_scope.py`).
* **A confined turn holds no credentials it does not need.** canopy refuses it
  the owner's GitHub token (`agents.delegations.github_token_for_turn` → 409).
  The cloud runner builds its env from an allowlist (`_confined_env` in
  `runner/ec2/cloud_runner.py`: Claude's login, locale/paths/TLS, which agent and
  turn) — no `CANOPY_TOKEN`, no agent `.env`, no 1Password key (it never calls
  `credentials/resolve`), no chat key; gog's keyring password only when the
  capability's own bash rules run the mail path (`canopy email …` /
  `bin/<slug>-email`).
* **A colleague's FULL turn reads as the colleague.** A full-profile turn not
  asked by the agent's owner, an admin or canopy (a `full:` rule, a workspace
  editor, a writer in an admin's session — `caller_tokens.is_caller_scoped`)
  gets a **scoped** caller token (`CallerToken.scoped`): every tool exists
  (`all_tools`), but each runs as the conversation's current asker, so
  `turn_access.can_read_turn_content` and the session ACL decide what
  `harness_list_turns` / `read_turn_messages` / `read_turn_transcript` return.
  The cloud runner leaves it at `~/.canopy/scoped/turn/<turn id>.token` and sets
  `CANOPY_SCOPED_TURN` (`_scoped_token_env`); the laptop runner leaves
  `~/.canopy/scoped/task/<emdash task>.token` from `execute._confine`
  (`scoped_token.py`) and clears it for a later turn with none. The plugin's
  `canopy-web-mcp-headers.js` sends it and never falls back to a PAT once it has
  one; a write failure refuses the turn. A scoped token does not serve a
  confined turn's conversation, and a confined token does not serve a full turn's.
* **Known limit.** On a laptop every session is the same OS user and emdash
  spawns it, so the runner cannot strip its environment; on any box, credential
  FILES (agent `.env`s, gog's keyring, ace's Drive key) stay readable to an
  escaped session. The guard and the native deny layer are what keep a confined
  session off them.

Spec: `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-and-access-design.md`.

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
  write into that conversation (its owner or an editor — a member who replied in
  the thread is one — or an admin of its agent) gets a new top-level message, a
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
* **Raw conversations stay exactly as private as before.** The one new read of
  turn content is `GET /api/people/{id}/conversations/?agent=<slug>`: the turns
  that person started with THAT agent, for that agent's OWN login only. An admin
  of the agent may call it but sees only turns the turn ACL already shows them.
  No other agent, and no plain member, can read them. A fact's `source_turn` is
  a link, readable only by whoever can already read the turn.
* **The person's own settings decide what agents get — per session.** Two
  features, record and use, each with a canopy-level `available` + `default`
  (`Person.hcp_*_available` / `hcp_*_default`, `PUT /api/people/me/agent-memory/`
  `{record?: {available?, default?}, use?: {…}}`) and a per-session override
  (`SessionAgentMemory`, `PUT /api/people/me/sessions/{id}/agent-memory/`
  `{record?, use?}` = `on|off|inherit`). Effective for a turn = available AND
  (that turn's session's override, else default); a turn with no chat session
  uses the defaults; a session never turns on what is not available (403).
  Everything starts unavailable; Jonathan is record available + on by default,
  use not available (migration `contacts/0019`).
  * record, "Agents may learn about me", gates WRITES: HCP add/create/update/
    delete, the legacy `POST /api/people/{id}/facts/` by an agent's login, and
    the envelope's `record` hint.
  * use, "Agents may use what they've learned", gates READS: HCP search/get, and
    the facts and `recall` in the envelope's `person` block.
  * These settings are POLICY, never a grant. An agent is served only under a
    grant the person gave THAT agent in a session (`hcp.issue_agent_grant`,
    `POST /api/people/me/sessions/{id}/agent-grants/`): "allow for this session"
    (temporary, ≤ 24 h, ends with the session) and then, as a separate act,
    "keep allowing" (persistent). Nothing is presumed; migration `contacts/0022`
    revoked the presumed grants. See `hcp-service.md`.
  * A refused operation is `scope-denied`: first the switch
    (`hcp_api._principal(need=)` → `hcp.require(person, need, turn)`), then the
    agent's grant (`hcp.NOT_GRANTED`). With nothing granted for what is on, the
    block has `grant: null`.
  * The block always carries `hcp: {record, use, available: {record, use},
    session, agent, granted: {record, use}, awaiting_grant}` — `record`/`use` are
    the switch AND this agent's grant; `awaiting_grant` lists features on in this
    session that the person has not granted this agent yet (the UI asks, never the
    agent). An envelope read is logged only when a read is actually granted.
  * Record-only builds a model of the person that no agent sees yet. Without
    `use` an agent cannot search before adding, so duplicates and contradictions
    are left to HCP's conflict quarantine.
  * Nothing is deleted when anything goes off. The person's own access (page,
    export, audit) never changes.
  * Only the person changes them — the canopy level as themself, a session's
    override only for a session they created (`created_by`; anyone else 404) —
    never an agent's login, a caller token, a system account or an admin; the
    routes are off MCP.
  * Each change is its own audit event: `agentRecord|agentUse.enabled|disabled`
    (available), `.defaultOn|defaultOff` (default), and `sessionRecord|sessionUse.
    on|off|inherit` (a session's override). These are canopy's own types, beyond
    the spec's required set.
  * Coverage counts only people who make record available (a session may still
    turn it off; coverage does not see that).
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
  `ProjectParticipant`: `role` free text ≤ 80, `source` fact | turn | manual,
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
(`apps/contacts/hcp.py`, `hcp_api.py`, discovery `/.well-known/hcp-configuration`),
and apps canopy does not operate may ask a person for parts of it through
canopy's own OAuth 2.0 authorization server (`hcp_oauth.py`, issuer
`<origin>/api/hcp` — separate from the `/oauth/*` MCP sign-in server). `hcpat_`
tokens open `/api/hcp/v1/` and nothing else, and the person's own record/use
settings bound every client whatever it was granted. It is internal-only behind
one gate, `HCP_SERVICE_AUDIENCE` (default `internal`). Clients, consent, tokens,
revocation and every judgment call: `docs/architecture/hcp-service.md`; the
self-audit against the spec: `docs/architecture/hcp-conformance.md`.
