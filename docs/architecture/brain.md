# Canopy Brain

One foundation every learning capability uses: what canopy may learn from, who agreed, who sees
the result, and how a removal reaches what was derived. Capabilities (coaching, personal memory,
project learning, system learning) sit on top and are added over time. The build's plan and its
open questions are the epic, issue 2728; this doc is what the code does now.

## What is stored (`apps/brain/models.py`)

Nothing here holds words. A `BrainSource` points at a session (`ref`, plus the typed `session`
pointer); the session's own rows hold what was said, and every rule about them is read live from the
session, never copied.

- **`BrainSource`**: `kind` (`session` now), `workspace`, `state` (`open`/`withdrawn`),
  `sealed_reason` (what the last refresh saw; `eligibility` never trusts it).
- **`SourceParticipant`**: every person whose words are in the source, from rows canopy already has
  (`participants.people_of`): the creator, `SessionParticipant` rows, everyone who wrote a line
  (`Message.author`) or started a turn in it (`Turn.initiator_user`/`initiator_contact`), including
  people since taken out of it, and the owner of the box a runner-discovered session ran on. Agents are
  never participants. A contact is, and makes the source ineligible for every shared use. The rows
  follow the session (`sources.refresh`): on a `SessionParticipant` added or removed, a line or turn by
  someone not yet in it (`signals.py`, after the commit), and on every look by the associator.
- **`LearningConsent`**: one live row per person (`learning_consent_one_live`): `classes`, `workspaces`
  (empty = all of theirs), `include_new`, `standing_rules`, `directives`. **`SourceExclusion`**: one
  person taking one source out, its own row.
- **`BrainCapability`**: something that learns, in one purpose class, declaring the purposes it reads
  for. Registered by a canopy admin (`registry.py`, `/api/brain/capabilities/`).

Sources are made lazily (`sources.for_session`) and warmed by `manage.py brain_backfill`.

## Purpose classes

`individual` and `memory` learn ABOUT a person from their own words: only that person's consent
counts. `project` and `system` are SHARED: every human participant must permit the class and none may
have excluded the source (most restrictive wins). A session is wholly visible for learning if it is visible at all:
everyone in it saw it at the time and knew they were talking to canopy's AI, so consent decides WHICH sessions a
learner reads and never cuts lines out of one. One
participant's exclusion takes the source out of every shared use for good, whether or not they are
later taken out of the session; it stays theirs to see and undo (`/api/me/learning/sessions/{id}/`,
`/api/me/learning/removed/`). What was learned about the others from their own words stays
(`consent.exclude`).

## `eligible` (`apps/brain/eligibility.py::eligible`)

The one question, asked on every request and cached nowhere: may this capability learn from this
source, for this purpose, about this subject? It fails closed. True only if the source is open and not
sealed (sensitive, a contact's conversation, words deleted, or a learning run's own conversation (`brain_run`:
it retells the sessions it read, and its only participant would be the admin its turn answers to): read live
from the session, the sensitive test being `access._not_sensitive_q`'s, held equal by
`tests/test_brain_sources.py`), the capability is
registered, enabled and declares the purpose, the right people consented to the class, none excluded the
source, no standing rule denies it, the source's workspace is within each person's consent and each is a
member of it (a guest is not). Who is in the source is read from the stored rows AND live from the
session (`participants.people_of`), so someone who joined since the last refresh is asked too;
`eligible_sources` reads the session's `SessionParticipant` rows and turn initiators live in SQL the
same way (`tests/test_brain_participants_live.py`). `tests/test_brain_eligibility.py` is the truth table.

**"Learning reads" comes first.** A source is eligible only when its own workspace has "Learning reads" on
(`learning_reads.is_on`; `eligible_sources` filters by `learning_reads.on_slugs`, the same answer for a listing): a
per-workspace setting, off unless set, a workspace with no setting of its own inheriting the nearest ancestor's that
has one (`learning_reads.effective`). Because the check is in `explain`, every learning read follows it without one of
its own: a run's `brain_read` and `brain_emit`, the project readers' leg, the automatic association layers and the
graph's would-be links (`everyone_agreed`, and `agreed_sources` for a listing). Coaching does not go through `explain` (`coaching_allows`), so the setting
does not stop coaching. Nothing is cached; a change is the next request's answer.

A source is judged where its session is NOW: the session's workspace is read live (`_facts`, and the listing's SQL),
never the workspace its source last saw, so a session moved to another workspace is judged there at once.

`eligible` is `eligibility.explain`, which says why. Without a capability, `explain` asks the same question of a
purpose class (what `everyone_agreed` asks).

A standing rule is deny-only: `{capability?, purpose?, class?, workspace?}`, denying when every key it
names matches. A rule the code cannot read denies (`rule_denies`).

## Consent: the person's, and an admin may opt people in (`apps/brain/api.py`, `apps/brain/consent.py`)

`/api/me/learning/` reads, sets and ends a person's consent; `/api/me/learning/sessions/{id}/` takes
one of their sessions out and back. **Every person-only brain route asks one question, `person.refusal`**: the
person's own browser session or a PAT they minted by hand, never an MCP call, and never what
`harness.acting_person.refusal` refuses (an agent's login, a turn's login or token, a request naming a turn, a
runner). An OAuth client signed in as them and a site's or MCP host grant's delegated token speak FOR them and are
a 403 too (`tests/test_brain_person_only.py`). The same question opens the manager leg and the reporting routes. None
of these is an MCP tool (`apps/mcp/api_tools.py` `EXCLUDED`).

