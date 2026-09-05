# Actor-aware runner routing

**Date:** 2026-09-05
**Status:** Design — approved in outline, not yet built
**Builds on:** `2026-07-27-source-aware-runner-routing-design.md` (source rules on
`RunnerAssignment`, the `explicit → sticky → source → default` ladder,
`assignment_rows_for`, the claim/unclaimable parity discipline)
**Related:** `2026-07-24-directed-runner-routing-design.md`,
`2026-07-25-cloud-agent-bootstrap-design.md`,
ace-web `docs/plans/2026-07-26-run-convergence-ace-side.md`

## Problem

Multiplayer is built and switched off, and it is switched off for a routing reason.

Everything the feature needs has shipped: multiplayer chat (SP3), ace-web's chat
cutover, cross-app presence, run-execution convergence
(`CANOPY_RUN_EXECUTION=true` is live in ace-web's deployed CloudFormation), and
source-aware routing itself. `cloud-ec2-1` is online, ready, session-capable, and
passed readiness drills for ace/echo/eva/hal on 2026-08-12/13 — the ACE drill ran
`bin/ace-doctor` on the box end to end and returned *47 PASS / 13 WARN / 0 FAIL,
HEALTHY*.

It has nonetheless claimed **zero turns**. Of the 100 turns created since the
operator paused the `jj-mbp-cdp` account on 2026-08-27:

```
 89  -> acedimagi-mbp-cdp
 11  -> (unclaimed)
  0  -> jj-mbp-cdp        (the pause working correctly)
  0  -> cloud-ec2-1
```

Every claimed turn in the fleet — five agents, every source — lands on a single
macOS account on the operator's own machine. Widening to the last 200 turns
(2026-08-17 →) splits those claims across `jj-mbp-cdp` and `acedimagi-mbp-cdp`, but
that is the *same operator on two accounts* before and after a pause, not two
people; `cloud-ec2-1` is still at zero across the whole window.

`cloud-ec2-1` is `enabled: false` on **every** agent's assignment list, and no agent
has a single source rule (`GET /api/agents/{slug}/runner-rules` → `[]` for all five).
The CloudFormation comment justifying `CANOPY_RUN_EXECUTION=true` asserts *"the
ace_web source rule is non-strict, so a dead cloud runner degrades to the laptop"* —
that rule does not exist.

The history says the switch-off was deliberate, not a regression. Sixteen canopy
sessions carry `metadata.source=ace-web`, **all sixteen on 2026-07-28**: a bring-up
arc (`ace toolchain probe`, `ace mcp tool probe`, `ace:status on the cloud runner`,
`routing check: derived origin`) ending in `ace@ round-trip: ace-web -> cloud runner`
and `/ace:run bednet-spot-check`, both bound to `cloud-ec2-1`. It worked. Then it was
turned off and nothing has used it in five weeks.

**Why it was turned off is the actual problem.** Source rules answer "which box runs
this *kind* of work". They cannot answer "which box runs *this person's* work". So
enabling the cloud runner is all-or-nothing per source: the moment `ace_web` or
`email` routes to cloud, the operator's own debugging work goes there too — and the
operator needs it local. The safe configuration is therefore "cloud off", which is
the configuration that has held for five weeks and blocks every other person from
using any of the shipped machinery.

Granularity is not a follow-on to multiplayer. It is its precondition.

## Two things that are not true, and would have mis-routed everything

The obvious implementation — key rules on `Turn.enqueued_by_email` — is wrong twice,
and both failures are silent.

**`enqueued_by_email` on an email turn is not the sender.** It is set once, at
`apps/harness/api.py:1030`, as `request.user` — the account the *runner* authenticates
as when its inbox watcher POSTs the turn. All 63 email turns in the sample read
`jjackson@dimagi.com` regardless of who wrote in:

```
enq= jjackson@dimagi.com  | from= Jonathan Jackson <jjackson@dimagi.com>
enq= jjackson@dimagi.com  | from= Beth Geoffroy <egeoffroy@dimagi.com>
enq= jjackson@dimagi.com  | from= Labs Alerts <no-reply@sns.amazonaws.com>
```

Keying on it would collapse every sender onto one rule. The true actor is
`origin_ref["from"]`, which the runner already writes
(`runner/canopy_runner/canopy_runner/inbox.py:145`).

**Chat turns carry no actor at all.** 30/30 `canopy_web_chat` turns in the sample have
`enqueued_by_email` empty, because `canopy_sessions/services.py` calls
`harness_services.enqueue_turn(...)` without it. ace-web dispatches through that same
session-send path (`apps/canopy/run_dispatch.py` → `client.send_message` →
`POST /api/canopy-sessions/{id}/send`), so **`ace_web` turns would have no actor
either** — the single most important source for this feature.

So the actor must first be made *observable*, and it is not one field.

## Decisions

- **The actor is a resolved value, not a column.** One pure function maps a turn to
  a normalized actor; each origin names where its actor lives. A second stored
  column beside `origin_ref`/`enqueued_by` is exactly the two-overlapping-writers
  mistake the source spec rejected when it declined to add `source` beside `origin`.
- **`enqueued_by` gets set on the chat/ace-web send path.** Not a new field — a
  field that already exists, already has the user in hand (`send_message(*, session,
  text, user, ...)`), and simply isn't threaded through. This is the whole ace-web leg.
- **Actor rules are the same rows as source rules**, with one more column.
  `actor=""` means *any actor* — bit-for-bit today's behaviour, so no existing row
  changes meaning and the migration cannot alter routing.
- **One actor per rule.** Two addresses for one person (Beth appears as both
  `bgeoffroy@` and `egeoffroy@`) is two rules. No comma-lists, no cohort objects —
  each rule stays independently toggleable and auditable, which is what "while we
  work out the kinks" needs.
- **A rule is an ordered LIST of runners, not one runner.** This is a change from
  the source-rule model, and it is forced by the operator's actual topology:
  `jj-mbp-cdp` and `acedimagi-mbp-cdp` are two macOS accounts on the operator's
  own machine, alternated as each runs out of tokens. So "my work stays on my
  boxes, never cloud" names *two* runners whose live one **rotates**. A
  single-runner rule cannot say it: strict names one box and parks whenever the
  other account is the active one, and non-strict falls through to the default
  order — which, on a cloud-default agent, puts cloud *above* the operator's other
  laptop and lands the work in exactly the place the rule existed to avoid.
  The source spec left the door open for this, keeping `rank` on the row because
  *"the column stays free for a future ordered source list without another
  migration."* This is that future; `rank` becomes meaningful within a rule.
- **`strict` is per-rule, and stays per-rule.** It already is a boolean on the row;
  actor rules inherit it unchanged. There is no global strict mode, and the direction
  is not fixed: a strict rule pointing *at* cloud keeps other people's work off the
  operator's laptop, and a strict rule pointing *at* the laptop keeps the operator's
  work off cloud. Both are wanted, on the same agent, at the same time.
- **Precedence extends rather than branches:**
  `explicit pin → sticky session binding → (source, actor) → (source, "") → default order`.

## Actor resolution

```python
def resolve_actor(origin: str, origin_ref: dict, enqueued_by_email: str) -> str
```

Pure, no queries, no clock. Returns a lowercased bare address, or `""` when the turn
has no human actor.

| origin | actor source | note |
|---|---|---|
| `email` | address parsed from `origin_ref["from"]` | `Beth Geoffroy <egeoffroy@dimagi.com>` → `egeoffroy@dimagi.com` |
| `ace_web` | `enqueued_by.email` | the signed-in ace-web user, via the delegated token |
| `canopy_web_chat` | `enqueued_by.email` | |
| `slack` | `enqueued_by.email` | reserved; no producer yet, and needs none beyond setting `enqueued_by` |
| `api` | `enqueued_by.email` | |
| `canopy_scheduler` | `""` | a schedule has no live human. Extension point: `AgentSchedule.created_by` |

Parsing is `email.utils.parseaddr` — it is stdlib, it is what wrote the header, and
hand-rolled `<...>` slicing fails on quoted display names like
`"Anthropic, PBC" <invoice+statements@mail.anthropic.com>`, which is a real value in
the sample.

A turn whose actor resolves to `""` matches no actor rule and falls through to the
source rule, then the default list. That is the correct behaviour for every
scheduler turn and for any turn enqueued by an unauthenticated caller.

## The rule model

`RunnerAssignment` gains one column:

```python
actor = models.CharField(max_length=254, blank=True, default="")   # 254 = RFC 5321
```

A **rule** is now the set of rows sharing `(agent, source, actor)`, ordered by `rank`.
The `one_priority_runner_per_agent_source` constraint — which capped a rule at one
runner — is replaced by one that caps a *runner* at one appearance per rule:

```python
models.UniqueConstraint(
    fields=["agent", "source", "actor", "runner"],
    condition=~models.Q(source=""),
    name="one_row_per_runner_per_agent_source_actor",
)
```

`one_default_assignment_per_agent_runner` (`condition=Q(source="")`) is untouched —
a default row never carries an actor, and `actor` is not in its key.

Two properties are rule-level but stored per row:

- **`strict`** must agree across every row of a rule; the API writes one value to
  all of them and the composer reads the first. Enforced on write (422), not left
  to disagree silently.
- **`enabled`** stays genuinely per row, so one runner can be dropped from a rule
  without deleting it. A rule whose rows are *all* disabled vanishes from
  `load_assignment_rows` and the turn falls through — which is the existing
  documented semantics ("a disabled source row is simply the rule switched off"),
  and it means **disabling a strict rule also disables its strictness**. That is
  intended and worth stating: switching a rule off must not park a queue.

Relaxing a uniqueness constraint can never fail on existing data, and no existing
row has a non-empty `actor`, so every current rule keeps meaning exactly what it
means today — a one-runner rule is just a rule of length one.

### Claim-time composition

`assignment_rows_for` gains one parameter and one rung. It stays pure.

`priorities` maps `(agent_id, source, actor)` to a **rank-ordered list of rows** —
the rule — rather than to a single row.

```python
def assignment_rows_for(agent_id, origin, actor, defaults, priorities) -> list:
    base = defaults.get(agent_id) or []
    exact = priorities.get((agent_id, origin, actor)) if actor else None
    anyone = priorities.get((agent_id, origin, ""))
    ladder = [rule for rule in (exact, anyone) if rule]
    if not ladder:
        return [(i, r) for i, (_rank, r) in enumerate(base)]

    seen, out, truncated = set(), [], False
    for rule in ladder:                           # actor rule, then source rule …
        for row in rule:                          # … each already rank-ordered
            if row.runner_id not in seen:
                seen.add(row.runner_id)
                out.append(row.runner)
        if rule[0].strict:                        # "these runners or nothing":
            truncated = True                      # nothing below this rung may claim
            break
    if not truncated:
        out += [r for _rank, r in base if r.id not in seen]   # … then the default order
    return list(enumerate(out))
```

**Any strict rung truncates the list at itself** — that is what makes "and nowhere
else" actually hold, and it is why the default order must be appended only when no
rung truncated. Appending it unconditionally would let a strict rule leak work back
to the very runners it exists to exclude, and the wedged-runner grace would then
promote them after 60s. (Writing that bug and catching it in review is the reason
this block is spelled out rather than described.)

A non-strict actor rule composes down through the source rule and then the default
order — honouring both layers rather than skipping the middle one, which would make
a source rule silently inert for anyone who also had an actor rule.

`load_assignment_rows` keys `priorities` on `(agent_id, source, actor)` instead of
`(agent_id, source)`. It stays one query.

`_assignment_allows_for_agent` resolves the actor from the turn it already holds and
passes it through. Rank renumbering, `is_available`, `enabled`, the 60s
`CASCADE_GRACE_SECONDS` and drills are all untouched — the cascade walks whatever
list it is handed.

### The two other callers

**`unclaimable_queued_turns` must resolve the actor the same way.** These two
disagreeing is the drift class this codebase already has a parity test for
(`tests/test_claim_schedule_parity.py`); coverage becomes per-`(agent, origin, actor)`.
A strict actor rule pointing at an offline box must report `offline` (recoverable),
never `config` (never runs).

**`inbound.online_runners_for` rings with `actor=""`, and that asymmetry is
deliberate.** A Gmail push carries a mailbox, not a sender — the sender is only known
after `gog gmail thread get`, which happens on the runner *after* the ring. So the
doorbell composes at the source rung and rings that set. This is safe and is already
the module's stated design: it rings *all* eligible runners rather than the best rank,
the enqueue is idempotent per `(thread, messageCount)`, and a ring we get wrong costs
latency, never correctness. Concretely: a strict `(ace, email, stewari@) → cloud` rule
means the laptop may be the box that *discovers* Sarvesh's thread while cloud is the
box that *answers* it — the enqueue lands, the laptop is refused at claim time, and
cloud takes it on its own 5s claim poll. Stated here because it will otherwise be
re-derived as a bug.

## API

**`AgentRunnerRuleOut` stays flat — one entry per row**, gaining `actor` and `rank`.
A rule of two runners is two entries sharing `(source, actor)`; the UI groups them.
Keeping it flat preserves the shape the frontend already consumes (`runner_name`,
`kind`, `online`, `ready`, `enabled` per runner) instead of nesting it, and the
derived-liveness fields are per runner anyway.

**`AgentRunnerRuleIn` becomes rule-shaped**, reusing the row schema the default-list
PUT already has:

```python
class AgentRunnerRuleIn(StrictModel):
    source: RoutableSource
    actor: str = ""                      # "" = any actor (today's source rule)
    runners: list[AgentRunnerRowIn]      # ORDERED; rank = list index
    strict: bool = False                 # rule-level; written to every row
```

This drops the rule-level `enabled` in favour of the per-row `enabled` inside
`AgentRunnerRowIn`. That is a breaking change to the request body — acceptable
because the endpoint's only consumer is canopy-web's own frontend and **there are
zero rules in production today** (all five agents return `[]`), so there is no
migration to perform and no third-party caller to break. Worth doing now rather
than after rules exist.

- **Dedupe key in `replace_agent_runner_rules` becomes `(source, actor)`** across
  rules, and `runner_id` within a rule; the 422s are
  `"one rule per (source, actor): duplicate in list"` and
  `"a runner may appear once per rule: duplicate runner in <source>/<actor>"`.
- **An empty `runners` list is a 422**, not a rule that matches and yields nothing.
  A zero-length strict rule would compose to an empty list and park the queue with
  no runner named as the reason — deleting the rule is how you turn it off.
- The existing wholesale-replace discipline is unchanged, as is its scoping to
  `.exclude(source="")` — the default-list PUT must still not clobber rules, and
  vice versa.
- **`actor` is normalized at the boundary** (lowercase, `parseaddr`'d) so a rule
  written as `Sarvesh Tewari <STewari@dimagi.com>` matches a turn from
  `stewari@dimagi.com`. A rule that cannot be normalized to something containing `@`
  is a 422, not a row that silently never matches.
- **`queued_count` becomes per-`(source, actor)`.** Today it is one `values_list("origin")
  .annotate(Count)`. With actors it must resolve the actor of each queued turn, so it
  becomes a small Python group-by over `Turn.objects.filter(agent, status=QUEUED)
  .only("origin", "origin_ref", "enqueued_by")`. Queued sets are single digits in
  practice; this is not a hot path, and getting it wrong makes the parked warning lie.
  Every row of a rule repeats its rule's count — the parked queue belongs to the
  rule, not to one runner in it.
- `GET`/`PUT` paths, auth, and `_runner_visibility_q` gating are unchanged.

Regenerate `frontend/src/api/generated.ts` (`npm run gen:api`) — `regen-openapi.yml`
fails the PR otherwise.

## UI — Runners tab

`RunnerSourceRules.tsx` keeps its shape; each rule line gains an optional actor field.

```
▾ Echo
    DEFAULT ORDER
    [1 ● cloud-ec2-1 cloud ↑ ↓ ⏻]  [2 ● acedimagi-mbp emdash ↑ ↓ ⏻]  [3 ● jj-mbp-cdp emdash ↑ ↓ ⏻]  [+ add]

    EXCEPT WHEN THE WORK COMES FROM
    email   from [jjackson@dimagi.com]  →  [1 ● acedimagi-mbp ↑ ↓ ⏻] [2 ● jj-mbp-cdp ↑ ↓ ⏻] [+]   ( only | fall through )  ✕
    api     from [jjackson@dimagi.com]  →  [1 ● acedimagi-mbp ↑ ↓ ⏻] [2 ● jj-mbp-cdp ↑ ↓ ⏻] [+]   ( only | fall through )  ✕
    email   from [anyone             ]  →  [1 ● cloud-ec2-1  ↑ ↓ ⏻]                          [+]   ( only | fall through )  ✕
    [+ rule]
```

- **A rule's runners render as the same rank chip row the default order uses**,
  with the same `↑ ↓ ⏻` affordances and the same optimistic-commit machinery
  (`rowsRef`, `commitSeqRef`). One interaction model for both, rather than a
  dropdown for rules and chips for the default list.
- The actor field is free text with placeholder `anyone`; empty renders as `anyone`
  and stores `""`, which is today's source rule. **A pre-existing one-runner source
  rule keeps rendering and editing identically** — it is simply a rule of length
  one — which is the property that makes this change additive rather than a
  migration of the UI's mental model.
- The source picker no longer excludes a source that already has a rule (several
  actors may share one source); it excludes only exact `(source, actor)` duplicates,
  which is what the API validates.
- Rules sort by `(source, actor)` with `actor=""` last, so the specific rules read
  above the catch-all — matching the order the cascade evaluates them in.
- The parked warning keeps its count and gains the actor:
  *"⚠ cloud-ec2-1 is offline — 3 ace_web turns from stewari@dimagi.com are parked,
  and will stay parked."*
- The precedence ladder is stated once where rules are edited, updated to five rungs.

## Migration

1. Add `RunnerAssignment.actor`; swap `one_priority_runner_per_agent_source` for the
   four-field `one_row_per_runner_per_agent_source_actor`. No data migration —
   every existing row gets `actor=""`, which is its current meaning, and `rank`
   (previously written `0` and ignored on a source row) becomes meaningful within a
   rule while `0` stays correct for a rule of length one.
2. `canopy_sessions/services.py`: thread `user` into `enqueue_turn(..., enqueued_by=user)`
   on **both** send paths — `send_message` and `_send_transcript_sourced_message`
   (the latter needs `user` added to its signature; it does not take one today).
3. `harness/services.py`: `load_assignment_rows` re-keys; `assignment_rows_for` gains
   `actor`; `_assignment_allows_for_agent` resolves and passes it.
4. `harness/actors.py` (new): `resolve_actor`. Pure, unit-tested against the real
   header shapes observed in production.
5. `unclaimable_queued_turns` and `inbound.online_runners_for` updated per above.
6. API schemas, endpoint, generated types, `RunnerSourceRules.tsx`.

Migrations run before cutover, so old code briefly meets the new schema — an added
nullable-defaulted varchar and a swapped conditional constraint are both compatible
with it, and old code composing without an actor gets precisely today's behaviour.

## Day-one configuration (the flip)

The point of the work. Applied **after** the code lands and a real round-trip is
verified, not before.

**`OPERATOR_BOXES` = `[acedimagi-mbp-cdp, jj-mbp-cdp]`** — one machine, two macOS
accounts, alternated as each runs out of tokens. `acedimagi` is listed first because
it is the live one (100% of the 100 turns since the 2026-08-27 pause). Any rule that
means "the operator's own work" names **both, in that order** — never one.

| agent | default order | rules |
|---|---|---|
| **ace** | unchanged: `OPERATOR_BOXES` | `(email, stewari@dimagi.com) → [cloud-ec2-1]` **only**<br>`(ace_web, stewari@dimagi.com) → [cloud-ec2-1]` **only**<br>`(email, <matt>) → [cloud-ec2-1]` **only**<br>`(ace_web, <matt>) → [cloud-ec2-1]` **only** |
| **echo** | `[1 cloud-ec2-1] + OPERATOR_BOXES` | `(email, jjackson@dimagi.com) → OPERATOR_BOXES` **only**<br>`(canopy_web_chat, jjackson@dimagi.com) → OPERATOR_BOXES` **only**<br>`(api, jjackson@dimagi.com) → OPERATOR_BOXES` **only** |
| **eva, hal, ada** | unchanged (operator-default) | none |

ACE is an **allowlist to cloud**: named people go to the cloud box, everything else —
including all of the operator's own work — stays on the operator's boxes while the
kinks get worked out. Note ACE therefore needs **no rule for the operator at all**:
its default order is already exactly `OPERATOR_BOXES`.

Echo is the inverse — cloud-default, with the operator carved back out by rule. That
carve-out is the case that *requires* multi-runner rules, and it is why the model
changed.

Strict in both directions is deliberate: strict cloud rules keep other people's work
off the operator's boxes (the isolation the whole feature exists for), and the strict
operator rules keep the operator's work off cloud without pinning it to whichever
account happens to be logged out.

**One prerequisite this config depends on, currently unmet:**

- **`cloud-ec2-1` must be `enabled: true`.** It is `enabled: false` on all five
  agents today, which is the single reason it has claimed nothing. An actor rule is
  its own row with its own toggle, so ace's rule rows can be enabled without touching
  ace's default list — but echo's cloud-default *does* require flipping the existing
  rank-2 row and re-ranking it to 0.

**Not a prerequisite, contrary to an earlier draft of this spec:** `jj-mbp-cdp` being
paused does not need fixing before applying these rules. Under the multi-runner model
its rules also name `acedimagi-mbp-cdp`, which is online — so a paused account
degrades within the rule instead of parking the queue. Removing that prerequisite is
the concrete payoff of the model change.

**Correction worth recording:** an earlier draft singled Echo out because it was the
only agent with a non-empty `Agent.runner_preference` (`['cloud','emdash']`). That
field does not route. It is read in exactly one place —
`harness/services.seed_assignments_from_capabilities`, the one-time bridge behind
data migration `0024_seed_runner_assignments` — and the frontend calls it "the
deprecated kind-based `runner_preference` … supersed[ed] by RunnerAssignment". All
five agents have identical live routing config. Echo's selection here is a product
choice, not a reflection of existing state, and `runner_preference` should be treated
as vestigial by anything reasoning about routing.

`<matt>`'s address is the one input not yet resolved; it is not in `config/allowlist.txt`
(which is domain-wide `@dimagi.com`) and no fleet turn carries it. ACE's other three
rules do not depend on it and can ship first.

## Testing

Extends `tests/test_source_rules.py` and the parity discipline rather than starting a
new suite.

- **Resolution** — every row of the actor table, against the real header shapes:
  bare address, `Display Name <addr>`, `"Quoted, Name" <addr>`, empty, malformed.
  Case-folding. `canopy_scheduler` → `""`.
- **Composition** — no rule → default; actor rule non-strict → actor rule's runners
  in rank order, then the source rule's, then defaults, deduped; actor rule strict →
  that rule's runners **and no others**; actor rule absent but source rule present →
  today's behaviour exactly; a rule whose rows are all disabled → falls through
  (and its strictness falls through with it); actor `""` rule of length one behaves
  identically to a pre-migration source rule.
- **Multi-runner rules — the case the model exists for.** A strict two-runner rule
  `(echo, email, jjackson@) → [acedimagi, jj-mbp]` composes to exactly those two in
  that order, never cloud; with `acedimagi` unavailable it yields `jj-mbp` rather
  than parking; with **both** unavailable it parks rather than falling to cloud.
  All three are asserted, because the middle one is the whole point and the third is
  what makes it strict.
- **Strictness holds past the grace** — a queued strict actor turn older than
  `CASCADE_GRACE_SECONDS` still refuses every non-priority runner.
- **Precedence** — a pinned turn ignores a contradicting actor rule; a bound session
  ignores one; an actor rule beats a source rule beats the default order.
- **Parity** — `unclaimable_queued_turns` and `claim_next_turn` agree per
  `(agent, origin, actor)`.
- **The wipe** — `PUT /runners` leaves actor rules intact; `PUT /runner-rules`
  leaves the default order intact; two rules differing only in `actor` both survive.
- **Frontend** — `RunnerSourceRules.test.tsx` changes shape: `availableSources()`
  must stop excluding a source that already has a rule (several actors share one
  source) and `nextRulesForRemove()` must key on `(source, actor)`. Both existing
  assertions are wrong under this design and must be updated, not deleted — a rule
  with `actor=""` is still one-per-source.
- **Producer** — a chat send and an ace-web dispatch both land a turn whose
  `enqueued_by` is the sending user; an email turn's actor is the sender, **not**
  `enqueued_by`. This is the regression test for the two false assumptions above.
- **Doorbell** — `online_runners_for` composes at the source rung and is unaffected
  by a strict actor rule (it rings the superset).
- **End to end, the three real cases:** `(ace, email, stewari@) → cloud strict` while
  `(ace, email, jjackson@) → default laptops`; `(echo, email, jjackson@) → jj-mbp
  strict` while echo's default is cloud; a scheduler turn matches no actor rule.

## Out of scope

- **A Slack producer.** `slack` is already a reserved origin and needs nothing here
  beyond setting `enqueued_by` when it lands; actor rules will work on it for free.
- **Actor cohorts / groups.** One actor per rule, deliberately — two people wanting
  the same routing is two rules. Revisit if the rule list outgrows a screen. (The
  *runner* side is now a list; the *actor* side is deliberately not.)
- **Retiring `Agent.runner_preference`.** Confirmed vestigial by this work — read
  only by the `0024` seed bridge, described as deprecated by the frontend. Deleting
  it is a separate, unrelated cleanup and touches the agents API surface.
- **Scheduler actors** (`AgentSchedule.created_by`). Named as the extension point;
  no current need routes on it.
- **Retiring `turn_driver`** (ace-web Task 12). Unblocked by this work in principle —
  its precondition was a session-capable cloud runner actually taking ace-web's
  turns — but it is ace-web's change and its own decision.
- **Hard workspace isolation** between ace workspaces on canopy (the residual
  documented in ace-web's `CLAUDE.md`: session LIST is scoped by `origin_key`, but a
  canopy workspace member can still open a session by id). Unrelated to routing.
