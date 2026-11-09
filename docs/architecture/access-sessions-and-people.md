# Access: sessions, participants and what agents know about a person

Who can see a chat session, who is in one, how Slack threads map onto sessions, and what the fleet brain (the people brain) holds about a person: project participants, coverage and contact notes. The words and the one rule are defined in [access.md](access.md). The HCP service, which lets outside applications ask a person for parts of their instance through OAuth, is [hcp-service.md](hcp-service.md).

**See also:** [access.md](access.md), [chat-sessions.md](chat-sessions.md), [hcp-service.md](hcp-service.md), [hcp-conformance.md](hcp-conformance.md).

## Who can see a session

A session is visible to the people who were **in the conversation where it
happened**, plus whoever they explicitly add — never to someone merely for being
in the same workspace. Multiplayer is always an explicit choice (a participant
row, added by the session's owner). Agents get no implicit access: an agent's own
login reads a session only if it was made a participant, it is running a live
turn in it (`access.own_turn_session`), the person granted it review (a coach), or
the person asked it to review that one session, read through a pinned token (below). Canopy only operates on what a machine has already **sent**; it asks a
machine for more (live streaming, history) only when its owner, or someone they
explicitly added, asks (`access.may_pull`). Adding someone is the session owner's
(`access.can_share`), with one more way in: the owner of the agent a member's
conversation is with (`Agent.owner`, not its agent admins), once in it, may bring someone
new in — a person or an agent — but owns nothing, reads nothing new, and changes or
removes nobody; never a contact's or a sensitive one (`access.can_add_people`). Sensitive conversations stay sealed to
their own people (`tenancy-sensitive-conversations.md` § Sensitive conversations). The rule is
`apps/canopy_sessions/access.py`; the ACL's gotchas are in
`tenancy-session-access-and-retention.md` § The chat-session ACL (CI-enforced).

| Where it happened | Who sees it (read + write unless noted) | Who may pull from the machine |
|---|---|---|
| **Laptop** (a runner-discovered session) | the box's owner (`Runner.owner`), + people they add; the box's runner admins read only (to move it off the box) | the box owner, + people they add as editor |
| **Web chat** | its creator + people they add | the same |
| **Email** | everyone on the thread who is a verified member (§ Who is in a session) | the agent's admins, + the thread's people |
| **Slack** | everyone in the Slack channel (a DM: its people), read + write, as in Slack | the agent's admins, + the channel's people |
| **Panel** (an embedded widget) | the visitor (a contact: `contact_session_q`) | the visitor |
| **An agent's own work** (an alarm, a scheduled turn) | the agent's admins (`Agent.is_admin`) | the agent's admins |
| **Work an agent starts for you** (a dispatched session) | you + the parent conversation's people (`services._inherit_participants`) | the same |
| **Coaching** | + the agent the person granted (`SessionReviewGrant`), read-only, in a turn asked by them or by the agent's admin, or dispatched for that one person, never a sensitive one, never one whose words its person deleted | no — it reads what is already uploaded |
| **One session, asked from its page** | + the coach the person asked (`SessionReviewAsk`), as the person, with a token pinned to that session, only inside the chat the ask opened, never a sensitive one, never one whose words its person deleted | no — it reads what is already uploaded |

**Every session says who can see it.** `GET /api/canopy-sessions/{id}` carries `audience`
(`apps/canopy_sessions/audience.py`), built from the same legs as the ACL.
`tests/test_session_audience.py` holds its readers equal to `access.can_read` for every
session shape, so a leg changed in `access.py` fails it until `audience.py` follows.

The email and Slack rows describe the entry points those channels open (participant rows
written when a person is on the thread or in the channel); the session ACL itself
only honours the rows.

**Every change to who can see a session is an event.** A participant change, a Slack
thread bound or detached, a move between workspaces, a person's project sharing or a coach's
grant, and a session becoming sensitive each write one `session.*` event to the workspace's
log, ids only, naming who did it and under which turn, so a workspace admin can say what
happened to a session without reading it (`apps/canopy_sessions/lifecycle.py`,
`chat-sessions.md` § A session's lifecycle is in the event log).

## The weekly coaching pass

A turn nobody asked for (the schedule's sweep) sees the coach's whole roster and reads no session
(`access._review_grant_q`). It starts one turn per person with `reads_for` (`POST /api/harness/turns/`,
`canopy agent dispatch --reads-for`), which only that agent's own login, inside such a turn, may name, and only
for a live grantor (`turn_gates.check_reads_for`). The stamp is the server's (`turn_asker.READS_FOR`,
`enqueue_turn` drops a client's copy). Held by canopy-web on that turn:

* it reads that person's own sessions, its own conversation and its own turn (`access.reach_of`), not
  another grantor's, a channel it was given, or the agent's other work, and its roster is that person;
* it writes mail only to that person's account address (`apps/mail/api.py::_check_scoped_recipients`);
* it gets no GitHub identity (`delegations.github_refusal`, a 409 at the runner's route in `harness/api_runners_credentials.py`);
* anything it starts carries the same scope (`dispatch_stamp`), and naming another person is refused.

The envelope's `reads_for` names the person to the turn, which is who its scripts treat as asking.

A dispatch with no scope is still a turn nobody asked for: it reads nothing (`access._review_grant_q`).
It keeps the full profile, because it must run the mail and ledger scripts. So the box's other
credentials and its ledger file are still in its hands, and the ledger's limits are the script's.

## Asking a coach to review one session

The person whose session it is (`access.is_own_session`, never someone added to it) presses
"Ask <coach> to review this" in the session's menu, with no passkey and no standing grant
(`apps/contacts/session_review_asks.py`, `POST /api/people/me/session-reviewers/sessions/{id}/`).
That writes one `SessionReviewAsk` per (session, agent, person), live for 7 days
(`WINDOW`), opens a chat with the coach owned by the person, and starts a turn there whose
verified asker is them.

**The profile.** Every turn in that chat runs confined in canopy's built-in `session_review`
profile (`published_profiles.SESSION_REVIEW`, `turn_context._review_rule`), whoever asks: owner, admin and
canopy too, as a sensitive conversation does, and a fork or a Slack branch of it stays in it or is
refused. `turn_context.resolve_turn_context` decides this; nothing is started as canopy. The transcript is text someone
may have written to steer whoever reads it, so the turn holds:

* no agent login (`turn_agent_tokens.refusal`), no GitHub identity (`github_token_for_turn`), and on
  a cloud runner no agent `.env`, no vault and no mail credentials (`runner/ec2/cloud_runner.py::_confined_env`);
* a confined caller token that lists only `who_is_asking` and `read_asked_session`, refused on REST
  (`_authenticate_caller_token`), so no send, task, dispatch or write exists for it;
* a profile with no Bash and no Write, which `profile_guard` and the native deny rules hold on the
  box. Those two are the box's: on a laptop every session is the same OS user
  (`access-confinement-and-act-as.md`, Known limit).

**The read** (`GET /api/canopy-sessions/asked-review/messages`, tool `read_asked_session`) takes no
session id. Only the turn's caller token gets through. It finds the live ask its conversation was
opened by and its asker made, then reads that session through the session ACL as the person, held to
`access.turn_reach`, which for this chat is itself and that session. Any other session is refused, on
every route that asks the ACL, whatever a transcript asks for. A later turn asked by someone else in
that chat finds no ask. Never a sensitive or contact conversation, even if it becomes sensitive after
the ask. The envelope's `session_review` names the session to the turn, and `audience` says so on the
session's "Who can see this" line. Each change is a `session.sharing_changed` event and on the person's
audit.

**Withdrawing** (`DELETE` on that route) refuses the next read and stops the turn through
`control.stop_session`: a queued turn ends at once, a running one gets `cancel_requested` in its
ledger, which the lease sweep honours if the runner is gone, and bound work with no turn gets
`RunnerBinding.pending_interrupt`. None of that depends on a socket frame. **It cannot take back what
the turn has already read**, nor what it has already said in that chat.
Expiry refuses the next read and stops nothing, since a turn does not run for 7 days.

**A successful injection** can still make the reply wrong or misleading, and can quote this session
back into that chat, which is the person's own. It can read the agent's own repo (`{cwd}/**`). That
is what stays open, and no canopy check covers it.

**Nothing a grant reads, reads this.** It enrols no one in the weekly pass, uploads no other session
and appears on no roster: `is_coached`, `full_upload_session_ids`, `session_reviewers.live`,
`coach_offer`, `granted_to` and `audience._coaches` read only `SessionReviewGrant`, which is why the
scope is a separate table (`tests/test_session_review_asks.py`).

**Who is offered it** (`session_review_asks.state`): only the agent `COACHING_AGENT_SLUG` names, and
only someone in its workspace, since the reply is in a chat there. It says plainly why not when the
session is sensitive, canopy holds none of the session's messages, or `turn_context.resolve_turn_context` gives them nothing
on the agent. Any access opens it, `read_only` too, because that profile is narrower than read mode. The
coach needs no membership of the session's workspace: it reads as the person.

## Who is in a session

A session is visible to the people who were in the conversation where it happened, plus
whoever they add. A `SessionParticipant` row (EDITOR) is the grant, made at the channel;
the ACL itself stays `apps/canopy_sessions/access.py`. Never a non-member, a contact
or an agent's login, and never into a sensitive conversation.

* **Email: everyone on the thread** (`harness.channels.email.admit_thread_people`) — the
  members among the From, To and Cc of each inbound message whose From is aligned (DMARC,
  or DKIM signed by the From: domain), joined as each message arrives; an unaligned
  message admits nobody, so a forged From cannot Cc someone into a thread's history. The
  agent's own replies admit their To/Cc (`mail.agent_mail._admit_recipients`). Those rows
  are on the thread's anchor, which holds nothing to read. The WORK session a message ran
  in takes that one message's members as VIEWERS when the runner records it
  (`email.admit_mail_people_to_work`), so a person sees the agent's whole transcript for
  mail addressed to them and cannot type into it. Only that message's people: a confined
  caller's Cc never opens an admin's session on the same thread. Someone Cc'd only on the
  agent's own reply gets it from the next inbound message that names them.
* **Slack: everyone in the channel, full access — as in Slack** (`apps/slack/audience.py`) —
  every linked canopy member (`SlackUserLink`, or auto-linked by verified email) in the
  conversation the session lives in (channel members; a DM's or group DM's people) is an
  editor, synced on each inbound message and by a periodic sweep (cadence and cache in
  `apps/slack/audience.py`); one the channel brought in (`SlackAudienceMember`) leaves
  when they leave the channel, never on an answer Slack did not give. Contacts and guests
  see it in Slack only. The one agent login ever added is a channel's review agent
  (`SlackChannelIngestAllow.review_sessions`), as a VIEWER. Never into a sensitive
  conversation (`participants.may_join_implicitly`).

## Slack threads

Who may write into a Slack thread's conversation is an access decision, so these Slack behaviours live here (`apps/slack/`, pinned in `tests/test_slack.py`).

* **A confined reply in someone else's thread gets a thread of its own** (`services._fork_confined`). When a message in an existing thread would run confined (asked of a probe turn through `turn_context.for_turn`, so it cannot disagree with the real stamp) and the thread's session is not the sender's, canopy opens a session keyed to a NEW top-level thread, owned by the sender and seeded with the thread they were reading, and links it from the original. Sharing the owner's session would queue behind the owner's turn (`routing_orders.blocking_turn`) and answer in their thread from a session nobody can find. One fork per (thread, person).
* **`@canopy branch <ask>` and `@canopy offline <ask>`** (`apps/slack/branch.py`, `apps/slack/offline.py`) split a conversation into a child session (`Session.relation` `branch`). Who may: anyone who may write into the conversation (`access.can_write`) or an admin of its agent; a viewer cannot, and a confined reply never branches (it forks, above). Offline puts the child in the requester's own DMs with canopy and **its only person is them**: the channel's other people do not come along. The agent may do the same in words (`POST /api/canopy-sessions/slack/offline`, `take_session_offline`), for THAT turn's person, never one it names. Neither takes a sensitive conversation (`slack.services.private_refusal`; the offline route answers 403).
* **Naming the agent picks its conversation:** `@canopy <agent> branch|offline <ask>` acts on that agent's conversation in the thread; unnamed, it is the thread's latest one.

**Lineage: dispatch, branch, fork, and reporting back** (`apps/canopy_sessions/lineage.py`, module docstring). Every child session records its parent the same way and says how it stands to it in `Session.relation`. **Only a dispatch starts with the parent's people** (`lineage.inherits_people`, `services._inherit_participants`); a branch or fork says who is in it itself. Any child may report its outcome back (`POST /api/canopy-sessions/lineage/report-back`, `lineage_api.report_back_to_parent`; an agent names its child by `turn_id`): it is a turn in the parent asked by the child's person, who must be able to write there.

## What agents know about a person (the fleet brain)

`apps/contacts/people.py` + `people_api.py` (`/api/people/…`). One `Person` per human: a member (`Person.user`, joined to their correspondent row by a VERIFIED address only) or a contact (`services.person_for(user=…|contact=…)`).

* **What may be kept: work context only.** `PersonFact.kind` is closed (`PersonFact.KINDS`), by a DB CHECK constraint (`person_fact_kind_is_work_context`) as well as `people.record_fact` (anything else is a 400). No health, personal life, performance judgements or sentiment. Facts are append-only: superseded (same person, same workspace) or retracted, never edited.
* **Per workspace.** A fact is written in one workspace and served only there: the envelope carries the facts of the turn's agent's workspace and no other. Reading or writing needs membership of that workspace (an agent's login is a member), AND the person must be someone that workspace deals with (`people.known_in`), otherwise 404, so an id or an address confirms nothing across tenants.
* **Raw conversations stay private.** The people API's one read of turn content is `GET /api/people/{id}/conversations/?agent=<slug>` (`people_api.py::list_person_conversations`): the turns that person started with THAT agent, for that agent's OWN login. An admin of the agent may call it but sees only turns `turn_access.can_read_turn_content` already shows them. No other agent, and no plain member, can read them. A fact's `source_turn` is a link, readable only by whoever can already read the turn.
* **The person decides what agents get, per session and per agent.** The record and use switches are policy, never a grant: an agent is served only under a grant the person gave THAT agent, and only for people the `HCP_PEOPLE` rollout gate names. Switches, grants, the envelope's `hcp` block and the rollout gate: [`hcp-service.md`](hcp-service.md) § "The person's record/use switches", § "canopy's own agents: grants per agent, only by the person's act" and § "Who has memory at all — the rollout gate".
* **The subject sees everything.** `GET /api/people/me/` (the page `/people/me/`) shows every live fact in every workspace and the recent reads. Every read, the envelope's and the API's, is a `PersonAccess` row (`people.log_access`).
* **Who may retract** (`people.retract`): the person, whoever asserted it (the user, or the asserting agent's login), or a workspace admin (`members.manage`).
* **Who writes: the session that learned it.** The agent records a fact in the turn where the person said it (HCP `addPreference`, `turn` = that turn), prompted by the envelope's person block and canopy's turn checklist. Do not add a second pass over a transcript: the session doing the work has the context.
* **Retention** (`apps/retention`) scrubs turn content, not facts; a fact whose source turn was scrubbed keeps its statement.

### Project participants, coverage, and contact notes

* **Project participants** (`ProjectParticipant`, `apps/agents/participants.py`). One row per project and person, the FIRST source wins. Filled automatically: any fact filed against a project makes its subject a participant (`people.record_fact`), and a task in a project whose `raised_by` turn a HUMAN started makes that human one. Served only inside the agent's workspace, on the same gates as the person (`GET /api/people/{id}/projects/?workspace=` is logged) and in the envelope's `person.projects`.
* **Coverage: a dead brain must be loud** (`apps/contacts/coverage.py` module docstring; `GET /api/people/coverage/?workspace=`, `manage.py people_coverage`, exit 1 when unhealthy). Per agent it counts human-started turns, how many were handed a NON-EMPTY person block (recorded when the envelope is BUILT, as `PersonAccess.had_context`, not re-derived from facts that have since changed), and facts the agent recorded in-session. `healthy` is the rule in that docstring. Counts only, no person named. Members of the workspace read it; an agent admin who is not a member (an inherited owner) sees only the agents they administer; anyone else 404.
* **`Contact.notes` is mirrored into a fact** (`people.py::NOTES_KIND`): each non-empty `notes` is ONE live declared `role` fact, marked `PersonFact.source_contact`. `PATCH /api/contacts/{id}/` with `notes` supersedes the previous mirror (clearing the notes retracts it), so the brain is the one place agents read. The `notes` field is kept.

