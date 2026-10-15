# Storyboards as a feedback surface: decision rows and feedback rounds

Date: 2026-10-15 · Status: approved by the owner 2026-10-15 (open questions answered; see "Owner decisions") · Owner: Jonathan Jackson

Plans: `docs/superpowers/plans/2026-10-15-storyboard-decision-rows.md` (B) and
`docs/superpowers/plans/2026-10-15-storyboard-feedback-rounds.md` (C, plus the
plugin-side changes).

## Problem

#1501 gave a storyboard a brief (audience, point to land, data disclosure, short
version, assumptions, divergences, open questions) and per-cut notes. The first
real use — the Connect Supply RUTF set for Edesia/OES — showed it is still a
page you read, not a page that improves the next version. The owner's review:

> "This isn't useful for people to actually provide you feedback to improve the
> product itself or the demo. We need to provide clear links you used that go
> back into the product … we also want multiple people to be able to make
> comments before we trigger the next run. (this shares elements of how we are
> trying to think about multiple people contributing to projects that we should
> try to align). Also, for assumptions and open question, this is similar to how
> ace-web tries to convey things and we should probably learn from that design
> pattern here as well with decision logs / assumptions state and choices."

Product links (part A) are a bug fix shipped separately. This spec covers:

- **B. Decision rows.** Assumptions, divergences and open questions become
  decision rows with a recommended option, a state, and who decided.
- **C. Feedback rounds.** Several people comment over a round; one close
  action hands the whole batch to the agent, which starts the next iteration and
  answers every comment.

## What exists today

| Piece | Where | Relevant behaviour |
|---|---|---|
| Storyboard brief | `apps/storyboards/models.py` (`brief`), `schemas.py` (`StoryboardBrief`) | Free strings for assumptions / open questions; `divergences` = `{script, data, resolution}`. No state, no options, no decider. |
| Storyboard notes | `apps/storyboards/api.py` `leave_feedback` / `list_notes` | Writes `Feedback` rows (anchor `cut:<id>`). `list_notes` is members-only and deliberately hides other reviewers' notes from token holders "to keep the read independent". |
| Feedback pool | `apps/feedback/models.py` | Generic over target; states new/triaged/answered/declined, `disposition_note`, `resolved_in_version`. Docstring: "a turn reads the pool when the owner is ready" — no signal, no round. |
| DDD review | `apps/reviews/api.py` `submit_review` / `suggest_review` | Single decider: the first editor submit resolves it; suggestions sit in `suggestions_json`, outside the pool. The DDD loop polls `response_json`. |
| Project tasks | `apps/agents/models.py` `AgentTaskAction`; `apps/agents/services.py` `act()` | approve / reply / nudge start a turn immediately (`enqueue_task_turn`); every editor reply is its own turn (owner decision 2026-10-08). |
| Participants | `apps/agents/models.py` `ProjectParticipant` | Who is on a project; no review semantics. |
| Run doc | `apps/agent_runs/` | Per-iteration DDD state; `AgentRunGate` has one `decided_by`. |
| ace-web decisions | ACE `lib/decisions-schema.ts` (v6), ACE `docs/decisions-contract.md`; ace-web `frontend/src/components/opps/summary/DecisionsReview.tsx`, `DecisionItem.tsx`, `DecisionReactions.tsx`, `apps/opps/decision_overrides.py`, `apps/opps/reactions.py` | One row type for defaults, asks and rulings; asks pinned, the rest collapsed; confirm / pick / write-in / comment; history and undo; the next run binds human rulings. |

Nothing collects a round and hands it over; nothing on a storyboard can record a
choice.

## B. Decision rows

### One row type, mirroring ace-web