**An admin may opt people in** (`POST /api/w/<ws>/brain/consent`, `opt_people_in`; `GET` lists who agreed to what): a
manager of the workspace's learning (`actor.of`, so a person or an admin agent) ADDS classes to the consent of named
members of it or below, or of everyone there who already agreed to any class (`already_agreed`). It only adds
(`consent.admin_opt_in`): dropping a class, narrowing, revoking and withdrawing a session stay the person's. A person
with no consent gets one covering only their workspaces the admin manages (no new ones); one whose consent reaches a
workspace the admin does not manage is refused for that person. Each change is on the person's own log naming who
made it, and the act is a `SettingChange`.

**Managing a workspace's learning is not a person's own choice, and an admin agent may do it.** One rule,
`actor.of` (`apps/brain/actor.py`), answers every management route: "Learning reads", a learner's switch, operator, entry
and scope (and becoming accountable for a learner nobody answers for, which makes the PERSON accountable, never the
agent), targets (add, take away, budget), Run now, a `system` or `project` finding's status, acknowledging a flag, and
the brain page's reads and why-not lookup. The caller is the person as themselves (`person.refusal`), or an agent's
own login inside its own running turn (the per-turn login, `turn_asker.serving_turn`; never a stored token) whose
turn answers to a person (`turn_asker.accountable_person`: its verified asker, the person it was started for, or its
schedule's creator); `Actor.may_manage` asks that the login AND that person each own or administer the workspace
(`perms.BRAIN_MANAGE`, inherited roles count) or be a canopy admin. A project target, and a `project` finding, are also
the project's lead's and admins' (`api_manage.may_manage_target`), and an agent's working for one of them once it is a
member agent of the project. A learning run's own turn (its conversation stamped `brain_run`, canopy's `brain-run`
initiator, or a credential or `X-Canopy-Turn-Id` naming one) is refused every management route, person or agent, so a run
never widens itself. An OAuth client, a delegated, act-as (`act_as.EXCLUDED_PATTERNS`) or caller token, a guest, a
contact and a non-member are refused. These routes are MCP tools, since agents reach canopy through MCP; a person's
token over MCP is still refused. Every management write names who acted: a setting's `SettingChange.acting` (agent,
turn, the person it answers to; `audit.note_acting`), shown on the page as "by <agent> for <person>", and an event for
Run now, a flag or a finding's status (`actor.event`, `findings.set_status`) (`tests/test_brain_agent_admin.py`).

`PUT /api/me/learning/` takes the whole set of `classes`; every other field (`workspaces`, `include_new`,
`standing_rules`, `directives`) changes only when sent, so a class toggle never moves the scope or resets
`known_workspaces` (what makes a workspace joined later "new"). Sent workspaces the person has since left
drop out; one they were never in is a 422, and so is a list left empty by that (empty would mean all).
A change that narrows the consent (a class dropped, fewer workspaces, `include_new` turned off, a standing
rule added) runs the removal cascade over the person's sources (`consent._narrows`).

Each class's `summary` and `seen_by` (`consent.CLASS_COPY`) say plainly who sees what: `individual` lets
the person's coaches read their full transcripts and their manager see only what was learned; `memory`
only the agents they grant; `project` anyone with access to the project, reading the session scrubbed,
and only when everyone in it agreed; `system` canopy's learning systems.

The person's controls: Settings → Learning (`/settings/learning`, `LearningPanel`) holds the class
switches, who manages them (only their own reporting lines), what was read, and the sessions they took
out, each linked to its page with "Let learning use this again". On a session's page, the ⋯ menu's
"Learning…" (`SessionLearning.tsx`) shows only to a person in the session (the route answers anyone else
404 or 403): it removes the session from learning or lets it back in, and lists the projects it is tied
to with "Not this project" (`rejected`), "Belongs here" (`core`), "Take back my call" for a declared
link, and a pick to tie it to another project of its workspace.

