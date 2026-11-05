# Access: direct use, admission rules and the caller envelope

How a person who is not the agent's owner uses an agent directly, what a caller's turn tells the owner, the admission rules a workspace's admins write to refuse or let turns run `auto`, and the caller envelope an agent receives with a caller's turn. The words and the one rule (`apps/agents/access.decide`) are defined in [access.md](access.md).

**See also:** [access.md](access.md) (glossary, roles, access list, decision table), [access-confinement-and-act-as.md](access-confinement-and-act-as.md), [access-sessions-and-people.md](access-sessions-and-people.md), [roles.md](roles.md).

## Direct use of an agent

A person may run an agent in their **own** Claude Code, with the agent's plugin and
skills, and canopy keeps the record (`apps/harness/local_turns.py`). The word for it is
**direct** (against a session a runner box claims). The stored names stay `local`:
`Turn.origin = local`, `LocalTurnIn`, `/api/harness/local-turns/`; prose and screens say
direct (a direct turn).

* A **private runner** (`PrivateRunner`) is a person's machine: plain Claude Code with
  the canopy plugin, no runner app. It is registered once with the person's own
  token (`canopy private-runner register`) and belongs to the PERSON — one serves
  every agent they use. On its own it never claims a turn and nothing routes to it.
* When the machine also runs the runner app, the two are **one private runner**:
  `PrivateRunner.runner` links the registration to that `Runner`, and the runner is
  marked **`private`** (`PUT /api/harness/runners/{id}/private`, owner only). A
  private runner takes no fleet work — no routed turn, no workspace order, no source
  rule (`Runner.directed_only`) — only what its owner pins or binds to it, plus the
  direct sessions on it. Its device key is what those sessions prove themselves
  with, and it may read an agent's secrets while its owner holds an active license
  (`device_keys.private_runner_licensed`), for that owner's own turns only.
* A machine **without** the runner app still gets its own key for reading the
  agent's vault: registering it (`POST /api/private-runners/`) gives it a
  `computer` runner (`Runner.COMPUTER`, `local.ensure_computer_runner`, returned as
  `runner_id`) to enrol a device key on. The key is owner-approved like a private
  runner's and serves exactly the agents its owner administers or holds an active
  license for; its token reaches only the owner's direct turns and license reads
  (`runner_auth._COMPUTER_RULES`), and the computer appears in no fleet runner list.
* **Acting as a lower role.** `acts_as` (`editor` or `viewer`, workspace roles) is a property of
  ONE turn or of one client's requests — never a setting left on a machine:
  - a turn started with it (`TurnIn.acts_as`, `canopy agent dispatch --as editor`;
    `LocalTurnIn.acts_as` for a direct session) is judged at that role from start to
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
  `Agent.is_admin`, `relationship_for_user`, `caller_runs_agent` — so a capped owner or agent
  admin is neither, and `decide` gives them only what the access list does (their own lines,
  and any workspace line at the lowered workspace role). It only ever lowers, so anyone may ask
  for it with no proof. It is how anyone, an agent included, runs the path of someone who is
  not an agent admin for real.
* **No PAT on a private runner.** The owner's direct sessions there sign in with the
  box's own device key (its runner token), which on a private runner also reaches the
  local-turn routes (`runner_auth._PRIVATE_RULES`) — never `/api/private-runners`,
  which takes the person's own token — and reads credentials under the owner's
  license. Such a turn's initiator assurance is `private-runner-key` (verified, like
  a PAT). A shared runner's token opens no direct session and reports on none, and a
  private runner's reaches only its local-turn routes, for its own registration.
  Only the owner registers or approves a device key on a private runner
  (`runners.may_trust_device_keys`); its runner admins still see and may revoke them.
* A **direct turn** (`Turn.origin = local`) is one session there with one agent,
  opened by the canopy plugin's SessionStart hook
  (`POST /api/harness/local-turns/`). It is a real turn: the person is its
  initiator (verified, `via=local`), it is RUNNING from the start and never
  claimed, and it carries the same caller envelope a runner turn gets
  (`trigger.private_runner` names the machine; `trigger.runner` is null).
* **Who may.** Exactly whom `decide` gives the WHOLE agent (rows 1–3): its owner
  or an agent admin, anyone whose lines give `write`. A read_only or capability line, or a contact, is refused
  (403) and keeps the confined path through canopy.
* **Mode.** `manual`, always — except an agent admin who asks for `auto`. Stamped
  at open, like a dispatch's request; an editor does not get the
  routing ladder here, because nothing about a session on their own machine is an
  entry point the owner set a posture for.
* **It never holds the agent.** A direct turn is the person's own session: it neither waits
  behind the agent's runner turns nor delays them, and two people may use the agent at once.
  (No agent turn holds the agent either: an agent is not a lane.)
