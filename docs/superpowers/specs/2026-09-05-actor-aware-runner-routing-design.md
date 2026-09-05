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

It has nonetheless claimed **zero turns**. Measured over the 200 most recent turns,
2026-08-17 → 2026-09-04:

```
39  canopy_scheduler -> jj-mbp-cdp        22  canopy_web_chat -> acedimagi-mbp-cdp
34  email            -> jj-mbp-cdp        17  api             -> jj-mbp-cdp
29  email            -> acedimagi-mbp-cdp 15  api             -> acedimagi-mbp-cdp
24  canopy_scheduler -> acedimagi-mbp-cdp  9  canopy_web_chat -> jj-mbp-cdp
                                           0  * -> cloud-ec2-1
```

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
- **One actor per row.** Two addresses for one person (Beth appears as both
  `bgeoffroy@` and `egeoffroy@`) is two rows. No comma-lists, no cohort objects —
  each rule stays independently toggleable and auditable, which is what "while we
  work out the kinks" needs. `RunnerAssignment.rank` remains the escape hatch if an
  ordered per-actor list is ever wanted.
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

The `one_priority_runner_per_agent_source` constraint is replaced by:

```python
models.UniqueConstraint(
    fields=["agent", "source", "actor"],
    condition=~models.Q(source=""),
    name="one_priority_runner_per_agent_source_actor",
)
```

`one_default_assignment_per_agent_runner` (`condition=Q(source="")`) is untouched —
a default row never carries an actor, and `actor` is not in its key.

No existing row has a non-empty `actor`, so the swap cannot fail and every current
rule keeps meaning exactly what it means today.

### Claim-time composition

`assignment_rows_for` gains one parameter and one rung. It stays pure.

```python
def assignment_rows_for(agent_id, origin, actor, defaults, priorities) -> list:
    base = defaults.get(agent_id) or []
    exact = priorities.get((agent_id, origin, actor)) if actor else None
    anyone = priorities.get((agent_id, origin, ""))
    ladder = [row for row in (exact, anyone) if row is not None]
    if not ladder:
        return [(i, r) for i, (_rank, r) in enumerate(base)]

    seen, out, truncated = set(), [], False
    for row in ladder:                            # actor rule, then source rule …
        if row.runner_id not in seen:
            seen.add(row.runner_id)
            out.append(row.runner)
        if row.strict:                            # "that runner or nothing":
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

`AgentRunnerRuleOut` and `AgentRunnerRuleIn` gain `actor: str = ""`.

- **Dedupe key in `replace_agent_runner_rules` becomes `(source, actor)`**, and the
  422 message becomes `"one rule per (source, actor): duplicate in list"`. The
  existing wholesale-replace discipline is unchanged, as is its scoping to
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
- `GET`/`PUT` paths, auth, and `_runner_visibility_q` gating are unchanged.

Regenerate `frontend/src/api/generated.ts` (`npm run gen:api`) — `regen-openapi.yml`
fails the PR otherwise.

## UI — Runners tab

`RunnerSourceRules.tsx` keeps its shape; each rule line gains an optional actor field.

```
▾ Ace
    DEFAULT ORDER
    [1 ● jj-mbp-cdp emdash ↑ ↓ ⏻]  [2 ● acedimagi-mbp emdash ↑ ↓ ⏻]  [+ add]

    EXCEPT WHEN THE WORK COMES FROM
    email    from [stewari@dimagi.com]  →  [● cloud-ec2-1 ▾]  ( only | fall through )  ✕
    ace_web  from [stewari@dimagi.com]  →  [● cloud-ec2-1 ▾]  ( only | fall through )  ✕
    email    from [anyone            ]  →  [● jj-mbp-cdp  ▾]  ( only | fall through )  ✕
    [+ rule]
```

- The actor field is free text with placeholder `anyone`; empty renders as `anyone`
  and stores `""`, which is today's source rule. **The existing rules keep rendering
  and editing identically** — this is the property that makes the UI change additive.
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
   three-field constraint. No data migration — every existing row gets `actor=""`,
   which is its current meaning.
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

| agent | default order | rules |
|---|---|---|
| **ace** | unchanged: `[1 jj-mbp-cdp] [2 acedimagi-mbp-cdp]` | `(email, stewari@dimagi.com) → cloud-ec2-1` **only**<br>`(ace_web, stewari@dimagi.com) → cloud-ec2-1` **only**<br>`(email, <matt>) → cloud-ec2-1` **only**<br>`(ace_web, <matt>) → cloud-ec2-1` **only** |
| **echo** | `[1 cloud-ec2-1] [2 jj-mbp-cdp] [3 acedimagi-mbp-cdp]` | `(email, jjackson@dimagi.com) → jj-mbp-cdp` **only**<br>`(canopy_web_chat, jjackson@dimagi.com) → jj-mbp-cdp` **only**<br>`(api, jjackson@dimagi.com) → jj-mbp-cdp` **only** |
| **eva, hal, ada** | unchanged (laptop-default) | none |

ACE is an **allowlist to cloud**: named people go to the cloud box, everything else —
including all of the operator's own work — stays local while the kinks get worked out.
Echo is the inverse, cloud-default with the operator carved back out, because Echo
already declares `runner_preference: ['cloud','emdash']`.

Strict in both directions is deliberate: strict cloud rules keep other people's work
off the operator's laptop (the isolation the whole feature exists for), and strict
laptop rules keep the operator's work off cloud.

**Two prerequisites this config depends on, both currently unmet:**

- **`cloud-ec2-1` must be `enabled: true`** on ace and echo. It is `enabled: false`
  on all five agents today. An actor rule is its own row with its own toggle, so the
  rule rows can be enabled without touching ace's default list — but echo's
  cloud-default *does* require flipping the existing row.
- **`jj-mbp-cdp` is paused** (`~/.canopy/PAUSED`, since 2026-08-27) and reads
  `online: false`. Every strict rule pointing at it will park immediately. That is
  the toggle working as specified, and the UI will say so with a count — but it must
  be an expected outcome, not a surprise. Unpause before applying echo's rules.

`<matt>`'s address is the one input not yet resolved; it is not in `config/allowlist.txt`
(which is domain-wide `@dimagi.com`) and no fleet turn carries it.

## Testing

Extends `tests/test_source_rules.py` and the parity discipline rather than starting a
new suite.

- **Resolution** — every row of the actor table, against the real header shapes:
  bare address, `Display Name <addr>`, `"Quoted, Name" <addr>`, empty, malformed.
  Case-folding. `canopy_scheduler` → `""`.
- **Composition** — no rule → default; actor rule non-strict → actor, source, defaults
  deduped in that order; actor rule strict → that runner only; actor rule absent but
  source rule present → today's behaviour exactly; disabled actor rule → falls to
  source rule; actor `""` rule behaves identically to a pre-migration source rule.
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
- **Actor cohorts / groups.** One actor per row, deliberately. Revisit if the rule
  list outgrows a screen.
- **Scheduler actors** (`AgentSchedule.created_by`). Named as the extension point;
  no current need routes on it.
- **Retiring `turn_driver`** (ace-web Task 12). Unblocked by this work in principle —
  its precondition was a session-capable cloud runner actually taking ace-web's
  turns — but it is ace-web's change and its own decision.
- **Hard workspace isolation** between ace workspaces on canopy (the residual
  documented in ace-web's `CLAUDE.md`: session LIST is scoped by `origin_key`, but a
  canopy workspace member can still open a session by id). Unrelated to routing.