Every change is on the person's own audit (`learning.consented|class_added|class_removed|revoked|
source_withdrawn|source_restored` in `PersonAuditEvent`) and, ids only, in the event log of each
workspace they are in (source `brain.consent`); taking one session out or back is logged only in that session's
own workspace, never another organisation's. `brain.consent` and `brain.read` events are kept from the event log's
pruning (`events.keep`): the cascade asks who ever agreed (`cascade.ever_agreed`) and the disclosure summary is
recomputed from the reads.

## Coaching is `individual`

`CoachingOptIn` still drives the coaching session ACL (leg 5 of `access.py`) and the runner's full
upload (`coaching.full_upload_session_ids`). Both honour removal: a session anyone in it took out of learning
(`access.removed_from_learning`) is read by no coach and shipped for none, and a person is coached only where
their consent allows coaching (`eligibility.coaching_allows`): the workspaces it covers, less any where a standing
rule denies the `individual` class or the `coaching` learner for either of its purposes. The two are kept the same in
both directions: coaching's own opt-in or out mirrors into the `individual` class
(`consent.mirror_coaching`, called by `session_reviewers`), and setting or dropping `individual` through
the learning routes writes the opt-in (`consent._sync_coaching`). Migration `0002_copy_coaching_opt_ins`
carried every live opt-in over; the old table stays until a later release drops it.

## Which project a source belongs to (`apps/brain/associate.py`)

`SourceLink` ties a source to a subject (a project today), one row per pair, in four layers where the
strongest wins (`associate.rank`): **dispatched** (a turn raised from a task of the project ran in the
session: certain), **signals** (the project's `ProjectSignature` matched against the session's recorded
`activity`; `associate.classify` is canopy's `project_history.classify`, ported), **judged** (a judge
run's ruling, below) and **declared** (an agent at close-out, or a person). A link is metadata and
confers no read by itself; only a `core` link opens the project leg.

- **Signals, not words.** Layer 2 reads `Session.activity` (repos, edited paths, PRs, MCP tool calls,
  branches) and the title; it never reads a message. Strong signals (owns a project PR, edits project
  paths, `mcp_min` or more project tool calls) make `core`, a lone owned PR makes `adjacent`, name terms
  and branch patterns alone make a `candidate`.
- **A signature is seeded from certain sessions only** (`associate.seed_signature`): from `core` links
  that were dispatched or declared, never from a signals link, so a guess cannot feed itself. It only
  widens; nothing a person added is removed.
- **A person's rejection sticks.** A person's `rejected` declaration outranks every automatic layer and
  every agent; only that same person undoes it (`undeclare`). Another person rejecting it too is recorded
  (`SourceLink.rejected_by`), so when the first takes theirs back the next one's holds it; another person's
  `core` against a rejection is a 409. An agent's declaration outranks the automatic layers but never a person's.
- **Never across a workspace, never a personal project.** Automatic layers link only workspace-visible
  projects of the source's own workspace, and neither a person nor an agent can declare a personal one.
- **Only sessions everyone in agreed to `project`.** The dispatched and signals layers and the
  signature seed skip a source unless every human in it agreed to `project` for its workspace, none
  excluded it and no contact is in it (`associate.may_look`, `eligibility.everyone_agreed`). A declaration still
  stands. A source skipped is looked at again when what skipped it changes (`associate.due_again`): "Learning reads"
  turning on there, a person in it agreeing to `project`, a removal undone.
- **A moved session leaves its links behind.** When a session moves to another workspace its source follows
  (`signals._a_moved_session`, `sources.refresh`) and its links to projects of the workspace it left go
  (`sources.drop_foreign_links`); the project leg and a project run also read only links to a project of the
  session's own workspace.
- **Computed, then written.** `associate.propose` (`propose_dispatched`, `propose_signals`) computes what layers 1 and
  2 would make, reading only; `link_dispatched` and `link_signals` write what it returns, through `put`, whose
  `outranks` decides whether a stored link keeps its place. The relations graph's would-be links are
  `associate.proposals_for`: the same compute over the sessions the associator would look at (`may_look`).
- **Off-request and debounced.** An activity fold (a `Session` save naming `activity`) only marks its
  source due (`associate.schedule`, five minutes, so a burst is one look); the tick `brain.associate`
  (`associate.run_due`) makes sources for sessions that have none and looks at what is due. A sealed
  source (sensitive, a contact's, deleted words) is never looked at.
- **Declaring.** A person: `PUT /api/me/learning/sessions/{id}/projects/{handle}/` (`core` or
  `rejected`), person-only and off MCP like the rest of `/api/me/learning/`. An agent:
  `PUT /api/brain/sessions/{id}/projects/{handle}/` from its own login inside its running turn, for that
  turn's own conversation only (`core` or `adjacent`).
- **Validating a signature.** `associate.compare` measures a project's stored links against a
  hand-checked list of session ids (precision and recall); `manage.py brain_validate_links` runs it.

## What learns (`apps/brain/registry.py`)

A `BrainCapability` is registered by a canopy admin (`POST /api/brain/capabilities/`, superuser only): one purpose
class, the purposes it declares, the source kinds and output kinds it works with, an `operator` agent and the plugin
`entry` a run starts with. It starts dark (`enabled` false). A learner may ship preset with its operator and entry (`registry.PRESETS`:
`system-learning` with `ada` and `/canopy:brain-learn`, applied by `registry.apply_presets` and migration `0017` when it has
no operator yet and the agent exists, never over a choice someone made), so turning it on is one switch plus a target. Its runs act for an accountable admin
(`registry.accountable`: who registered it, while active); a canopy admin who enables or changes a feature nobody active
answers for (a seeded learner, or one whose registrant left) becomes its accountable admin, and enabling one with nobody
is refused. Its audience is its class's (`registry.CLASS_AUDIENCE`)
and may only narrow: a wider one is refused, because that is a new class each person must agree to. Its `scope` is how
much of a session its runs read, a view of the core read (`transcript_read.VIEWS`): the whole session (`full`) unless
narrowed, and it too may only narrow; each change is an event (`brain.capability`, `capability.scope_changed`) on its
operator's workspace. The built-in
`project-readers` (migration `0005`) is the purpose a project's readers read a session under.

## Who sees what it produced (`apps/brain/outputs.py`)

Outputs (`BrainOutput`) are read by their learner's audience (`outputs.visible_to`, `GET /api/brain/outputs/`): each leg
below opens only for a learner whose `audience` names it (`person`, `coaches`, `manager`; `project_readers`;
`canopy_admins` for the superusers' leg and `agent_admins` for the owners' and admins', as the person and as an admin
agent), so narrowing a learner's audience closes those legs and a learner with none is read by no one. A person's own
`memory` outputs are always theirs. The legs:
`individual` by the person, their coaches (the same rule as reading their sessions, and only outputs made in the
workspaces they coach, `access.coach_scope`) and their manager; `memory` by
the person; `project` by the project's readers; `system` by canopy admins and the owners and admins of the output's workspace
(`perms.BRAIN_MANAGE`; inherited roles count, so an ancestor's owner or admin sees a descendant's findings and a
sub-workspace's or a sibling's never sees them; this leg opens to the person as themselves and to an admin agent in its
own turn (`actor.of`: where its login and its turn's person both manage, `Actor.managed`), never an agent's stored token
or a person's token over MCP; a run started for a workspace writes its findings as that workspace's, `emit`). Pinned to a
workspace, the route answers only that workspace's outputs and the ones below it. Only `active`
outputs are seen: a withdrawn or stale one is seen by no one. Outputs are derived, so they stay readable after
a workspace turns "Learning reads" off; nothing new is made from its sessions then. `ReportingLine` (`apps/brain/reporting.py`, set by a workspace
owner by hand, each change an event) is the manager relationship; it opens a manager's reading of their reports'
`individual` outputs made in the workspace the line is in, and nothing else, and only to the manager as a person
(`api_admin.manager_leg_open`, which is `person.is_person`: their browser session or a PAT they minted by hand, as HCP's governance acts; never an
agent, a turn's token, an act-as token, an OAuth or MCP client or a site's delegated token, and never an MCP call:
`list_outputs` is an MCP tool, and through it the other legs answer but the manager leg stays shut). The reporting routes are person-only
too and are not MCP tools; setting a line names "no such member" alike for an unknown address and a non-member.

## Reading as a project's reader (`apps/brain/readers.py`)

A session `core`-tied to a project is readable, as a seventh leg of the session ACL (`access._brain_project_q`), by
every PERSON who may read that project, when `eligible_sources` passes it for `project-readers`. Never an agent's login
(an agent reads a project's sessions through a brain run) and never a request held to a turn's reach (a caller token).
The leg is opt-in per surface (`access.visible_session_q(brain=True)`): only the session list and search, `GET /{id}`,
its scroll-back and human inputs, and the brain's project listing carry it. Every other surface (turn prompts, ledgers
and raw transcripts, threads, huddles, attachments, the chat socket, exports, Slack, people) reads legs 1 to 6, so a
leg-7-only reader is refused there and a new surface fails closed; `tests/test_brain_project_leg_surfaces.py` lists the
routes. On the surfaces that carry it the reader is a viewer (never a write, share, bind or pull) and the transcript
comes out scrubbed (`common.redact`, via `transcript_read.brain_redacted`), with each read leaving one `brain.read`
event (ids only; reads of a session by a reader coalesce). A list row is metadata, not a read: its title scrubbed,
no opening or last reply (`session_row.BRAIN_ONLY_BLANKED`), and a text search never matches such a row. A page asks
which rows are leg-7-only once (`access.brain_only_ids`). The leg and the record are inert for a session whose workspace has "Learning reads" off.
`eligible_sources` is `eligible` for a whole listing: the structure is SQL and what is not (a consent's classes and
workspaces, standing rules, who is a member where) is judged in Python over the people who consented, once, so nothing is
cached; `tests/test_brain_project_leg.py` holds the two equal over a world.

## A run (`apps/brain/runs.py`, `reading.py`, `emit.py`, `api_run.py`)

The server starts a run of a feature (`runs.start`) for a target (above): a conversation of the operator agent stamped
`metadata.brain_run` (and `brain_target`), with no creator and no participant, and a first turn queued as canopy's own,
accountable to the target schedule's creator (the learner's accountable admin). A run carries a workspace `scope` (`BrainRun.scope`): it reads only
eligible sources of that workspace and the ones below it (`reading.candidates`, `reading.subtree`, the existing
`descendant_slugs` walk), and what it emits is that workspace's (`emit`). Eligibility, consent and the cascade are
unchanged: the scope only narrows what a run reads and who sees what it found. A `system` run's subject is its scope,
so one run is open per feature and workspace.
**The run's turn is an ordinary full-profile turn of the operator** (`turn_context` resolves it like any system turn of
the agent): its entry skill may do anything the agent's turns may (a task, a PR, a draft, a finding), and outbound
follows the operator's turn mode and rails. What makes it a learning run:

- **Its context is the learning system's** (`context.py`): the learner, the target, the accountable person, the run,
  the sessions due (by source id and count, never a word) and its budget, in its first prompt (`runs.prompt`) and in
  its caller envelope as `brain_run` (`harness.caller_context`), read with `who_is_asking`. It is one typed, versioned
  document (`context.document`, `schemas.BrainRunContext`, `context.VERSION`: a field added is not a new version, a
  field whose meaning changes is). The one a run was started with is kept on it (`BrainRun.context`) and shown on the
  brain page with the run, less the ids of the sessions due (`state.context_shown`).
- **It reads the sessions due only through `brain_read`.** Its login reads its own conversation and nothing it is due
  (`access.turn_reach`; the sessions are other people's), so every read is recorded and eligibility, consent and the
  cascade apply unchanged. `brain_read` and `brain_emit` answer only a turn in the run's own conversation while it runs,
  by its credential (a caller token's turn, or the per-turn agent login a full turn reaches canopy with,
  `lineage.turn_of_request`), and only while "Learning reads" is on somewhere it reads (`learning_reads.run_refusal`).
- **It is its own session with nothing carried over.** Its conversation has no creator and no participant, so no other
  turn of the operator, its asker or its login reads it (`tests/test_brain_full_profile.py`); it is never a learning
  source (`participants.sealed_reason`). On the box it is `isolated` (`harness.isolation`): the claim gives it only to
  a runner whose code reports `isolated` at `ISOLATED_VERSION` on its heartbeat, which turns Claude Code's auto memory
  off for it (`runner/ec2/cloud_runner.py` `ISOLATED_TURN_ENV`), so what it read never reaches the memory the agent's
  other turns load, and deletes its Claude session (transcript, tool results, todos) once it ends and its rows have
  shipped (`_isolated_forget_turn`), so no later turn reads it off the disk. A laptop runner does not report it and
  takes none.

- **`brain_read`** serves the run's next eligible sources once each, most recently active first (`reading.activity`, the
  session list's last-activity rule), only those active within its target's window (`budget.lookback_days`, on the
  target: 14 days for a workspace, 30 for a project unless set; an older session is never read and no run walks back
  through a backlog), leaving out what a clean run of the same feature already read for the same subject. A session
  that grew by `reading.REREAD_ROWS` rows past where its last clean read stopped (`BrainRunRead.through`) is due again
  and served from there (`read_before`). Each through the core read (`transcript_read.read`) in the feature's
  scope (`reading.view_of`; a judge never wider than `opening`), secrets scrubbed, never cut by speaker. One call serves
  at most `reading.MAX_CHARS` and never splits a source that fits in a call; a source larger than a call pages across
  calls (`next_index`, passed back as `index`), never splitting a row. The directives of the people in a source travel with it, and the sources removed since the feature learned
  from them are named once (`revoked`, from `RevocationNotice`). Every source served is recorded (`BrainRunRead`, a
  `brain.read` event).
- **`brain_emit`** writes one output of a declared kind, about the run's own subject, citing sources, and a `key`
  naming what it is: a repeat folds into the open finding with that key (below). Every cited
  source must be one this run read and must still be eligible now, or nothing is written. The lineage
  (`OutputLineage`: the source and the people whose words were in it) is written from the reads: every source the run
  had read when it emitted, not only the cited ones (`emit.write_lineage`), since all of them were in its context, so
  a removal of any reaches the output; `cited` marks the ones it names, which are all a screen shows. Words are scrubbed,
  object keys as well as values; the same output twice is one. Emits spend a budget of their own per run turn
  (`mcp.rate_limit`, `BRAIN_RUN_WRITE_LIMIT`), not the accountable admin's.
- The run's conversation holds the words it read. It has no creator and no participant and is not runner-discovered, so
  no session leg reads it and nor do its turns' content routes (`turn_access` asks the session's ACL): nobody reads it
  in canopy-web, the operator's admins included. When a source it read is removed the cascade deletes its content.

## A finding's lifecycle, and repeats (`apps/brain/findings.py`)

An output's `state` is the cascade's (active, withdrawn, stale) and nobody sets it. Its `status` is the people's:
`open` when made, then `accepted`, `dismissed` or `done` (`POST /api/w/<ws>/brain/outputs/{id}/status`, by the management
rule, each change an event in its workspace naming who made it). Only someone who sees it AND acts for its audience sets it
(`findings.may_set`; an admin agent only a `system` or `project` one, `findings.actor_may_set`): a `system` finding's workspace owners and admins (or an ancestor's) or a canopy admin, a `project`
output's members who may read the project, an `individual` or `memory` output's own person.

A run names what an output is with a `key`. An emit whose kind and key match an output of the same learner, subject and
workspace that is still active and `open` or `accepted` is a repeat and folds into it (`findings.match`, `fold`): `seen`
counts it, the newer words replace the old, and this run's reads join the lineage, so a removal of any session either
run read still reaches it. A `dismissed` or `done` one takes no repeat: a new output is made. A repeat spends no output
of the run's budget.

A run is told the open findings its learner already holds for its subject and workspace (`findings.open_for`, in the
run context's `open_findings`: id, key, title, sightings, most recently seen first, capped) and told to reuse a key for
the same problem; it may also name the finding outright (`brain_emit`'s `same_as`, `findings.match_id`). The page's copy
of a run's context leaves the list out (`state.context_shown`): who sees a run is not who sees its findings. Two outputs
that are one problem are merged (`POST /api/w/<ws>/brain/outputs/{id}/merge`, by the rule that sets a status, on both;
`findings.merge`): the kept one takes the other's sightings, lineage and a `merged_from` note, the other becomes
`duplicate` with `merged_into`, a repeat of its key folds into the kept one, and an `output.merged` event records it.
An open or accepted finding no run re-saw within its target's `fade_days` (21 unless set) is **faded**
(`findings.faded`, computed, nothing deleted): left out of runs' context, listed last on the page, fresh again when a
repeat folds in or someone sets it open or accepted (`set_status` moves its `last_seen_at`).

## Lineage over everything a run makes (`apps/brain/lineage.py`, `alerts_removal.py`)

`brain_emit` outputs stay the default record of what was learned, with their own lineage. Everything else the run's turn
makes is recorded too, by the server, never by the skill: `lineage.capturing`, a decorator on every API route (beside
the settings audit in `apps/api/api.py`), sees the rows a write by the run's turn creates (its credential names the turn;
an MCP tool is the same route) and records each as a `BrainRunArtifact` with `OutputLineage` (`output_kind`
`brain.artifact`) to every source the run had read by then, in the request's own transaction (`SKIP_APPS`/`SKIP_MODELS`
leave out plumbing). The pull requests, issues and links the turn reports in its replies or result are recorded when the
run ends (`lineage.record_reported`; canopy's own pages are rows already).

When a removal reaches a source, the sweep (`lineage.sweep`, from `cascade.sweep`, judged by `cascade.removed_reason`
on the run's learner) undoes what canopy can and **flags** what it cannot (`lineage.undo`): an app registers how to undo
its rows (`lineage.register_undo`: a task no one has started or finished is deleted, a draft project update and a mail
draft are deleted); a sent email, a published update, a PR, an issue, a link or anything else is flagged. A flag is
listed on its workspace's brain page ("Could not be undone", `api_manage.flags_out`) to its owner (the person the run
answered to) and the workspace's managers, and raised to its owner as a `learning_removal` alert until one of them
acknowledges it (`POST /api/w/<ws>/brain/flags/{id}/acknowledge`, by the management rule). An artifact holds ids and a
link, never a word.

## Removal and the cascade (`apps/brain/cascade.py`)

What a learner produced is written with its sources (`OutputLineage`), so a removal finds it. `cascade.sweep`
runs on the tick `brain.cascade` (every 15 minutes) and also right after the act that removed something
(`cascade.after_commit`, from `consent.exclude`, `consent.revoke`, a consent that narrows and a session's
content deletion, `signals._cascade_content_deletion`). It is idempotent.

- A source is **removed** for an output when its words are gone (deleted, `withdrawn`, its session gone;
  such a source becomes `withdrawn` for good) or it is sealed; for a shared (`project`, `system`) use when ANYONE
  ever excluded it, still in it or since taken out of it (as `eligible` and `emit_allowed`); or when the person the
  output is about, or for a shared output any person in the source when it was made (`participant_scope`), excluded
  it, stopped agreeing to the class, or narrowed
  their consent so it no longer allows the use: the source's workspace no longer covered (`eligibility.covers`)
  or a standing rule now denying it (`eligibility.rule_denies`, on the output's learner and its run's purpose; a
  rule naming a learner or purpose the sweep cannot tell is not taken to match) (`cascade.removed_reason`).
  Removing X withdraws what was learned about X; what was learned about Y from
  Y's own words stays.
- **Someone joining later.** A run's reads and a link's evidence record no participants, so they are judged against
  who is in the source now, less anyone who never agreed to the class (`cascade.ever_agreed`): every such use passed
  `eligible`, which needed everyone's consent, so that person joined after it. Joining without consent closes every
  later read (`eligible` says no) and destroys nothing already made; someone who once agreed and stopped counts,
  whenever they joined (fails safe).
- An output derived only from removed sources is **withdrawn**: body and title emptied, lineage kept as ids,
  seen by no one. One derived from several is **stale**: emptied and hidden the same way (its words cannot be told
  apart by source), with a `RederiveRequest`; the feature's next run for that subject reads the remaining sources again
  and a clean end closes the request. The learner gets a `RevocationNotice`, served by its next `brain_read`.
- The evidence of every link of a source removed from shared (project) use is deleted, whether or not an output cites it.
- The conversation of every run that read a removed source is deleted (`content_deletion.delete_content`: messages,
  turn events and transcripts), since `brain_read` results and `brain_emit` inputs cannot be cut out source by source;
  a run still working is deleted by a later sweep.
- A personal-memory entry derived from a removed session (HCP `derivedFrom`, `EntryRelationship` with
  `source_kind` "session") is deleted through HCP's own path (`apps/brain/hcp_store.py`, `hcp.delete_entry` as canopy):
  never served to an agent again, its embedding cleared, still shown to its person marked deleted. The cascade acts
  for a canopy user who once agreed to `memory` on any removal (their exclusion, their consent ended or narrowed, the
  words gone, the session sealed); for anyone else only on their own exclusion of the session or its words being gone.
  A contact's entries (no canopy account, e.g. a preference recorded through trusted contacts) are never deleted by
  the cascade, and a seal (a sensitive session, a contact's) deletes nothing for someone who never agreed.
- **A tombstone, not a toggle.** `consent.include` restores eligibility only; a withdrawn output stays
  withdrawn, a replay cannot cite a removed source (`cascade.emit_allowed`), and a runner re-ship of deleted
  words is refused as before (`transcript_ingest.refuses_ship`).
- The one other store that writes `OutputLineage`, personal memory, is swept by name (`hcp_store.sweep`); a new
  store is added to `cascade.sweep` beside it. The sweep skips withdrawn outputs.

## Disclosure (`apps/brain/disclosure.py`)

Each read the brain allows leaves a `brain.read` event (`readers.record_read`). `disclosure.compact` (tick
`brain.disclosure`, hourly, and on each read of the summary, then over only the events of that person's sessions) folds
them into `DisclosureDay`: per person whose
words were in the sources, per learner and day, how many reads and how many of their sessions. Never the
reader's own reads, never a word; recomputable from the raw events (`disclosure.raw_rows`).
`GET /api/me/learning/disclosure/` and `/api/me/learning/removed/` are the person's own and off MCP.
The Settings page `/settings/learning` shows them beside the class switches.

## Where a learner runs: targets and their schedules (`apps/brain/targets.py`)

A learner runs only where it is aimed. A **target** (`BrainTarget`) is a workspace (a run reads eligible sources of it and
the workspaces below it) or a workspace project (the sessions `core`-tied to it, in its workspace's subtree); a learner
with no target never runs. A `system` learner takes either; a `project` learner only projects (what it makes is for the
project's readers); the per-person learners and the built-in project readers take none (`targets.kind_refusal`). Each
target has its own runs, what they read (`reading.learned`, by the run's subject) and findings: a workspace target's
run is about `system` and that workspace, a project target's about the project, scoped to its workspace
(`targets.subject_of`, `runs.subject_of`). The learner's operator must live in the target's workspace or above it
(`targets.run_place_refusal`), so a run never puts a workspace's words in a sibling's or a sub-workspace's agent; a
change of operator that would break that, or that takes the operator away while targets stand, is refused
(`targets.operator_refusal`).

**Each target's schedule is a normal canopy schedule** (`harness.AgentSchedule`) on the operator agent, made with the
target (`targets.add`; daily at 06:00 UTC unless given): its cron, Run now and history are on the agent's schedules page
beside its other schedules, and canopy's own clock fires it (`harness.schedule_clock`, or a laptop runner reporting the
slot). It fires as the learner's accountable admin's standing request (`created_by` is `registry.accountable`, kept in
step by `targets.follow` when the learner's operator, entry or admin changes), so a fire whose admin may no longer
direct the operator is refused like any trigger (`triggers.refusal`). Whoever added the target is recorded on it and in
its `SettingChange`. Deleting the schedule deletes the target, and taking the target away deletes its schedule.

The fire does not enqueue a plain turn: the harness asks the brain's announcer (`harness.schedule_hooks`, registered in
`apps.py`; a project check-in's is the other), which consumes the slot whatever comes of it and starts a run
(`targets.start` → `runs.start`) only when one may start (`targets.why_not`: learning reads, enabled, operator, entry,
accountable, where the operator lives, an active project) and there is something new for the target (`targets.has_new`:
an eligible source active in its window that no clean run of it read, or that grew since, so a session linked or agreed
to late is still read, and a target with only old unread sessions does not fire). One run per
target and slot (`brain_one_run_per_slot`), however many firers report it, and one open per target
(`brain_one_open_run`). A slot that starts nothing leaves a `brain.target` event saying why. The run's turn is an
occurrence of the schedule (`origin_ref.schedule_id`), so the schedule's history lists it; its `grace_minutes` is the
run timeout and it is `always_run`. There is no other scheduler: no learner field, interval or tick decides when a
learner runs.

**A target's budget** (`apps/brain/budget.py`, `BrainTarget.max_*`): sessions one run reads and outputs it writes, and
runs, outputs and tokens a target spends in a UTC day, set by the same people on the brain page
(`PATCH /api/w/<ws>/brain/targets/{id}/budget`, a setting) and shown there with today's use. A run stops cleanly at a
cap: `brain_read` serves nothing more and answers `done` with `budget.stopped`, `brain_emit` refuses a new output with
the reason, and a fire or Run now past a daily cap starts nothing (`budget.start_refusal`, in `targets.why_not`).
Tokens are what the boxes reported for the runs' conversations (`events.SessionUsage`), so they are checked before a
run starts, never during one; a run is one turn by construction (its first turn's end finishes it).

**Run now** on the brain page (`POST /api/w/<ws>/brain/targets/{id}/runs`) is the target's schedule's own Run now
(`targets.run_now`): one run now, answering to the schedule's creator, with who pressed it recorded. It is the target
workspace's owners' and admins' (an ancestor's are too, `perms.BRAIN_MANAGE`) or a canopy admin's, by the management
rule (a person, or an admin agent in its own turn), and refused with the reason when the run cannot start or there is nothing new. Adding and taking away a target
(`POST /api/w/<ws>/brain/targets`, `DELETE …/targets/{id}`) are settings of the same people, for `<ws>` or a workspace
below it, never above. A clean end of the run's turn (`signals`, `runs.finish`) makes what it read count as learned; a
failed one counts nothing and the next fire reads it again. A run open past `BRAIN_RUN_TIMEOUT_SECONDS` (six hours by
default) is abandoned as failed at the next fire, and its turn is stopped (`harness.cancel_turn`), so nothing runs on
for a run that is over. A system run's outputs are findings: they act on nothing by themselves.

The **judge** (layer 3 of the associations; the registered `project-link-judge`) is such a run with the purpose `judge`:
it is served only the sessions weakly tied to the project (a `candidate` link made by signals, never one a person or a
dispatch settled), each as the title and the core read's `opening` view (the first few lines people typed, short), beside the project's outcome and open tasks.
Its one output kind, `link`, is a ruling on exactly one source it read: a tier, a confidence from 0 to 1 and the model
that judged, written as a `judged` `SourceLink`, which never overrides a person's call or a dispatch. Only sessions whose
people agreed to project learning can be judged, since reading them is a project read. canopy makes no model calls: the
judge is the operator agent's own turn. `manage.py brain_validate_links --how judged` measures its rulings against a
hand-checked list; the plumbing is tested with a rule standing in for the model.

`GET /api/brain/workspaces/{slug}/projects/{handle}/sessions/` lists the sessions tied to a project that the caller may read
(by the same rule as every session read): ids, titles and how each tie was made, never a word of a transcript. It is what a
project history reads instead of re-deriving the selection.

## The brain page (`/w/<ws>/brain`; `frontend/src/pages/BrainPage.tsx`, `apps/brain/state.py`, `api_state.py`)

A workspace's view of the brain, among the health dashboard's views and linked from Settings → Learning (the flat
`/brain` redirects to the active workspace's). Every read is pinned (`/api/w/<ws>/brain/…`) and covers `<ws>` and the
workspaces below it the viewer is in. That scope is ONE function the page, the outputs list and the graph share
(`scope.of_request`, `scope.below`: the read rule every list follows, so an unpinned outputs list never mixes two
organisations); what each shows inside it is still its own rule. Its routes
(`GET /api/brain/state`, `/state/links`, `/state/runs`) are the person's own as themselves, or an admin agent's pinned to
a workspace it manages (`actor.of`), and they write nothing (`tests/test_brain_page.py` captures every query). The page holds no copy of a
count. `tests/test_brain_workspace_scope.py` pins each role's slice.

- **Its owners and admins** (`logs.read`, the health dashboard's gate; an ancestor's owner or admin holds it below)
  see the aggregate of the workspaces they hold it in: consent as counts per workspace and class (never a name, and a
  count below `state.SMALL`, three, hidden as "<3" so a small workspace's few are not named by elimination),
  sources open, withdrawn and sealed by why, link counts by project and tier on its readable projects, removal work
  (`state._scoped`), and the runs started for those workspaces (`state._runs`). They also manage learning there
  (`can_manage`, `perms.BRAIN_MANAGE`, by the management rule): its `system` findings, its targets
  (add, Run now, take away), and the two settings below.
- **A member** sees their own: their classes, their own sources and removals, the links of sessions they read, runs
  about them, and the outputs `outputs.visible_to` already gives them.
- **Why a session is not read** (`GET /api/w/<ws>/brain/why/{session_id}?learner=`, `api_manage.why_not_read`): for a
  session in `<ws>` or below that the viewer is in or reads through legs 1 to 6 (an admin agent: one its own login
  reads), whether a learner may read it now and
  why not, by `eligibility.explain` itself. A reason about a person says "you" or "someone in it", never who.
- **Everyone** sees the learners and who answers for each (`registry.accountable`); which agent operates one, its
  entry and who answers for it only inside the operator's organisation (`registry.shown_to`), the same on
  `/api/brain/capabilities/`, so tenants never meet there. A session is named (id, scrubbed
  title) only if the viewer reads it through legs 1 to 6, never the project leg; a run's read of one they do not, or
  an output's citation of it, is a count (`state.run_page`, `api_admin._cited`). A sub-workspace's owner never sees a
  sibling's or a parent's aggregate, runs or findings; a canopy admin also sees the unscoped runs. The runs list reads
  again while one is open.
- **Two tabs.** Findings, the default (`frontend/src/pages/BrainFindings.tsx`), is written for whoever acts on them:
  filtered by status (open and accepted at first), severity and the repo the fix belongs in, most severe first then the
  most recently seen; a body's known fields (`brain-learn`'s system finding) drawn in plain words, any other as
  "label: value". Each evidence quote names a source id; the outputs list maps it to a session only through
  `source_sessions`, which lists the cited sources whose session the viewer already reads (`api_admin._cited`), so a
  quote from any other says "a session you do not read". A faded finding sits in its own collapsed group with Keep; a
  merged duplicate is never listed. Everything else is "Setup and audit" (`?tab=setup`).

## Managing a workspace's learning (`apps/brain/api_manage.py`, `learning_reads.py`)

- **"Learning reads"** (`WorkspaceLearningReads`, `PUT /api/w/<ws>/brain/learning-reads`): on, off, or unset to inherit
  from the nearest ancestor that sets it; a root with none is off. It is a setting like any other: `@gate(..., setting=True)`
  writes one `SettingChange` per change (who, the workspace, its own setting and what was in effect before and after,
  `audit.note_change`), and the page lists the latest (`api_manage.reads_out`) to those who may change it. A change that
  changes nothing records nothing.
- **A learner** (`PATCH /api/w/<ws>/brain/learners/{slug}/`): whether it is enabled, its operator, `entry`, and
  `scope` (only narrowing); where and when it runs is its targets. A learner is canopy-wide, so a workspace configures only one whose operator, if it has
  one, is an agent of `<ws>` or below, and gives it only such an agent, and only a project or system learner
  (`api_manage.learner_refusal`; a per-person learner is canopy-wide); enabling one
  nobody active answers for makes the person its accountable admin (`registry.update`). Its class, purposes and audience
  (what people agreed to) stay a canopy admin's (`/api/brain/capabilities/`). The built-in project readers are never
  configured here.

## Every reader is placed

The learners canopy has today and will have are registered, dark, by migration `0007` (coaching, personal memory, the project
link judge, project learning, system learning): none has an operator or is enabled, so registering them changes nothing, and
coaching keeps reading through its own leg until it moves. `tests/test_message_readers_are_registered.py` lists every server
module that reads a session's transcript rows and why it may (the chat itself, ingest, plumbing, a person sharing their own
session, a notification); a new reader fails it until it is placed (calling the core read
`transcript_read.read` counts), and a learning read belongs in `brain.reading`, which is the one module marked as such. The agent-side readers (`ada:session-review`, `canopy-run-review`, `canopy:agent-review`, the
stall classifier, `canopy analyze/improve`, the contributor pages and check-in runs) read through the API, not through a
server module; moving them to runs of a registered learner is work in the canopy plugin and the agents' repos.

## Not built yet

A per-person scheduler for `individual` and `memory` features, and the capability leg's other readers (coach and manager
reading of transcripts stays on the existing coaching leg). No workspace reads a transcript through the brain until its owners or admins turn "Learning reads" on.
