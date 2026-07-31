# Item ⊕ Turn: one object for work you do, one for work the agent does

**Status:** design approved, not yet implemented
**Date:** 2026-07-15
**Related:** `2026-07-05-agent-execution-control-plane-design.md` (the harness — `Turn`), `2026-07-14-canopy-mobile-design.md` (the supervisor surface + the Phase 3 composer), `2026-07-15-agent-scheduled-turns-design.md` (PR #218 — independently reached this spec's core conclusion; see "Independent corroboration"), `2026-06-24-canopy-framework-harvest-design.md` (the framework/product boundary), `2026-06-19-team-activity-timeline-design.md` (the timeline this reuses)

**Checked against `main` as of PR #220.** Revised after #216 (`apps/push`), #218 (scheduled turns, in flight), and #219 (runner review-spam fix) landed or opened — see §5b, "Independent corroboration", and §2.

## The problem

Canopy has **three half-overlapping representations of "a thing that needs addressing", and none of them is the object.**

1. **`NeedsYouItem`** (`apps/agents/services.py`, `_task_item`) — a read-only *projection*: `{type, ref_kind, ref_id, title, subtitle, url, created_at}`, recomputed on every request from tasks, run gates, syncs, and work products. Already polymorphic via `ref_kind`. Carries **no decision and no dispatch** — you can look at it, then you must go elsewhere to act.
2. **`AgentTaskCommand`** (`apps/agents/models.py:246`) — the human's action. Already has `kind="dispatch"`. But it only hangs off an `AgentTask`, and its `payload` is a freeform `JSONField` ("reason / assignee / next_action …").
3. **`cluster.dispatch[]`** (`packages/canopy_runner/canopy_runner/reviews.py`) — `{target_agent, prompt, origin, origin_ref}`. The **only typed dispatch spec in the system**, and it lives in an untyped JSON blob, inside a *product* app's review payload, drained by a runner that hard-codes `INGESTIBLE_GATE = "product_findings"`.

Each was reasonable alone. Together they mean a supervisor's queue has no state, decisions live in whatever produced them, and the one place that knows how to spawn work from a decision is a string-keyed dict inside a review.

**The phantom narrative was the symptom.** Ada's fleet audit needed a decision UI. The only one that existed was the DDD narrative review surface, so the audit borrowed `gate="product_findings"` — and a review of the fleet appeared in the DDD rail under a narrative conjured out of its `run_id`: active, empty, unnavigable (fixed in #213). That fix was correct and it treated a symptom. **The audit borrowed the review surface because the object it needed did not exist.**

## The thesis

There are exactly two kinds of thing, and they are duals.

- **`Turn` — work an agent does.** Already exists and is already right (`apps/harness/models.py:67`): `agent`, `origin`, `origin_ref`, `prompt`, `idempotency_key`, `routing`, `status`. **Do not touch it.**
- **`Item` — work *you* do.** The supervisor's queue. Same shape, opposite side of the loop.

They form a cycle:

```
  Turn ──raises──▶ Item ──you decide──▶ dispatch ──▶ Turn ──raises──▶ …
 (agent work)   (your queue)          (TurnSpec)   (agent work)
```

An agent runs a turn; the turn raises items; you decide; decisions spawn turns. **Ada's fan-out is that same edge with `target_agent != self`.** It is a parameter, not a code path. Normally an agent dispatches to itself.

**`dispatch[]` was never a new concept — it is a deferred `Turn` enqueue.** Compare what already exists:

| Ada's `cluster.dispatch[]` | mobile Phase 3 composer | `harness.Turn` |
|---|---|---|
| `target_agent` | `agent_slug` | `agent` FK |
| `prompt` | `prompt` | `prompt` |
| `origin` | `origin` | `origin` |
| `origin_ref` | — | `origin_ref` JSON |

Three spellings of one payload. The runner already collapses them by hand (`reviews.py:73` → `client.enqueue_turn(...)`). This design gives them one name.

## The test this design has to pass

**It must delete concepts, not add one.** A fourth representation of "a thing needing address" would be the exact failure the canopy-mobile spec names when it refuses a second command catalog: *"A second catalog would be a second thing to keep in sync — reintroducing the failure the current design prevents."* The ledger in §4 is how this design answers that charge.

### Independent corroboration (PR #218, in flight)

`feat(harness): agent scheduled turns` was designed with no knowledge of this spec and reached two of its conclusions unprompted:

- *"**No occurrence table.** The `Turn` **is** the occurrence... canopy-web already has three half-overlapping representations of 'a thing that needs addressing'; an occurrence row would be a fourth."* — same count, same framing, same refusal.
- It splits its router into `api_schedules.py` rather than growing `api.py`, which is the same move as `items_api.py`.

It also demonstrates the cost of the object not existing yet. Its **nag** — "you never finished the session the run spawned" — is textbook Item (an agent needs something from you, and there is work to spawn if you say so). With no Item to raise, it ships as *"a projection, not an object — a source inside `needs_you()`"*, adding a **fourth producer** to the projection and a fourth `ref_kind` (`"schedule"`) to `NeedsYouItem`'s `Literal`. That is not a criticism of #218; given what exists, it is the correct local choice. It is evidence that the projection accretes a new source every time someone needs the supervisor's attention, and that each one must be hand-wired into `needs_you` **and** into `apps/push`'s receivers (§5b).

**Two independent features in one week both needed this object.** Neither could have it.

## Scope

**In:** the `Item` model; the typed `TurnSpec`; the `Turn`↔`Item` edge; migrating all three producers (findings clusters, run gates, suggested tasks) onto Items; rewriting `needs_you` as a query; the runner draining items instead of polling reviews; the findings review page becoming a batch view over Items.

**Out:** the Phase 3 composer (it stays exactly as designed — see §8); `AgentTurn` (the packaged turn *report* — a different object with a confusingly similar name); the DDD narrative review surface (`concept_change` / `external_release` remain narrative gates on `apps/reviews` and are untouched).

## Design

### 1. `Item` — the model

Framework tier. Lives in `apps/harness` beside `Turn`, because the two are one cycle and reference each other; splitting them across apps would put a FK across an app boundary for no gain.

**On the import direction — verified, and narrower than it looks.** `apps.harness.models` imports `apps.agents.models` (`Turn.agent`). The constraint that follows is only this: **`apps.agents.models` must not import `apps.harness`.** It does not today, and `apps.agents.services` / `apps.agents.api` importing `apps.harness.models` is fine — confirmed by importing both under `django.setup()`.

Do **not** cargo-cult the lazy import in `_run_inbox_items`. Its docstring justifies itself with a cycle (*"agent_runs imports apps.agents.models — eager import would cycle"*), but the actual hazard there is narrower too; copying the pattern into `needs_you` for `Item` would add indirection for a cycle that does not exist. Import normally; if a cycle ever appears, it will be an `apps.agents.models` import and the fix is to move it, not to defer it.

Both apps are framework tier, so `tests/test_architecture_boundary.py` has nothing to say here either way.

```
Item
  id             UUID
  agent          FK agents.Agent      # whose queue this belongs to
  workspace      FK (via agent)       # tenancy rides the agent, as Turn's does

  origin         str                  # run | email | audit | board | api
  origin_ref     JSON                 # provenance: evidence, deep links, thread ids
  raised_by      FK Turn NULL         # the turn that produced it (closes the cycle)

  kind           str                  # review | question
  title          str                  # its own words — see below
  body           str                  # the ask + evidence, markdown

  state          str                  # open | decided | dismissed
  decision       str                  # kind=review only: implement | skip | defer. CLOSED SET.
  comment        str                  # kind=review: the reviewer's note (optional)
                                      # kind=question: the answer (required to decide)
  decided_by     str                  # email
  decided_at     dt

  dispatch       JSON [TurnSpec]      # what to enqueue on approve
  batch_key      str                  # groups items reviewed in one sitting
  idempotency_key str UNIQUE          # producers re-post safely
```

**The Item carries its own text, and this is the load-bearing decision.** It is not a mirror of a subject living elsewhere; it is an utterance at a moment — *"hal: discard 81 junk/stale unread emails"* — like an email, which never re-reads the thing it describes. `origin_ref` is **provenance, not identity**: it deep-links to the evidence, it is not a foreign key the renderer resolves.

Three things follow, and they are the whole reason this model is simple:

- **No registry.** Nothing resolves `ref_kind` → a product module. The row renders from itself.
- **No drift.** There is no second copy to fall out of sync, because there is no first copy — the Item *is* the content.
- **No framework→product import.** `apps/harness` never learns what a review is. Product apps *create* Items (product→framework, always allowed).

If the subject changes materially, the producer dismisses the Item and raises a new one. It owns that transition, because it is the only thing that knows the subject changed.

**The decision vocabulary is closed, and the two kinds resolve differently.** A `review` is decided with `implement | skip | defer` — three buttons the UI can render for any Item without asking its producer what its verbs are, which is precisely what a generic inbox requires. A `question` is resolved by a non-empty `comment` (the answer); its `decision` stays blank. Producers do **not** define their own verbs: a producer-defined vocabulary would mean the inbox cannot render an Item it has never seen, which defeats the object. A producer that needs richer choices expresses them as several Items or in `body`.

**A commitment dispatches; a decline does not.** On a `review` that is `implement`; on a `question` it is the answer itself, because a question has no verb to click — typing an answer IS the commitment. `skip` and `defer` decide the Item and enqueue nothing — the difference is that `defer` is a signal to the producer to raise it again later, which the producer honours (or does not) on its own schedule. An answered `question` with no `dispatch[]` likewise enqueues nothing; there is nothing to route.

*Revised 2026-07-30.* Answering was originally inert — the Item left the open queue carrying the human's instruction and nothing enqueued it, so the work depended on an agent happening to re-read decided rows. Nothing did: three cards answered on 2026-07-30 produced zero turns, and the queue the producer surveys (`?state=open`) no longer contained them, so the answers were not merely delayed but invisible. The reply now also rides into every dispatched prompt (`dispatch._with_reply`) — an answer is where the *steer* lives ("yes, but send it to hal instead"), and dispatching the pre-written brief without it would run work the human already amended. This does mean a non-committal answer ("what happens if I click this?") fires the card; the preamble tells the agent the reply overrides the brief and to report back rather than execute when it isn't an approval.

### 2. `TurnSpec` — the typed dispatch

```python
@dataclass(frozen=True)
class TurnSpec:
    prompt: str
    target_agent: str = ""      # "" means SELF — the Item's own agent
    origin: str = "api"
    origin_ref: dict = field(default_factory=dict)
    routing: str = Turn.PREFER_LOCAL
```

One function turns a decided Item into work:

```python
def dispatch(item: Item) -> list[Turn]:
    """Enqueue an approved Item's work. Idempotent per (item, index)."""
```

`target_agent=""` → the Item's own agent. **Self-dispatch is the default and needs no ceremony**; Ada's cross-agent fan-out is the same call with the field set. The idempotency key is `item-{item.id}-{i}`, replacing the runner's hand-rolled `review-{rid}-{cid}-{i}` and preserving its guarantee: re-draining never double-enqueues.

**The `TASK_NOT_FOUND` rule is inherited verbatim, not weakened.** `execute.py:66` permits *only* `TASK_NOT_FOUND` to fall through from reuse to create; any other send failure fails the turn rather than duplicating a session. That rule exists because it once spawned two Hal sessions. Item-driven dispatch must not relax it.

### 3. The cycle

`Item.raised_by → Turn` and `Turn.raised_from → Item` (both nullable — an Item can come from an email poll with no turn behind it; a Turn can come from the composer with no Item).

This is not bookkeeping. It makes the loop **traceable**: *Ada's audit turn → 7 items → you approved 3 → 3 turns on hal and eva → each raised its own items.* Today that history exists only as log lines in the runner.

### 4. The ledger — what this deletes

| Deleted | Becomes |
|---|---|
| `NeedsYouItem` projection (`_task_item`, `_run_inbox_items`) | `Item.objects.filter(agent=…, state="open")` — a query |
| `cluster.dispatch[]` untyped JSON | `Item.dispatch: [TurnSpec]`, typed |
| runner `INGESTIBLE_GATE` + resolved-review polling (`reviews.py`) | drains decided Items; stops knowing what a "review" is |
| `AgentTask.SUGGESTED` | an Item whose approval *creates* the task (§6) |
| `gate="product_findings"` | an Item batch (§7) |
| the source registry for the inbox | never built (§1) |
| `notify` band in `needs_you` | `/timeline`, which already is this (§5) |
| `apps/push`'s three hand-wired receivers | one receiver on `Item` — and its Drive-backed-agent gap stops being expressible (§5b) |
| #218's schedule-nag projection (`ref_kind="schedule"`) | an Item, once it exists (Phase 3) |

Net: one new model, eight concepts retired. Three objects survive with non-overlapping roles — **`Item`** = a decision, **`Turn`** = an execution, **`AgentTask`** = tracked work that outlives a turn.

### 5. `notify` is not an item — it is the timeline

`needs_you` today emits three bands, but the code already concedes the split:

```python
items = review + question
waiting_count = len(items)   # review + question are the gated items
```

A posted sync or a shipped work product asks nothing of you. It is **activity**, and canopy-web already has an activity feed built for exactly this, with a source registry and cursor pagination: `/timeline` (`apps/timeline`, framework tier). The `notify` band is a third rendering of it, capped at 5 and stapled to the inbox.

**Decision:** `Item.kind ∈ {review, question}` only. The supervisor surface composes its FYI band from the timeline, deleting the sync/work-product/completed-run loops from `needs_you`.

**This costs one small addition, and the spec is explicit about it rather than assuming:** `GET /api/timeline/` today filters by `subsystem` only (`apps/timeline/api.py:49`) — there is **no `agent` filter**. Phase 2 adds one, which means `apps.agents.timeline.recent_events` gains an optional `agent` kwarg via the same opt-in seam `workspace_slugs` already uses (`_call_source` passes a kwarg only if the source accepts it, `sources.py:52`). That seam exists precisely so a source can narrow itself without the aggregator learning its models — use it; do not add a bare `agent` param to every source.

This is the one place this design touches a surface that shipped days ago (#212, `WaitingOnYou.tsx` renders three bands from `RANK`). It is called out rather than buried because it is a real cost and a reviewer should weigh it: the alternative is keeping a notification concept inside an object whose entire purpose is "needs a decision".

### 5b. What `apps/push` proves (added after #216 shipped)

`apps/push` (PR #216) sends a Web Push when an agent's `waiting_count` rises. Because `needs_you()` is an aggregation and not an event, nothing emits "the fleet needs you now" — so push hand-wires `post_save`/`post_delete` receivers to **each producer individually**: `AgentTask`, `AgentRunGate`, `AgentRunStep` (`apps/push/signals.py`). Exactly the three producers §4 retires.

Two consequences, and the second is the point:

- **Phase 2 must re-point push at `Item`.** Not optional: with `needs_you` reading Items, the old receivers stop tracking what the badge shows. One receiver on one model replaces three.
- **It closes push's documented gap.** CLAUDE.md records it: *"an agent listed in `AGENT_RUNS_DRIVE_ROOTS` (Drive-backed run store) has no DB rows for its run gates, so no signal fires and its gate-opens don't push."* That gap exists **because the inbox is a projection over things that may not be rows.** An Item is always a row. The gap does not get fixed — it stops being expressible.

This is the strongest available evidence for the whole design, and it arrived independently: a feature built with no knowledge of this spec had to enumerate the producers by hand, and shipped a known hole because one of them isn't in the database.

### 6. `AgentTask.SUGGESTED` becomes an Item

A suggested task is not a task. It is a **proposal about creating one** — which is why `needs_you` has to special-case it into the review band, and why the board renders a column for work nobody agreed to yet.

- Agent proposes → `Item(kind="review", title="Do X", dispatch=[TurnSpec(prompt="/echo:do-x")])`.
- You approve → the `AgentTask` is created (status `IN_PROGRESS`) **and** the dispatch fires.
- You decline → the Item is `dismissed`. No task is ever created.

`AgentTask.STATUS_CHOICES` loses `SUGGESTED`, keeping `IN_PROGRESS | DONE | DECLINED`. `AgentTaskCommand` keeps `accept`/`decline` for *existing* tasks; `dispatch` as a command kind is retired in favour of `Item.dispatch`.

**Note for the implementer:** `AgentTask`'s docstring claims *"Source of truth is a Google Sheet the agent maintains"*. That is stale — CLAUDE.md and the board's command loop make the DB the source of truth. Fix the docstring while you are in there; do not design around the sheet.

### 7. `product_findings` dissolves; the review page becomes a batch view

Ada stops posting a review. She posts Items with a shared `batch_key`:

```
POST /api/agents/ada/items/
  [{kind: "review", title: "hal: discard 81 junk/stale unread emails",
    body: "All 81 are automated or older than 1 week…",
    origin: "audit", origin_ref: {…evidence…}, batch_key: "fleet-audit-2026-07-14",
    dispatch: [{target_agent: "hal", prompt: "/hal:turn --thread <id>", origin: "email"}]}]
```

The findings page becomes `/w/:ws/agents/:slug/items?batch=<key>` — a batch view over Items, rendering the same one-sitting review UX (`ProductFindingsReview` is already a standalone component, branched before the narrative editor at `ReviewPage.tsx:1752`; it is re-pointed at Items, not rewritten).

**`apps/reviews` returns to being exactly what its name says**: the DDD narrative review surface (`concept_change`, `external_release`). `RUN_CHILD_GATES`, `is_run_child_gate`, the attach-but-never-create rule in `apps/runs/aggregate.py`, and the nullable `narrative_slug` all become dead code and are removed with the migration. **#213 stops being load-bearing and gets deleted** — the correct outcome for a fix to a symptom.

### 8. Reconciliation with canopy-mobile Phase 3

They compose; neither blocks the other.

- **The composer stays exactly as designed.** Tapping a `launchable` skill enqueues `POST /api/harness/turns/` with `prompt: "/{slug}:{skill} {args}"`. That is **human-initiated** work and needs no Item — there is no proposal and no decision, just a person asking for a thing. Do not route it through an Item.
- **Items make Phase 3's other half trivial.** `WaitingOnYou.tsx` says *"Read-only for now: rows link out. Acting on an item inline is Phase 3, with the composer."* With Items, acting inline is `POST /api/items/{id}/decide` — approving **is** the dispatch. No composer special-case, no per-producer action path.

The rule: **human-initiated work → Turn directly. Agent-initiated ask → Item → decision → Turn.** Both terminate in a Turn, which is why `Turn` is the thing that must not change.

### 9. Tenancy and authorization

Items ride the agent, exactly as `Turn` does: `item.agent.workspace`. The fleet inbox scopes through `_visible_agent_workspace_ids` (`apps/agents/api.py:42`) — the single definition #212 introduced after the predicate was hand-copied three times. **Build from it; do not re-derive it.** Deciding an Item requires membership of its agent's workspace; a non-member gets 404, not 403 (no existence leak), matching `apps/harness`'s authz (`tests/test_harness_authz.py`).

## API

```
GET    /api/agents/{slug}/items/          list (filters: state, kind, batch)
POST   /api/agents/{slug}/items/          create (batch; idempotent per idempotency_key)
GET    /api/items/{id}/                   detail
POST   /api/items/{id}/decide             {decision, comment} → dispatches on approve
POST   /api/items/{id}/dismiss
GET    /api/agents/items/                 fleet-wide open items (the supervisor home)
```

`GET /api/agents/{slug}/needs-you` and `GET /api/agents/needs-you` (#212) are **re-implemented over Items and keep their response schema** — typed items + `waiting_count` — so `/supervisor`, `WaitingOnYou.tsx`, and the menubar panel keep compiling against the same contract.

Their **content** changes in one way, and it is not a silent one: no Item is `kind="notify"`, so that band returns empty and `WaitingOnYou`'s `RANK` loop renders nothing for it (`band.length === 0 → return null` — already handled). The supervisor surface then grows an FYI section fed by `/api/timeline/?agent=<slug>`. Do **not** leave the notify band as a permanently-empty affordance: delete it from `RANK` in the same change that adds the timeline section, or the surface reads as broken rather than reorganised.

## Phases

| # | Phase | Rationale |
|---|---|---|
| **0** | `Item` + `TurnSpec` + `dispatch()` + the `Turn`↔`Item` edge. No producer migrated. | The model, provable in isolation. |
| **1** | Ada's findings → Items. Runner drains items (`reviews.py` deleted). Batch view. | The live pain, and it retires `product_findings` + #213. |
| **2** | `needs_you` **also** reads Items; **`apps/push` also watches `Item`** (§5b). | The read path, once there are real Items to read. Push is not optional here — leaving its receivers on the old producers would silently stop the badge and the phone agreeing. |
| **2b** | `notify` → timeline (§5). | Separable from 2a and touches a surface that shipped days ago (#212's `WaitingOnYou`), so it gets its own decision rather than riding along. |
| **3** | Run gates → Items; **#218's schedule nag → an Item** (dropping `ref_kind="schedule"`). | Mechanical once §2 lands. |
| **4** | `AgentTask.SUGGESTED` → Items; board loses the column. | Last, because it touches the surface Echo/ACE use daily. |

Phase 1 before 2 is deliberate: it puts real Items in the table before anything reads from them, so the read path is written against reality rather than fixtures.

**Correction, found while executing Phase 2 (and exactly the cost this phasing predicted would only be legible then): Phase 2 must be ADDITIVE.** The table above originally said "`needs_you` re-implemented over Items", which would have **emptied the inbox**: `needs_you` has six producers (suggested tasks, blocked tasks, run gates, #218's schedule nag, syncs, work products), and tasks do not migrate until Phase 4 and run gates until Phase 3. So `needs_you` reads Items *alongside* its projections, and each projection deletes itself as its producer moves. `needs_you` becomes the pure query §4 promises only after Phase 4 — not after Phase 2.

**One plan per phase, not one plan for this spec.** Phases 0+1 together are a coherent first plan — the model plus its first real producer. Phases 2–4 each get their own plan, written after the phase before it has shipped, because each one's real cost (what breaks in `needs_you`, what the board loses) is only legible once Items exist.

**Phase 1 spans two repos, and the retirement is gated on the far one.** Ada lives in her own repo (`~/emdash/repositories/ada`), so "Ada emits Items" is not a canopy-web change. The order is forced:

1. **canopy-web** ships the Items API + item draining, **additively** — `product_findings` keeps working, the runner drains both.
2. **ada** switches her audit to post Items.
3. **canopy-web** retires `product_findings`, deletes the runner's `reviews.py`, and deletes #213's `RUN_CHILD_GATES` / attach-but-never-create / nullable-`narrative_slug` code.

Step 3 is a **separate PR gated on step 2**, not a task in the Phase 1 plan. Deleting the old path before Ada has moved would strand her next audit with no decision surface at all — the precise failure this spec exists to end.

## Verification

- A decided Item enqueues exactly one Turn per dispatch entry; re-draining enqueues **none** (idempotency key `item-{id}-{i}`).
- `target_agent=""` dispatches to the Item's own agent; a set `target_agent` dispatches to that agent — **same call, no branch**.
- An Item whose `dispatch` is empty is decidable and enqueues nothing (a decision can be a no-op — "skip" must not require a spec).
- A non-member gets **404** from list/detail/decide for another workspace's Item.
- Deciding an already-decided Item is a **409**, not a second dispatch.
- A dismissed Item never dispatches, even if `decision` was previously set.
- The `TASK_NOT_FOUND` rule survives: a non-`TASK_NOT_FOUND` send failure fails the turn and creates **no** second session.
- Phase 1: a fleet-audit batch renders in one page; approving 3 of 7 enqueues 3 turns on the named agents; `apps/reviews` is never touched.
- Phase 4: no `AgentTask` is created by a declined proposal.

## Risks

**This rewrites surfaces that work.** `needs_you`, the board, the runner's ingestion, and Ada's emitting skill all change. The phasing is the mitigation: each phase is independently shippable and independently revertible, and nothing reads Items until Phase 2.

**`Item` is a generic name in a codebase that already overloads `Turn`** (`harness.Turn` = the execution envelope; `agents.AgentTurn` = the packaged report; `related_name="harness_turns"` exists solely because of that collision). It also already overloads **`notify`**: the `needs_you` band (retired by §5) and, since #218, `apps/harness/notify.py`'s push-**channel** registry — same word, unrelated concept. And `NeedsYouItem` is itself called an "item" today, which this model replaces rather than renames. If `Item` proves too thin a word in review, the fallback is `Ask` — but do not repeat the `Turn` mistake and pick a name whose plural already means something else here.

**#218 lands in the same four files** (`apps/harness/models.py`, `services.py`, `schemas.py`, `api.py`). Whichever merges second rebases, and the harness migration numbers will move. Neither design blocks the other: #218 adds `Turn.MISSED` and a schedule model; this adds `Item` and one nullable FK.

**A bad dispatch spec must not strand an approved item — and the answer is atomicity, not retries.** If a spec names an agent that does not exist, `dispatch()` raises. Because deciding is once-only (409), committing the decision before dispatching would leave the item DECIDED-but-undispatched and permanently unfixable: approved in the UI, work never enqueued. So **decide + dispatch are one transaction** — a bad spec is a 422 on an item that is still `open`, retryable the moment the producer fixes it.

This deliberately does *not* reproduce the runner's old "leave it unprocessed and retry every poll" behaviour, which **PR #219 removed** from `reviews.py` as warning spam, with reasoning that applies here verbatim: *"a resolved review's decisions are immutable, and Ada re-emits fixes as a NEW review id — so retrying this one forever can never succeed."* An immutable decision cannot be rescued by retrying it. Roll it back instead, and there is nothing to retry and nothing to reconcile — which is why this design needs no runner-side item drain at all.

## Deferred (recorded so it is not relitigated)

- **Items raised for *another human*.** `Item.agent` is the asking agent; the decider is any workspace member. Per-person routing ("this one is Beth's") is a real need later and is deliberately not modelled now — `decided_by` records who acted, which is enough to learn from before designing assignment.
- **Item → Item edges.** A decision that raises another decision (rather than a Turn) is expressible as `dispatch → turn → item`, which is one hop longer but keeps the cycle two-object. Revisit only if that hop proves to be a fiction.