* A **license** (`AgentLicense`, `apps/agents/credentials/licenses.py`) is person × agent: the
  record that someone uses the agent directly. The access list decides who MAY
  (`decide`, in [access.md](access.md)); the license is who DOES. Requested on the first direct turn and
  issued at once when `decide` gives the whole agent (basis `owner`, `admin` or
  `grant:<line>`, an editor line); anyone else gets a `requested` row the agent's admins
  see, and a refusal. An agent admin **revokes** it per person without editing the
  rule, and a revoked license is never re-issued automatically; **approve**
  reinstates it, re-asking `decide`, so it cannot license someone the access list
  does not give the whole agent. `GET /api/agents/{slug}/licenses` (any member)
  lists holders, their private runners, direct turns, credential reads and last use;
  the agent's Settings → Roles → Direct use shows it.
* **Credentials.** `/credentials/resolve` has a third principal beside `runner` and
  `admin`: **`license`** — an ACTIVE license (re-checked against `decide` on every
  read) AND one of the caller's OWN live turns on that agent named in
  `X-Canopy-Turn-Id`: their running direct turn, or — on their private runner's
  token — a turn of theirs that runner claimed. No turn, no secrets. A request naming
  that turn is a `license` read even from an agent admin or a runner owner. What it is
  handed is `authority`'s direct column: never the agent's canopy login (graded `system`,
  past an editor line's `manual_only` cap), the owner's GitHub delegation, or lent or shared
  credentials. Each read is logged with `via=license`,
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
  workspace admins (`admission.manage`) at `/w/:workspace/settings/admission-rules`. Its
  conditions are any subset of: agent, origin (the `Turn.origin` vocabulary),
  principal (who asked, in this condition's own words: `member` is a canopy user, `contact` an
  outside person with no account, `agent`, `system`; it is not an agent role), access
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
    agent's own mailbox. On a Gmail thread canopy-web never sends it itself (it
    holds no mailbox token it can open): the notice is left `pending`, and
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
    one of its workspace admins turns it on, on the Admission rules page
    (`AdmissionSettings`, `PUT /api/workspaces/{slug}/admission-settings`,
    logged to the event log); while it is off, the notice is recorded as
    `held`.
  * The **owner** gets, off the thread: an email (SES), plus a review ask on
    the agent's board parked on them (Waiting on you). Both name who wrote, the
    subject and thread, the rule and its reason, what the sender was told (a
    `Reply:` line: sent, queued for the runner, off in this workspace, or why
    not), and links to fix it: the rule
    on the settings page (`?rule=<id>`, highlighted), the agent's turn mode and
    runners, its access list, and its turns.
* **Defaults.** Every workspace is seeded with two rows (migration
  `admission/0002`, and at creation for every new one). The first rejects email + confined + manual + cloud, with both
  notices, and is **on**. The second is the same on a laptop (`emdash`) runner
  and is **off**. From then on they are ordinary rows. Disable one, narrow it
  with an `allow`, or delete it (a deleted default is not re-seeded).
* **Refusals as rules.** canopy refuses two
  kinds of turn on its own, before any rule: mail from a contact the workspace
  **blocked** (`channels.email.refused_email_turn`), and a person the agent offers
  **nothing** (no line gives them a role that opens a capability) (`_apply_capability`, access `none`). Those are access
  decisions, so they stand whatever the rules say: no `allow` can cover them,
  and disabling their rule does not let them run. Each is reported through a
  rule instead (`evaluate.at_refusal`): conditions `sender: blocked` or
  `access: none`, which also takes origin, principal, capability and agent but
  never a runner or a mode (a refusal comes before either). A matching enabled
  rule records the ledger event and the fired count and sends its notices, while
  the turn keeps the refusal's own `result_note`. Migration `admission/0005`,
  and every new workspace, seed `blocked-sender` and `offered-nothing`, each
  with **every notice off**, so by default these refusals stay silent. A blocked
  sender gets no reply unless a workspace admin turns one on.

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
  and `no-runner-offline` (24h): ordinary rules, **on**, owner notice only. A
  workspace admin who disables them gets turns that sit queued instead.

## The caller envelope

What the runner hands the agent beside each turn (`apps/harness/caller_context.py`,
VERSION 3 — 3 only added `person` and `trigger.kind`, so a v2 reader is unaffected): `who`, `verified` (THIS message), `role` and `source`
(below), `relationship`, `profile` (`full` | `confined`), `permissions`, `manual_only`, `granted_by`, `capability` (the confined
profile), `turn_mode` (`{mode, basis}`), and:

