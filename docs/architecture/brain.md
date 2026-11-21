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
graph's would-be links (`everyone_agreed`). Coaching does not go through `explain` (`coaching_allows`), so the setting
does not stop coaching. Nothing is cached; a change is the next request's answer.

`eligible` is `eligibility.explain`, which says why. Without a capability, `explain` asks the same question of a
purpose class (what `everyone_agreed` asks).

A standing rule is deny-only: `{capability?, purpose?, class?, workspace?}`, denying when every key it
names matches. A rule the code cannot read denies (`rule_denies`).

## Consent is the person's alone (`apps/brain/api.py`, `apps/brain/consent.py`)

`/api/me/learning/` reads, sets and ends a person's consent; `/api/me/learning/sessions/{id}/` takes
one of their sessions out and back. Each route asks `harness.acting_person.refusal`: an agent's login, a
turn's token, a runner and a request naming a turn are a 403, and none of them is an MCP tool
(`apps/mcp/api_tools.py` `EXCLUDED`). Nobody opts anyone in; there is no admin route.

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
workspace they are in (source `brain.consent`).

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
  every agent; only that same person undoes it (`undeclare`), and a different participant's attempt is a
  409. An agent's declaration outranks the automatic layers but never a person's.
- **Never across a workspace, never a personal project.** Automatic layers link only workspace-visible
  projects of the source's own workspace, and neither a person nor an agent can declare a personal one.
- **Only sessions everyone in agreed to `project`.** The dispatched and signals layers and the
  signature seed skip a source unless every human in it agreed to `project` for its workspace, none
  excluded it and no contact is in it (`eligibility.everyone_agreed`). A declaration still stands.
- **Computed, then written.** `associate.propose` (`propose_dispatched`, `propose_signals`) computes what layers 1 and
  2 would make, reading only; `link_dispatched` and `link_signals` write what it returns, through `put`, whose
  `outranks` decides whether a stored link keeps its place. The relations graph's would-be links call the same compute.
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

Outputs (`BrainOutput`) are read by their class's audience (`outputs.visible_to`, `GET /api/brain/outputs/`):
`individual` by the person, their coaches (the same rule as reading their sessions, and only outputs made in the
workspaces they coach, `access.coach_scope`) and their manager; `memory` by
the person; `project` by the project's readers; `system` by canopy admins and the owners and admins of the output's workspace
(`perms.BRAIN_MANAGE`; inherited roles count, so an ancestor's owner or admin sees a descendant's findings and a
sub-workspace's or a sibling's never sees them; this leg opens only to the person as themselves, as the manager leg,
never an agent's login or an MCP call; a run started for a workspace writes its findings as that workspace's, `emit`). Pinned to a
workspace, the route answers only that workspace's outputs and the ones below it. Only `active`
outputs are seen: a withdrawn or stale one is seen by no one. Outputs are derived, so they stay readable after
a workspace turns "Learning reads" off; nothing new is made from its sessions then. `ReportingLine` (`apps/brain/reporting.py`, set by a workspace
owner by hand, each change an event) is the manager relationship; it opens a manager's reading of their reports'
`individual` outputs made in the workspace the line is in, and nothing else, and only to the manager as a person
(`api_admin.manager_leg_open`: their browser session or a PAT they minted by hand, as HCP's governance acts; never an
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
`metadata.brain_run`, with no creator and no participant, and a first turn queued as canopy's own, accountable to the
target schedule's creator (the learner's accountable admin). A run carries a workspace `scope` (`BrainRun.scope`): it reads only
eligible sources of that workspace and the ones below it (`reading.candidates`, `reading.subtree`, the existing
`descendant_slugs` walk), and what it emits is that workspace's (`emit`). Eligibility, consent and the cascade are
unchanged: the scope only narrows what a run reads and who sees what it found. A `system` run's subject is its scope,
so one run is open per feature and workspace.
That stamp puts every turn in it in the confined `brain` profile (`published_profiles.BRAIN`,
`turn_context._brain_rule`): no login, GitHub, mail, vault, Bash or Write, and a caller token that lists only
`who_is_asking`, `brain_read` and `brain_emit`; REST refuses the token outright. Only that token reaches the two routes,
and only while the turn runs and "Learning reads" is on somewhere it reads (`learning_reads.run_refusal`).