Every default the set is built on is a row. Whether it reads as an assumption,
an open ask or a ruling follows from its `status` and `review_ask`, exactly as in
ace-web (`DecisionsReview.tsx`: "the same data model … the SAME row
component").

| Field | Meaning |
|---|---|
| `id` | Stable kebab-case key, unique per storyboard. Reused across versions — it is how a ruling binds the next run. |
| `scope` | `set` (the whole storyboard) or a `narrative_slug` on it. |
| `question` | Internal phrasing. |
| `plain_question` | What a reviewer reads. Plain language; no ids or jargon (ace-web's plain-language gate). |
| `options[]` | Short labels, 1–8 words. A write-in is appended here. |
| `recommended` | The agent's default; must be one of `options` (= ace-web `ai-default`). |
| `override` | A person's choice, one of `options`. |
| `value` | Derived, never stored: `override` if set, else `recommended`. |
| `reasoning` | Why the agent recommends it. |
| `override_reasoning` | Why the person changed it. |
| `source` | Citation only (a scene, a file, a doc). |
| `evidence_basis` | `stated` / `inferred` / `conflicting`. |
| `conflict_signals[]` | For `conflicting`: the competing readings. This is where today's `divergences` (script vs data) go. |
| `status` | `ai-default` / `human-decided` / `overridden` / `deferred` (+ `revisit_when`). |
| `review_ask` | Absent, `recommended-confirmation`, or `required-before`. |
| `needed_by` | For `required-before`: `external-share` or `next-run`. |
| `confirm_reason` | Why a person should look. |
| `decided_by`, `decided_by_verified`, `decided_at` | Set by the server from the signed-in user, never by the caller. |
| `confirmed` | A person kept the recommended value — recorded distinctly from "never looked". |
| `history[]` | Newest-first prior values with who and when; every change is undoable. |
| `supersedes` / `superseded_by` | A correction replaces a row; the old one is kept collapsed. |

Comments on a row are **not** fields: they are `Feedback` rows anchored
`decision:<id>` (section C). An edit asserts a value; a comment is discussion —
the same split as ace-web's `DecisionReactions.tsx`.

Fields deliberately left out of the subset: `phase`, `skill`, `params`,
`inherited_from_run`, `answer_channel`, `owner`, `audience`, `value_set_by`.
They describe ACE runs, not a set of demos. Add one only when a storyboard needs
it.

### Storage: a model, not the brief JSON

Recommendation: a `StoryboardDecision` table (one row per decision, FK to the
storyboard), with `history` as a JSON column on the row.

Why not keep it in `Storyboard.brief`: rows are edited one at a time by
different people during a round. A JSON blob makes every confirm a
read-modify-write of the whole brief — a lost update between two reviewers is
the expected case, not an edge. A row also gives each decision an addressable
URL (`#decision:<id>`) and a clean place to hang `decided_by`.

The brief keeps `audience`, `purpose`, `data_disclosure` and `short_version`,
which are framing, not decisions.

### Migration from the brief

A data migration converts what #1501 stored:

- each `assumptions[]` string → a row, `status: ai-default`, `evidence_basis:
  inferred`, no `review_ask`, `options: [<the string as stated>]`,
- each `divergences[]` entry → a row, `evidence_basis: conflicting`,
  `conflict_signals: [script, data]`, `recommended` = its `resolution`,
  `review_ask: recommended-confirmation`,
- each `open_questions[]` string → a row with `review_ask:
  recommended-confirmation` and no options yet (the publisher adds them on the
  next publish).

The API keeps accepting the three brief lists for one release and converts them
on write. After that they are refused like any unknown key. Narrative-level
`assumptions` / `divergences` / `open_questions` in a review's `request_json`
(read by `apps/storyboards/cuts.py` `caveats`) are converted the same way at
read time, scoped to that narrative.

### Layout

The order follows ace-web: what the reader must do comes first. Each row
appears in exactly one place.

1. **Answer before sharing**: rows with `required-before` / `external-share`.
   Pinned at the top and open.
2. **Please confirm**: rows with `recommended-confirmation`. Open.
3. **What the agent assumed**: everything else, collapsed, grouped by scope.
4. **Not needed now**: deferred rows. Collapsed.

Superseded rows sit behind a toggle. Every row uses one component: the plain
question, the current value with a chip (`recommended`, `confirmed by X`,
`changed by X`, `pending`), the reasoning on expand, and the comment thread.

### Actions on a row

- **Confirm**: keep the recommended value. Sets `confirmed`.
- **Pick another option.**
- **Write in a value**: appended to `options`.
- **Comment.**

The first three are edits. While a round is open, an edit is **staged** on the
round, not on the row (section C), and shows as `pending`. It lands when the
round closes. That makes the round's batch hold everything that changed, and two
people disagreeing on one row is visible before anything is applied. If two
staged edits on the same row conflict at close, the closer picks one.

**Outside a round, a member's edit applies immediately** (owner decision,
2026-10-15): it is attributed to the signed-in member, recorded in `history`,
and undoable. A link holder (no account) can edit only inside an open round,
where the edit is staged; outside one, their edit is refused with 409 and the
page tells them no round is open.

Who may edit: workspace members, plus anyone holding the share link when the
board's capability is `suggest` (inside a round only). Comments follow the
`comment` grant.

### What is shared with ace-web

- **One contract:** the field names, the status and `review_ask` vocabularies,
  and the rule that "a human ruling binds the next run". Both repos cite ACE's
  `docs/decisions-contract.md`. A test in canopy-web pins the vocabularies to the
  values copied from `lib/decisions-schema.ts`, and fails on drift.
- **Mirrored, not shared:** storage (canopy-web uses Postgres, ace-web uses
  Drive YAML plus `decision-overrides.yaml`) and the React components. A shared
  package is not worth it for two consumers in different stacks. Revisit if a
  third appears.

### How the next run reads the rulings

The round's close payload (section C) carries every decision whose `status` is
`human-decided` or `overridden`, and every row `confirmed` in that round. The
publisher must treat these as binding:

- The next version's narrative uses the ruled value.
- The next publish re-sends the row with the same `id`, and the server keeps the
  human status and history.
- A publish that tries to reset a human-decided row to `ai-default` is refused
  with 409. To change a ruling, the agent supersedes the row and states why.

## C. Feedback rounds

### The object

A `FeedbackRound` belongs to one storyboard and links to the storyboard's
`agent_project`. Opening a round requires that link.

**Where the model lives.** `Feedback` is framework tier (`apps/feedback`) and
`storyboards` is product tier; a framework model may not hold an FK to a product
one (`tests/test_architecture_boundary.py`). So `FeedbackRound` lives in
`apps/feedback`, generic over its target exactly like `Feedback`
(`target_kind="storyboard"`, `target_ref=<slug>`, `workspace`), and
`apps/storyboards` owns every route and the rules that make it a storyboard
round. `staged_edits` is a JSON list for the same reason: the framework app
stores them; the storyboard service applies them to `StoryboardDecision` rows.

| Field | Meaning |
|---|---|
| `storyboard` | The board under review. |
| `number` | 1, 2, 3, … per board. |
| `state` | `open`, then `closed`, then `consumed`. |
| `pinned_versions` | `{narrative_slug: version}` frozen at open, so every comment is about the same cut. |
| `opened_by`, `opened_at` | Who opened the round, and when. |
| `closed_by`, `closed_at`, `close_note` | Who closed it, when, and what they said when closing. |
| `staged_edits[]` | Decision edits: who, row id, new value, reasoning, and when. |
| `task` | The `AgentTask` the close created. |
| `consumed_by_run` | The DDD run that read it. |

`Feedback` gains a nullable `round` FK. Every note left while a round is open
gets it. A note left with no open round has no round, and the next round picks
it up when it opens.

Only one round per board can be open at a time.

### Who may comment

- **Link holders**, as the board's capability allows. Guests must type a name;
  the server refuses an empty one.
- **Signed-in members** comment under their own name. They do not type one; it
  comes from `submitted_by`.
- **What can be commented on**:
  - a cut (`cut:<id>`),
  - a scene (`scene:<id>`),
  - a decision (`decision:<id>`),
  - the product screen a cut films (`product:<cut id>`, which carries the Labs
    URL from part A),
  - the set as a whole.

### Visibility changes

**Everyone in an open round sees everyone's comments.**

This reverses the rule in `list_notes`. That rule hid other reviewers' notes
from token holders "to keep the read independent". It made sense for a one-off
read. A round is different: people building on each other's points is the
whole reason to have one.

How it works:

- The board's GET returns the round's notes to anyone who can read the board.
- Each note shows its author's name. Emails are never shown to non-members.
- Outside a round, nothing changes: notes stay members-only.

### Closing a round

The board's owner and its editors can close a round. Commenters and guests
cannot.

Close is one transaction:

1. The round becomes `closed`. No more notes or edits are accepted into it.
2. Staged decision edits apply to their rows. The closer picks the winner of any
   conflict.
3. One `AgentTask` is created on the linked project. Its card summarises the
   round: number of notes, number of people, decisions changed, and the close
   note. Its payload names the round.
4. The close counts as one `approve` on that task through `act()`. That calls
   `enqueue_task_turn` and starts **one** turn.

This is the opposite of today's project replies, where every editor reply starts
its own turn. The round is how several people contribute before the agent
moves. Closing it is the one deliberate trigger.

### The DDD step that reads the round

This is a new canopy skill step, `ddd-feedback-round`, run by the turn the close
starts.

1. **Read** the round: `GET /api/storyboards/<slug>/rounds/<n>`. It returns the
   notes, the applied decision edits, and the pinned versions.
2. **Cluster** the notes per narrative and scene, across people and channels.
3. **Decide each note's disposition** and record it with `resolve_feedback`:
   - `answered`, with `resolved_in_version` = vN+1 and a `disposition_note`
     saying what changed, or
   - `declined`, with the reason.
4. **Draft vN+1** of each affected narrative. The decision rulings are binding.
   Then re-render the cuts and publish through the normal path.
5. **Record** the consumed feedback ids and the round number on the DDD run doc
   (`put_run_doc_state`). Mark the round consumed:
   `POST /api/storyboards/<slug>/rounds/<n>/consume` with the run id.
6. **The next round opens automatically** on the new versions (owner decision,
   2026-10-15). The consume call opens round N+1 in the same transaction,
   pinned to each narrative's current version — which is vN+1, because step 4
   published it first. Nobody has to remember to reopen the board, and notes
   left between rounds always have a round to land in.

A note left in state `new` after the step fails the step. That makes the rule
"every comment gets an answer" enforced, not prose.

### Review-page suggestions join the same pool

`suggest_review` writes a `Feedback` row (target the narrative, anchor the
scene, the round if one is open on a board that contains the narrative), in
addition to `suggestions_json` for one release. A narrative then has one place
its feedback lives.

`submit_review` is unchanged: a single gate decision is still a different thing
from a round.

### "What happened to my comment"

On the next version's board, each note from the previous round shows:

- its disposition (changed in vN+1, answered, or declined),
- the `disposition_note`,
- a link to the scene or cut where it landed.

The board gets a filter: **My notes from round N**. A guest sees their own notes
by the name they typed, scoped to the share token. This is the same promise as
ACE's feedback ledger, built on the same `Feedback` states.

### Alignment with project contribution

| Concept | Reused | New |
|---|---|---|
| Who takes part | `ProjectParticipant`. The first note from a signed-in member adds them as a participant on the linked project (`source: storyboard`). | — |
| Waking the agent | `AgentTask` + `act(approve)` → `enqueue_task_turn` | The round summary card |
| Comment store | `Feedback`, `resolve_feedback` | `round` FK, `decision:` / `product:` anchors |
| Iteration record | DDD run doc | Consumed round and feedback ids |
| Batching | — | `FeedbackRound` |

The round is deliberately generic over its target in the data model. A later
"round on a project task" or "round on a run doc" is the same object with a
different owner. This spec builds it for storyboards only.

## Non-goals

- Per-person grants on a storyboard. The board's one-grant model stays.
- Quorum or voting rules for closing a round. The owner or an editor closes it.
- Rounds on anything other than storyboards.
- Notifications beyond the existing suggestion email. A digest can come later.
- Moving ace-web onto this model.

## Owner decisions (Jonathan Jackson, 2026-10-15)

The three open questions, answered:

1. **The next round opens automatically.** When the agent consumes round N it
   opens round N+1 on the new versions (section C, step 6). The owner opens only
   the first round on a board.
2. **A member's edit outside a round applies immediately**, attributed and
   undoable (section B, "Actions on a row"). Inside a round, every edit is
   staged until close.
3. **`needed_by` keeps `external-share` / `next-run`.** ace-web's stage names
   (`award`, `go-live`, `closeout`, `extension`) describe running a programme,
   not a set of demos; the drift test pins canopy-web's own vocabulary and ACE's
   shared ones (`status`, `review_ask`, `evidence_basis`) separately.

## Test plan

**canopy-web**
- **Model and API**
  - Create, edit, confirm, write-in and undo on a decision.
  - A staged edit during a round, and the conflict pick at close.
  - A 409 on an attempt to reset a human-decided row.
  - The brief-to-rows migration, including the read-time conversion of
    narrative caveats.
  - The vocabulary-drift test against values copied from `decisions-schema.ts`.
- **Rounds**
  - Open → note → close → consumed.
  - Close creates exactly one task and enqueues exactly one turn. Assert the
    count while several people comment.
  - Notes are visible to round participants, and emails stay hidden.
  - A closed round refuses new notes.
  - Only one round can be open per board.
- **Review suggestions** land in the pool with the round.
- **Frontend**
  - The decision section's grouping and order.
  - The row component in each status.
  - Round banner, staged chips, the close dialog, and the "My notes" view.
  - The Playwright storyboard spec, extended.

**Plugins**
- A unit test of the `ddd-feedback-round` step against a fixture round. It must
  fail when a note is left `new`.

## Rollout

Order: **B, then C.** Rounds need decision rows to stage edits against.

1. **canopy-web B**
   - `StoryboardDecision`, its API, the migration and the UI.
   - Brief lists accepted for one release and converted on write.
2. **ACE publisher**
   - `skills/demo-narrative` and the storyboard publish in `bin/ace-canopy-web`
     send `decisions[]` instead of the brief lists.
   - Re-publish the RUTF board.
3. **canopy-web C**
   - `FeedbackRound`, the `round` FK, the close-to-task wiring.
   - Visibility, review suggestions joining the pool, and "My notes".
4. **canopy plugin**
   - The `ddd-feedback-round` step and the run-doc recording.
   - A `ddd` orchestrator branch: when a turn carries a round, run this step
     instead of polling `response_json`.
5. **ACE**
   - `skills/narrative-iteration-review` reads rounds as well as review
     responses.
   - Its feedback-ledger view renders `Feedback` dispositions.
6. **Pilot**
   - Open round 1 on the RUTF board for Lily's team before the 12 November
     Edesia demo.
