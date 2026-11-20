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
  (`participants.people_of`): the creator, `SessionParticipant` rows, the owner of the box a
  runner-discovered session ran on. Agents are never participants. A contact is, and makes the source
  ineligible for every shared use.
- **`LearningConsent`**: one live row per person (`learning_consent_one_live`): `classes`, `workspaces`
  (empty = all of theirs), `include_new`, `standing_rules`, `directives`. **`SourceExclusion`**: one
  person taking one source out, its own row.
- **`BrainCapability`**: something that learns, in one purpose class, declaring the purposes it reads
  for. Registered by a canopy admin (`registry.py`, `/api/brain/capabilities/`).

Sources are made lazily (`sources.for_session`) and warmed by `manage.py brain_backfill`.

## Purpose classes

`individual` and `memory` learn ABOUT a person from their own words: only that person's consent
counts. `project` and `system` are SHARED: every human participant must permit the class and none may
have excluded the source (most restrictive wins; transcripts are not split by speaker). One
participant's exclusion takes the source out of every shared use; what was learned about the others
from their own words stays (`consent.exclude`).

## `eligible` (`apps/brain/eligibility.py::eligible`)

The one question, asked on every request and cached nowhere: may this capability learn from this
source, for this purpose, about this subject? It fails closed. True only if the source is open and not
sealed (sensitive, a contact's conversation, words deleted: read live from the session, the sensitive
test being `access._not_sensitive_q`'s, held equal by `tests/test_brain_sources.py`), the capability is
registered, enabled and declares the purpose, the right people consented to the class, none excluded the
source, no standing rule denies it, the source's workspace is within each person's consent and each is a
member of it (a guest is not). `tests/test_brain_eligibility.py` is the truth table.

A standing rule is deny-only: `{capability?, purpose?, class?, workspace?}`, denying when every key it
names matches. A rule the code cannot read denies (`rule_denies`).

## Consent is the person's alone (`apps/brain/api.py`, `apps/brain/consent.py`)

`/api/me/learning/` reads, sets and ends a person's consent; `/api/me/learning/sessions/{id}/` takes
one of their sessions out and back. Each route asks `harness.acting_person.refusal`: an agent's login, a
turn's token, a runner and a request naming a turn are a 403, and none of them is an MCP tool
(`apps/mcp/api_tools.py` `EXCLUDED`). Nobody opts anyone in; there is no admin route.

Every change is on the person's own audit (`learning.consented|class_added|class_removed|revoked|
source_withdrawn|source_restored` in `PersonAuditEvent`) and, ids only, in the event log of each
workspace they are in (source `brain.consent`).

## Coaching is `individual`

`CoachingOptIn` still drives the coaching session ACL (leg 5 of `access.py`) and the runner's full
upload (`coaching.full_upload_session_ids`). Both honour removal: a session anyone in it took out of learning
(`access.removed_from_learning`) is read by no coach and shipped for none, and a person whose consent names
workspaces is coached only in those (`eligibility.covers`). The two are kept the same in
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
  projects of the source's own workspace.
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
`entry` a run starts with. It starts dark (`enabled` false). Its audience is its class's (`registry.CLASS_AUDIENCE`)
and may only narrow: a wider one is refused, because that is a new class each person must agree to. The built-in
`project-readers` (migration `0005`) is the purpose a project's readers read a session under.

## Who sees what it produced (`apps/brain/outputs.py`)

Outputs (`BrainOutput`) are read by their class's audience (`outputs.visible_to`, `GET /api/brain/outputs/`):
`individual` by the person, their coaches (the same rule as reading their sessions) and their manager; `memory` by
the person; `project` by the project's readers; `system` by canopy admins and the workspace's owners. A withdrawn
output is seen by no one. `ReportingLine` (`apps/brain/reporting.py`, set by a workspace owner by hand, each change an
event) is the manager relationship; it opens a manager's reading of their reports' `individual` outputs and nothing else.

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
which rows are leg-7-only once (`access.brain_only_ids`). The leg and the record are inert until `BRAIN_READS_ENABLED`.
`eligible_sources` is `eligible` for a whole listing: the structure is SQL and what is not (a consent's classes and
workspaces, standing rules, who is a member where) is judged in Python over the people who consented, once, so nothing is
cached; `tests/test_brain_project_leg.py` holds the two equal over a world.

## A run (`apps/brain/runs.py`, `reading.py`, `emit.py`, `api_run.py`)

The server starts a run of a feature (`runs.start`): a conversation of the operator agent stamped `metadata.brain_run`,
with no creator and no participant, and a first turn queued as canopy's own, accountable to whoever registered the feature.
That stamp puts every turn in it in the confined `brain` profile (`published_profiles.BRAIN`,
`turn_context._brain_rule`): no login, GitHub, mail, vault, Bash or Write, and a caller token that lists only
`who_is_asking`, `brain_read` and `brain_emit`; REST refuses the token outright. Only that token reaches the two routes,
and only while the turn runs and the gate is on.

- **`brain_read`** serves the run's next eligible sources once each, in order past its cursor: stored user and
  assistant text only, harness noise dropped, secrets scrubbed, and for a feature that learns about a person only that
  person's own lines (other people's words in the same session are left out). The directives of the people in a source
  travel with it. Every source served is recorded (`BrainRunRead`, a `brain.read` event).
- **`brain_emit`** writes one output of a declared kind, about the run's own subject, citing sources. Every cited
  source must be one this run read and must still be eligible now, or nothing is written. The lineage
  (`OutputLineage`: the source and the people whose words were in it) is written from the reads. Words are scrubbed;
  the same output twice is one.
- The run's conversation holds the words it read, in the operator's own turn record, readable by the operator's admins as
  any of its work is: the same exposure coaching has today.

## Removal and the cascade (`apps/brain/cascade.py`)

What a learner produced is written with its sources (`OutputLineage`), so a removal finds it. `cascade.sweep`
runs on the tick `brain.cascade` (every 15 minutes) and also right after the act that removed something
(`cascade.after_commit`, from `consent.exclude`, `consent.revoke`, a dropped class and a session's content
deletion, `signals._cascade_content_deletion`). It is idempotent.

- A source is **removed** for an output when its words are gone (deleted, `withdrawn`, its session gone;
  such a source becomes `withdrawn` for good), or when the person the output is about, or for a shared
  output ANY person in the source (`participant_scope`), excluded it or stopped agreeing to the class
  (`cascade.removed_reason`). Removing X withdraws what was learned about X; what was learned about Y from
  Y's own words stays.
- An output derived only from removed sources is **withdrawn**: body and title emptied, lineage kept as ids,
  seen by no one. One derived from several is **stale** and gets a `RederiveRequest` for the scheduler to take.
  The links' evidence is deleted and the learner gets a `RevocationNotice`.
- **A tombstone, not a toggle.** `consent.include` restores eligibility only; a withdrawn output stays
  withdrawn, a replay cannot cite a removed source (`cascade.emit_allowed`), and a runner re-ship of deleted
  words is refused as before (`transcript_ingest.refuses_ship`).
- Another store (an HCP entry) registers with `cascade.register_store` and is asked to withdraw or mark stale;
  nothing in this app touches the memory store itself.

## Disclosure (`apps/brain/disclosure.py`)

Each read the brain allows leaves a `brain.read` event (`readers.record_read`). `disclosure.compact` (tick
`brain.disclosure`, hourly, and on each read of the summary) folds them into `DisclosureDay`: per person whose
words were in the sources, per learner and day, how many reads and how many of their sessions. Never the
reader's own reads, never a word; recomputable from the raw events (`disclosure.raw_rows`).
`GET /api/me/learning/disclosure/` and `/api/me/learning/removed/` are the person's own and off MCP.
The Settings page `/settings/learning` shows them beside the class switches.

## Scheduled project learning and the judge (`apps/brain/schedule.py`)

A project feature (`purpose_class` project) with an operator, an enabled flag and a `schedule` (`hourly`, `daily`,
`weekly`) is run by the `brain.schedule` tick for each ACTIVE workspace project of the operator's workspace tree (never a
personal one), only when a run is due and there is something new: eligible sources past the feature's `BrainCursor` for
that project. The run reads past that cursor (`BrainRun.floor`); a clean end of its turn (`signals`, `runs.finish`) moves
the cursor past what it read, a failed one moves nothing and the next interval tries again. One run per feature and project
at a time.

The **judge** (layer 3 of the associations; the registered `project-link-judge`) is such a run with the purpose `judge`:
it is served only the sessions weakly tied to the project (a `candidate` link made by signals, never one a person or a
dispatch settled), each as the title and the person's first few lines, short, beside the project's outcome and open tasks.
Its one output kind, `link`, is a ruling on exactly one source it read: a tier, a confidence from 0 to 1 and the model
that judged, written as a `judged` `SourceLink`, which never overrides a person's call or a dispatch. Only sessions whose
people agreed to project learning can be judged, since reading them is a project read. canopy makes no model calls: the
judge is the operator agent's own turn. `manage.py brain_validate_links --how judged` measures its rulings against a
hand-checked list; the plumbing is tested with a rule standing in for the model.

`GET /api/brain/workspaces/{slug}/projects/{handle}/sessions/` lists the sessions tied to a project that the caller may read
(by the same rule as every session read): ids, titles and how each tie was made, never a word of a transcript. It is what a
project history reads instead of re-deriving the selection.

## Every reader is placed

The learners canopy has today and will have are registered, dark, by migration `0007` (coaching, personal memory, the project
link judge, project learning, system learning): none has an operator or is enabled, so registering them changes nothing, and
coaching keeps reading through its own leg until it moves. `tests/test_message_readers_are_registered.py` lists every server
module that reads a session's transcript rows and why it may (the chat itself, ingest, plumbing, a person sharing their own
session, a notification); a new reader fails it until it is placed, and a learning read belongs in `brain.reading`, which is
the one module marked as such. The agent-side readers (`ada:session-review`, `canopy-run-review`, `canopy:agent-review`, the
stall classifier, `canopy analyze/improve`, the contributor pages and check-in runs) read through the API, not through a
server module; moving them to runs of a registered learner is work in the canopy plugin and the agents' repos.

## Not built yet

A per-person scheduler for `individual` and `memory` features, the capability leg's other readers (coach and manager
reading of transcripts stays on the existing coaching leg), and the HCP entry store's cascade hook (it follows the HCP v2
`derivedFrom` work). No capability reads a transcript through the brain in production: `BRAIN_READS_ENABLED` is the rollout
gate and stays off until the owner turns it on.