- **`brain_read`** serves the run's next eligible sources once each, in order, leaving out what a clean run of the
  same feature already read for the same subject, each through the core read (`transcript_read.read`) in the feature's
  scope (`reading.view_of`; a judge never wider than `opening`), secrets scrubbed, never cut by speaker. One call serves
  at most `reading.MAX_CHARS` and never splits a source that fits in a call; a source larger than a call pages across
  calls (`next_index`, passed back as `index`), never splitting a row. The directives of the people in a source travel with it, and the sources removed since the feature learned
  from them are named once (`revoked`, from `RevocationNotice`). Every source served is recorded (`BrainRunRead`, a
  `brain.read` event).
- **`brain_emit`** writes one output of a declared kind, about the run's own subject, citing sources. Every cited
  source must be one this run read and must still be eligible now, or nothing is written. The lineage
  (`OutputLineage`: the source and the people whose words were in it) is written from the reads. Words are scrubbed,
  object keys as well as values; the same output twice is one. Emits spend a budget of their own per run turn
  (`mcp.rate_limit`, `BRAIN_RUN_WRITE_LIMIT`), not the accountable admin's.
- The run's conversation holds the words it read, in the operator's own turn record, readable by the operator's admins as
  any of its work is, until a source it read is removed: then the cascade deletes the conversation's content.

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
- Another store that writes `OutputLineage` registers with `cascade.register_store` and is asked to withdraw or mark
  stale. The sweep skips withdrawn outputs.

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
an eligible source no clean run of it read, so a session linked, agreed to or backfilled late is still read). One run per
target and slot (`brain_one_run_per_slot`), however many firers report it, and one open per target
(`brain_one_open_run`). A slot that starts nothing leaves a `brain.target` event saying why. The run's turn is an
occurrence of the schedule (`origin_ref.schedule_id`), so the schedule's history lists it; its `grace_minutes` is the
run timeout and it is `always_run`. There is no other scheduler: no learner field, interval or tick decides when a
learner runs.

**Run now** on the brain page (`POST /api/w/<ws>/brain/targets/{id}/runs`) is the target's schedule's own Run now
(`targets.run_now`): one run now, answering to the schedule's creator, with who pressed it recorded. It is the target
workspace's owners' and admins' (an ancestor's are too, `perms.BRAIN_MANAGE`) or a canopy admin's, as a person only (off
MCP), and refused with the reason when the run cannot start or there is nothing new. Adding and taking away a target
(`POST /api/w/<ws>/brain/targets`, `DELETE …/targets/{id}`) are settings of the same people, for `<ws>` or a workspace
below it, never above. A clean end of the run's turn (`signals`, `runs.finish`) makes what it read count as learned; a
failed one counts nothing and the next fire reads it again. A run open past `BRAIN_RUN_TIMEOUT_SECONDS` (six hours by
default) is abandoned as failed at the next fire. A system run's outputs are findings: they act on nothing by themselves.

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
workspaces below it the viewer is in (`wsvc.request_workspace_slugs`, the read rule every list follows). Its routes
(`GET /api/brain/state`, `/state/links`, `/state/runs`) are the person's own as themselves (`api_state.the_person`), none
is an MCP tool, and they write nothing (`tests/test_brain_page.py` captures every query). The page holds no copy of a
count. `tests/test_brain_workspace_scope.py` pins each role's slice.

- **Its owners and admins** (`logs.read`, the health dashboard's gate; an ancestor's owner or admin holds it below)
  see the aggregate of the workspaces they hold it in: consent as counts per workspace and class (never a name, and a
  count below `state.SMALL`, three, hidden as "<3" so a small workspace's few are not named by elimination),
  sources open, withdrawn and sealed by why, link counts by project and tier on its readable projects, removal work
  (`state._scoped`), and the runs started for those workspaces (`state._runs`). They also manage learning there
  (`can_manage`, `perms.BRAIN_MANAGE`, as a person only, none of it an MCP tool): its `system` findings, its targets
  (add, Run now, take away), and the two settings below.
- **A member** sees their own: their classes, their own sources and removals, the links of sessions they read, runs
  about them, and the outputs `outputs.visible_to` already gives them.
- **Everyone** sees the learners and who answers for each (`registry.accountable`). A session is named (id, scrubbed
  title) only if the viewer reads it through legs 1 to 6, never the project leg; a run's read of one they do not, or
  an output's citation of it, is a count (`state.run_page`, `api_admin._cited`). A sub-workspace's owner never sees a
  sibling's or a parent's aggregate, runs or findings; a canopy admin also sees the unscoped runs. The runs list reads
  again while one is open.

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