* **`role`** — the asker's named role on the agent: `owner` | `admin` | `editor` |
  `read_write` | `read_only` | `system`, null when refused or when only named-capability lines
  name them (`granted_by` then says `capability:<name>`). **`source`** — why: `owner` | `admin` |
  `system` | `session` | `list:<line>` (the access-list line that gave it, e.g.
  `list:domain:dimagi.com`), `""` when refused. An agent says "you are read_only here because …"
  from these, instead of guessing. Added without a version bump.
* **`permissions`** — what the asker holds on THIS agent on this turn: a sorted list from the
  agent vocabulary (`read`, `write`, `agent_edit`, `admin`, `own`, and scoped `use:<capability>`),
  closed under the dependency rule (`agent_edit` implies `write` implies `read`). A named role
  gives `roles.permissions_of(role)` (owner, admin and system included; system holds what an admin
  does); an access-list asker gets what their lines give (`Decision.permissions`); a confined turn
  holds only `read` (when their lines give it) and `use:<capability>` for the capability the turn
  runs in, even for an owner whom a sensitive conversation confined; `[]` when refused or for a
  turn with no agent. **`write` is in `permissions` exactly when `profile` is `full`**, so a reader
  may trust either (`tests/test_caller_envelope.py`). **`manual_only`** — `true` when the line that
  gives them `write` says every turn they start runs `manual` (write, but an admin approves what
  goes out); `false` otherwise. Both added without a version bump, computed beside `role`/`source`
  from the one decision (`apps/agents/interface.py::grant_facts`).
  **`agent_edit` in `permissions` is the agent's cue that THIS asker may change the agent itself:
  its skills, its persona and the code in its own repo (`access.may_edit_agent` says the same on
  canopy-web).** An agent whose asker lacks it does the work asked and does not push or merge to its
  own repo; the canopy plugin's runner-side rail enforces that, canopy-web only reports it here.
* **`granted_by`** — a legacy one-word summary of the same decision, kept for older readers;
  new readers should use `permissions` and `profile`, never match its strings: `owner` | `admin` |
  `system` | `session:<role>` | `grant:<line>` (a line that gives `write`) | `capability:<name>` (a
  confined asker in that capability) | `sensitive:<capability>` | `refused`, and `no-interface` for a
  turn with no agent. New values may appear.
* **`access_help`** — on a confined turn only: `{your_role, workspace, role_needed_for_full,
  who_can_grant}`. Why the caller is confined and what would change it, in roles (never people
  or other callers' data); the Slack "Questions only" notice says the same. `null` on a full profile.
* **`relationship`** — an older, coarser field, kept for readers that have not moved: `owner` |
  `admin` | `system` for those, and `member` or `contact` for everyone else (in the agent's
  workspace, or not). It is how canopy knows the asker, not a role: it says nothing about what
  they may do. Read `role`.

* **`agent_repo`** — the agent's own repo as `owner/repo`, on every turn and whoever asks. The
  canopy plugin's own-repo rail reads it, so the rail holds in a session outside the agent's clone.
  `""` means the agent has no repo set, and a turn whose asker lacks `agent_edit` then cannot make
  repo writes from a session where the hook finds none either. `null` means the turn has no agent. An
  envelope without the field (an older canopy-web) means neither.
* **`own_box_turn`** — true only for a verified person's unconfined turn with no agent, on a box
  that person owns. `relationship: owner` cannot say this, since a teammate who owns a session on
  someone else's box is `owner` too. The own-repo rail allows such a turn in an agent's clone, as it
  does a person by hand. An agent's own login, on its owner's box or not, is never one.
* **`ship_grant`** — when ANOTHER agent's verified login that is this agent's
  owner or an agent admin dispatched the turn at it, push / PR / merge in this agent's OWN
  repo are pre-approved even in `manual`. Nothing else is.
* **`sender_trust`** — `{email, reason, expires_at, active}` when an email turn's
  sender was tied to a member although THIS message is not aligned, by a temporary
  `SenderTrust` an agent admin set for that address (`verified` stays false); null
  otherwise.
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
  null only when the asker is not a human (canopy or one of the agent's triggers,
  another agent's login, unknown). See "What agents know about a person" in [access-sessions-and-people.md](access-sessions-and-people.md).
* **`trigger.kind`** (v3) — what canopy-initiated work the turn is
  (`origin_ref.trigger`); null otherwise. **`trigger.agent_trigger`** —
  `{id, name, kind, set_up_by}` when one of the agent's own triggers fired the turn
  (see "Triggers" in [access.md](access.md)); null otherwise. The same object is in `who.trigger`. **`trigger.matched_classes`**
  is kept for agents whose skills still read it and is always `[]`: the caller classes it listed
  are gone, and `role` and `source` say which line admitted the asker.
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
