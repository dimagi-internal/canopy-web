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
  for. Registered by a canopy admin (the routes are a later phase); only the model exists.

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
turn's token, a runner and a request naming a turn are a 403, and none of the six is an MCP tool
(`apps/mcp/api_tools.py` `EXCLUDED`). Nobody opts anyone in; there is no admin route.

Every change is on the person's own audit (`learning.consented|class_added|class_removed|revoked|
source_withdrawn|source_restored` in `PersonAuditEvent`) and, ids only, in the event log of each
workspace they are in (source `brain.consent`).

## Coaching is `individual`

`CoachingOptIn` still drives the coaching session ACL (leg 5 of `access.py`) and the runner's full
upload (`coaching.full_upload_session_ids`); its readers are unchanged. The two are kept the same in
both directions: coaching's own opt-in or out mirrors into the `individual` class
(`consent.mirror_coaching`, called by `session_reviewers`), and setting or dropping `individual` through
the learning routes writes the opt-in (`consent._sync_coaching`). Migration `0002_copy_coaching_opt_ins`
carried every live opt-in over; the old table stays until a later release drops it.

## Which project a source belongs to (`apps/brain/associate.py`)

`SourceLink` ties a source to a subject (a project today), one row per pair, in four layers where the
strongest wins (`associate.rank`): **dispatched** (a turn raised from a task of the project ran in the
session: certain), **signals** (the project's `ProjectSignature` matched against the session's recorded
`activity`; `associate.classify` is canopy's `project_history.classify`, ported), **judged** (a later
phase) and **declared** (an agent at close-out, or a person). A link is metadata and confers no read
by itself; only a `core` link will open the project leg.

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
class, the purposes it declares, the source kinds and output kinds it works with. It starts dark (`enabled` false). Its
audience is its class's (`registry.CLASS_AUDIENCE`) and may only narrow: a wider one is refused, because that is a new
class each person must agree to. The built-in `project-readers` (migration `0005`) is the purpose a project's readers
read a session under. The API's prose calls these learners.

## Who sees what it produced (`apps/brain/outputs.py`)

Outputs (`BrainOutput`) are read by their class's audience (`outputs.visible_to`, `GET /api/brain/outputs/`):
`individual` by the person, their coaches (the same rule as reading their sessions) and their manager; `memory` by
the person; `project` by the project's readers; `system` by canopy admins and the workspace's owners. A withdrawn
output is seen by no one. `ReportingLine` (`apps/brain/reporting.py`, set by a workspace owner by hand, each change an
event) is the manager relationship; it opens a manager's reading of their reports' `individual` outputs and nothing else.

## Reading as a project's reader (`apps/brain/readers.py`)

A session `core`-tied to a project is readable, as a seventh leg of the session ACL (`access._brain_project_q`), by
whoever may read that project, when `eligible_sources` passes it for `project-readers`: read-only (a viewer: never a
write, share or pull), and the rows come out scrubbed (`common.redact`, via `transcript_read.readable_window`, the
one read pipeline, and the session list's words). Each read leaves one `brain.read` event (ids only; reads of a
session by a reader coalesce). The leg and the record are inert until `BRAIN_READS_ENABLED`.
`eligible_sources` is `eligible` for a whole listing: the structure is SQL and what is not (a consent's classes and
workspaces, standing rules, who is a member where) is judged in Python over the people who consented, once, so nothing is
cached; `tests/test_brain_project_leg.py` holds the two equal over a world.

## Not built yet

Confined learning runs with their read and emit tools, the removal cascade over outputs, the model judge (layer 3) and
the scheduler that starts runs per active project. No capability reads a transcript through the brain in production:
`BRAIN_READS_ENABLED` is the rollout gate and stays off until the read paths and the cascade are merged.
