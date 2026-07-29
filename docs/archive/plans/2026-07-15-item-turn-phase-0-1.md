# Item ⊕ Turn — Phases 0+1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `Item` — the first-class "thing that needs addressing" — and make Ada's fleet audit its first real producer, so approving an item dispatches work to the right agent.

**Architecture:** Two duals in one cycle. `Turn` (exists, untouched) is work an agent does; `Item` (new, `apps/harness`) is work *you* do. A turn raises items → you decide → an approved item's `dispatch` enqueues turns. `target_agent=""` means self; Ada's fan-out is the same field set to another agent. `dispatch()` wraps the existing idempotent `services.enqueue_turn`.

**Tech Stack:** Django 5 + Django Ninja 1.x + Pydantic v2 + PostgreSQL; React 19 + Vite + Tailwind 4; pytest; Playwright. (No `canopy_runner` changes — see File Structure.)

**Spec:** `docs/superpowers/specs/2026-07-15-item-and-turn-design.md`

## Global Constraints

- **Do not modify `harness.Turn`'s existing fields, statuses, or `one_executing_turn_per_agent`.** The only change to `Turn` is one new nullable FK (`raised_from`).
- **`Item` lives in `apps/harness`** (framework tier), beside `Turn`. `apps.agents.models` must never import `apps.harness`; `apps.agents.services`/`api` importing `apps.harness.models` is fine and needs no lazy import.
- **Decision vocabulary is a CLOSED set:** `implement | skip | defer`. Only `implement` dispatches. Producers do not define their own verbs.
- **`Item.kind ∈ {review, question}`.** There is no `notify` item — that is the timeline (Phase 2, not this plan).
- **Non-member → 404, never 403** (no existence leak). Mirrors `apps/harness` authz; see `tests/test_harness_authz.py`.
- **Idempotency:** items are idempotent per `idempotency_key`; dispatch is idempotent per `item-{item.id}-{i}`.
- **Additive only.** `gate="product_findings"`, `apps/reviews`, and the runner's `reviews.py` all keep working, untouched. Retirement is a separate PR gated on Ada (see "Out of scope").
- **Design tokens only** in frontend (`bg-card`, `text-muted-foreground`, …). No raw palette literals (`stone-*`, `orange-*`, …).
- **Regenerate types** after any schema change: dump the schema in-process (see Task 5, Step 6), then `npx openapi-typescript`. Do not hand-edit `frontend/src/api/generated.ts`.
- Run backend tests with `uv run pytest`. If you hit `ModuleNotFoundError`, run `uv sync --extra dev` first.

## Out of scope (do not do these here)

- **Retiring `product_findings`.** Gated on Ada's repo (`~/emdash/repositories/ada`) switching to Items. Deleting the old path first would strand her next audit with no decision surface. That includes `RUN_CHILD_GATES`, the attach-but-never-create rule, nullable `narrative_slug` (PR #213), and the runner's `reviews.py` — all of which stay until then.
- **`needs_you` over Items / `notify` → timeline** (Phase 2).
- **Run gates → Items** (Phase 3); **`AgentTask.SUGGESTED` → Items** (Phase 4).
- **The mobile Phase 3 composer.** Human-initiated work enqueues a Turn directly and needs no Item.

## File Structure

| File | Responsibility |
|---|---|
| `apps/harness/models.py` (modify) | Add `Item`; add `Turn.raised_from` |
| `apps/harness/migrations/000N_item.py` (create) | Schema for both — **N is the next free number**, see Task 1 |
| `apps/harness/dispatch.py` (create) | `TurnSpec` + `dispatch()` — the decision→work edge |
| `apps/harness/services.py` (modify) | `create_items()`, `decide_item()`, `dismiss_item()` |
| `apps/harness/schemas.py` (modify) | `ItemIn`, `ItemOut`, `ItemDecideIn`, `TurnSpecIn` |
| `apps/harness/items_api.py` (create) | The two item routers (kept out of `api.py`) |
| `apps/api/api.py` (modify) | Mount the item routers |
| `frontend/src/api/client.v2.ts` (modify) | Add `/api/items` to `WS_SCOPED_API_PREFIXES` |
| `frontend/src/api/items.ts` (create) | Typed client |
| `frontend/src/pages/agents/ItemsBatchSection.tsx` (create) | The batch view |

**No runner changes.** An earlier draft had the runner drain decided-but-undispatched items. `decide_item` is atomic (Task 3), so that state cannot exist — and the drain would have re-introduced exactly the retry-forever warning spam that #219 removed from `reviews.py`. There is nothing for the runner to reconcile.

## Coordinate with PR #218 (open — `feat(harness): agent scheduled turns`)

It touches **the same four files**: `apps/harness/models.py`, `services.py`, `schemas.py`, `api.py`, plus `apps/api/api.py`. Whichever lands second rebases.

- **Migrations collide.** #218 adds `0004_turn_missed` + `0005_agentschedule`, but main already has `0004_runner_workspace` + `0005_backfill_runner_workspace`, so #218 renumbers to `0006`/`0007`. **Do not hardcode a migration number** — run `makemigrations` and take what it gives you.
- **It adds `Turn.MISSED`**, a new terminal status. That is #218's change, not ours, and it does not conflict: our only `Turn` change stays the one nullable FK.
- **`apps/harness/notify.py` (#218) is a push-channel registry** — "notify" there means *delivery channel*, not the `notify` inbox band this spec retires. Same word, different concept. Do not merge them.
- **`api_schedules.py` (#218) is the same split-the-router pattern** as our `items_api.py`. Follow its conventions where they differ from ours; convergent evidence that the split is right.

---

## Phase 0 — the model

### Task 1: `Item` model + `Turn.raised_from`

**Files:**
- Modify: `apps/harness/models.py`
- Create: `apps/harness/migrations/000N_item.py` (N = next free; see Step 4)
- Test: `tests/test_item_models.py`

**Interfaces:**
- Consumes: `harness.Turn`, `agents.Agent`
- Produces: `Item` with `.agent`, `.kind`, `.state`, `.decision`, `.dispatch`, `.batch_key`, `.idempotency_key`, `.raised_by`; `Item.REVIEW`/`QUESTION`, `Item.OPEN`/`DECIDED`/`DISMISSED`, `Item.IMPLEMENT`/`SKIP`/`DEFER`; `Turn.raised_from`

- [ ] **Step 1: Write the failing test**

Create `tests/test_item_models.py`:

```python
"""Item model — the supervisor's queue. Dual of harness.Turn."""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent():
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def test_item_defaults_to_open_review_with_no_decision(agent):
    item = Item.objects.create(
        agent=agent, kind=Item.REVIEW, title="discard 81 junk emails",
        idempotency_key="k1",
    )
    assert item.state == Item.OPEN
    assert item.decision == ""
    assert item.dispatch == []
    assert item.raised_by is None


def test_idempotency_key_is_unique(agent):
    Item.objects.create(agent=agent, kind=Item.REVIEW, title="a", idempotency_key="dupe")
    with pytest.raises(IntegrityError):
        Item.objects.create(agent=agent, kind=Item.REVIEW, title="b", idempotency_key="dupe")


def test_turn_records_the_item_it_came_from(agent):
    item = Item.objects.create(agent=agent, kind=Item.REVIEW, title="a", idempotency_key="k2")
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1", raised_from=item,
    )
    assert list(item.dispatched_turns.all()) == [turn]


def test_item_records_the_turn_that_raised_it(agent):
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t2")
    item = Item.objects.create(
        agent=agent, kind=Item.REVIEW, title="a", idempotency_key="k3", raised_by=turn,
    )
    assert list(turn.raised_items.all()) == [item]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_item_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Item' from 'apps.harness.models'`

- [ ] **Step 3: Add the model**

In `apps/harness/models.py`, add `raised_from` to `Turn` (after the `agent` FK, before `origin`):

```python
    # The Item whose approval enqueued this turn. Null for turns with no decision
    # behind them: the mobile composer (a human asking directly), cron, inbox polls.
    raised_from = models.ForeignKey(
        "Item", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatched_turns",
    )
```

Then append `Item` at the end of the file:

```python
class Item(models.Model):
    """A thing that needs addressing — the dual of Turn.

    Turn is work an agent does; Item is work YOU do. They form a cycle: a turn
    raises items, you decide them, and an approved item's `dispatch` enqueues
    turns. Ada's cross-agent fan-out is that same edge with TurnSpec.target_agent
    set; the default ("") is self-dispatch.

    The Item carries its OWN text. It is not a mirror of a subject living
    elsewhere — it is an utterance at a moment, like an email, which never
    re-reads the thing it describes. `origin_ref` is provenance (evidence, deep
    links), NOT identity: nothing resolves it to render this row. That is what
    keeps this model free of a source registry, of drift, and of any
    framework->product import.
    """

    REVIEW, QUESTION = "review", "question"
    KIND_CHOICES = [(REVIEW, "Review"), (QUESTION, "Question")]

    OPEN, DECIDED, DISMISSED = "open", "decided", "dismissed"
    STATE_CHOICES = [(OPEN, "Open"), (DECIDED, "Decided"), (DISMISSED, "Dismissed")]

    # CLOSED set. A generic inbox must be able to render three buttons for an Item
    # it has never seen; producer-defined verbs would make that impossible.
    # Only IMPLEMENT dispatches. DEFER decides the item and signals the producer to
    # raise it again later, on its own schedule.
    IMPLEMENT, SKIP, DEFER = "implement", "skip", "defer"
    DECISION_CHOICES = [(IMPLEMENT, "Implement"), (SKIP, "Skip"), (DEFER, "Defer")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="items",
        help_text="Whose queue this belongs to — the agent ASKING, not the "
                  "dispatch target. Tenancy rides this FK, as Turn's does.",
    )
    raised_by = models.ForeignKey(
        Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="raised_items",
        help_text="The turn that produced this item. Null for items raised outside "
                  "a turn (an email poll, a manual post).",
    )

    origin = models.CharField(max_length=10, choices=Turn.ORIGIN_CHOICES)
    origin_ref = models.JSONField(default=dict, blank=True)

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=REVIEW)
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True, default="")

    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=OPEN)
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES, blank=True, default="")
    comment = models.TextField(
        blank=True, default="",
        help_text="kind=review: the reviewer's note (optional). "
                  "kind=question: the answer (required to decide).",
    )
    decided_by = models.CharField(max_length=200, blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)

    dispatch = models.JSONField(
        default=list, blank=True,
        help_text='[TurnSpec] — deferred Turn enqueues fired on implement. '
                  'e.g. [{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}]',
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)

    batch_key = models.CharField(
        max_length=120, blank=True, default="", db_index=True,
        help_text="Groups items reviewed in one sitting (e.g. a fleet audit).",
    )
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["agent", "state"]),
            models.Index(fields=["state", "created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"item:{self.agent.slug}:{self.kind}:{self.state}:{self.id.hex[:8]}"
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations harness --name item`
Expected: `Create model Item` + `Add field raised_from to turn`.

**Take whatever number it assigns — do not rename it to match this plan.** main is at `0005_backfill_runner_workspace`, and PR #218 is in flight adding two more harness migrations. If #218 lands first, yours moves up; that is normal and fine. Run `uv run python manage.py makemigrations --check` afterwards to confirm the tree is clean.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_item_models.py -q`
Expected: `4 passed`

- [ ] **Step 6: Run the full suite (nothing regressed)**

Run: `uv run pytest -q`
Expected: `595 passed, 1 skipped` (591 + the 4 new)

- [ ] **Step 7: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/ tests/test_item_models.py
git commit -m "feat(harness): Item — the dual of Turn, and the edge between them"
```

---

### Task 2: `TurnSpec` + `dispatch()`

**Files:**
- Create: `apps/harness/dispatch.py`
- Test: `tests/test_item_dispatch.py`

**Interfaces:**
- Consumes: `Item` (Task 1); `apps.harness.services.enqueue_turn(*, agent, origin, idempotency_key, prompt="", origin_ref=None, routing=Turn.PREFER_LOCAL) -> tuple[Turn, bool]`
- Produces: `TurnSpec(prompt, target_agent="", origin="api", origin_ref={}, routing="prefer_local")`, `TurnSpec.from_dict(d) -> TurnSpec`, `dispatch(item) -> list[Turn]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_item_dispatch.py`:

```python
"""dispatch() — an approved Item becomes work. Self by default; anyone on request."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness.dispatch import TurnSpec, dispatch
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def ws():
    return wsvc.ensure_default_workspace()


@pytest.fixture
def ada(ws):
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


@pytest.fixture
def hal(ws):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws)


def _item(agent, **kw):
    kw.setdefault("idempotency_key", f"k-{agent.slug}-{kw.get('title', 'x')}")
    kw.setdefault("kind", Item.REVIEW)
    kw.setdefault("title", "x")
    kw.setdefault("origin", Turn.ORIGIN_API)
    return Item.objects.create(agent=agent, **kw)


def test_empty_target_agent_dispatches_to_the_items_own_agent(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    turns = dispatch(item)

    assert [t.agent for t in turns] == [ada]
    assert turns[0].prompt == "/ada:conduct"
    assert turns[0].raised_from == item


def test_named_target_agent_dispatches_to_that_agent(ada, hal):
    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}])

    turns = dispatch(item)

    assert [t.agent for t in turns] == [hal]
    assert turns[0].origin == "email"


def test_dispatch_is_idempotent_per_entry(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    first = dispatch(item)
    second = dispatch(item)

    assert [t.id for t in first] == [t.id for t in second]
    assert Turn.objects.count() == 1


def test_an_item_with_no_dispatch_enqueues_nothing(ada):
    item = _item(ada, dispatch=[])

    assert dispatch(item) == []
    assert Turn.objects.count() == 0


def test_an_unknown_target_agent_raises_rather_than_silently_dropping(ada):
    item = _item(ada, dispatch=[{"target_agent": "ghost", "prompt": "/ghost:turn"}])

    with pytest.raises(ValueError, match="ghost"):
        dispatch(item)


def test_each_entry_gets_its_own_turn(ada, hal):
    item = _item(ada, dispatch=[
        {"target_agent": "hal", "prompt": "/hal:turn"},
        {"prompt": "/ada:conduct"},
    ])

    turns = dispatch(item)

    assert [t.agent for t in turns] == [hal, ada]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_item_dispatch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.harness.dispatch'`

- [ ] **Step 3: Write the implementation**

Create `apps/harness/dispatch.py`:

```python
"""The decision->work edge: an approved Item becomes Turns.

`dispatch[]` was never a new concept — it is a deferred Turn enqueue. Ada's
`{target_agent, prompt, origin, origin_ref}`, the mobile composer's
`{agent_slug, prompt, origin}`, and Turn are three spellings of one payload.
TurnSpec is that payload, named once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.agents.models import Agent

from . import services
from .models import Item, Turn


@dataclass(frozen=True)
class TurnSpec:
    """One deferred Turn enqueue.

    `target_agent=""` means SELF — the Item's own agent. Self-dispatch is the
    default and needs no ceremony; Ada's cross-agent fan-out is this same field
    set to another slug. A parameter, not a code path.
    """

    prompt: str
    target_agent: str = ""
    origin: str = Turn.ORIGIN_API
    origin_ref: dict = field(default_factory=dict)
    routing: str = Turn.PREFER_LOCAL

    @classmethod
    def from_dict(cls, d: dict) -> "TurnSpec":
        return cls(
            prompt=(d.get("prompt") or "").strip(),
            target_agent=(d.get("target_agent") or "").strip(),
            origin=(d.get("origin") or Turn.ORIGIN_API).strip(),
            origin_ref=d.get("origin_ref") or {},
            routing=(d.get("routing") or Turn.PREFER_LOCAL).strip(),
        )


def dispatch(item: Item) -> list[Turn]:
    """Enqueue an approved Item's work. Idempotent per (item, index).

    Raises ValueError for an unknown target_agent rather than skipping it: an
    approved item whose work silently never happens is the worst outcome here —
    the caller surfaces the error, it does not get swallowed.
    """
    turns: list[Turn] = []
    for i, raw in enumerate(item.dispatch or []):
        spec = TurnSpec.from_dict(raw)
        if spec.target_agent:
            target = Agent.objects.filter(slug=spec.target_agent).first()
            if target is None:
                raise ValueError(
                    f"item {item.id} dispatch[{i}]: unknown target_agent {spec.target_agent!r}"
                )
        else:
            target = item.agent
        turn, _created = services.enqueue_turn(
            agent=target,
            origin=spec.origin,
            idempotency_key=f"item-{item.id}-{i}",
            prompt=spec.prompt or f"/{target.slug}:turn",
            origin_ref=spec.origin_ref,
            routing=spec.routing,
        )
        if turn.raised_from_id is None:
            turn.raised_from = item
            turn.save(update_fields=["raised_from"])
        turns.append(turn)
    return turns
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_item_dispatch.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add apps/harness/dispatch.py tests/test_item_dispatch.py
git commit -m "feat(harness): TurnSpec + dispatch() — target_agent defaults to self"
```

---

### Task 3: `decide_item()` / `dismiss_item()` / `create_items()`

**Files:**
- Modify: `apps/harness/services.py`
- Test: `tests/test_item_services.py`

**Interfaces:**
- Consumes: `Item`, `dispatch()` (Tasks 1–2)
- Produces:
  - `create_items(*, agent, payloads: list[dict]) -> list[Item]` — idempotent per `idempotency_key`
  - `decide_item(item, *, decision: str, comment: str, by: str) -> tuple[Item, list[Turn]]`
  - `dismiss_item(item, *, by: str) -> Item`
  - `AlreadyDecided` (exception)

- [ ] **Step 1: Write the failing test**

Create `tests/test_item_services.py`:

```python
"""Item state machine: open -> decided (dispatching) | dismissed. One way only."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def ada():
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def _item(ada, **kw):
    kw.setdefault("idempotency_key", "k1")
    kw.setdefault("kind", Item.REVIEW)
    kw.setdefault("title", "x")
    kw.setdefault("origin", Turn.ORIGIN_API)
    return Item.objects.create(agent=ada, **kw)


def test_implement_decides_and_dispatches(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com")

    assert item.state == Item.DECIDED
    assert item.decision == Item.IMPLEMENT
    assert item.decided_by == "jj@dimagi.com"
    assert item.decided_at is not None
    assert item.dispatched_at is not None
    assert len(turns) == 1


def test_skip_decides_and_dispatches_nothing(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.SKIP, comment="", by="jj@dimagi.com")

    assert item.state == Item.DECIDED
    assert turns == []
    assert Turn.objects.count() == 0
    assert item.dispatched_at is None


def test_defer_decides_and_dispatches_nothing(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.DEFER, comment="", by="jj@dimagi.com")

    assert item.decision == Item.DEFER
    assert Turn.objects.count() == 0


def test_deciding_twice_raises_rather_than_dispatching_again(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])
    services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com")

    with pytest.raises(services.AlreadyDecided):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com")

    assert Turn.objects.count() == 1


def test_a_question_requires_an_answer(ada):
    item = _item(ada, kind=Item.QUESTION, title="which repo?")

    with pytest.raises(ValueError, match="answer"):
        services.decide_item(item, decision="", comment="", by="jj@dimagi.com")

    item, _ = services.decide_item(item, decision="", comment="canopy-web", by="jj@dimagi.com")
    assert item.state == Item.DECIDED
    assert item.comment == "canopy-web"


def test_a_review_rejects_a_decision_outside_the_closed_set(ada):
    item = _item(ada)

    with pytest.raises(ValueError, match="decision"):
        services.decide_item(item, decision="yolo", comment="", by="jj@dimagi.com")


def test_a_failing_dispatch_rolls_the_decision_back(ada):
    """A bad spec must NOT leave a decided-but-undispatched item. Deciding twice is
    409, so committing the decision before dispatch would strand the item forever:
    approved in the UI, work never enqueued, unfixable. It stays OPEN and retryable."""
    item = _item(ada, dispatch=[{"target_agent": "ghost", "prompt": "/ghost:turn"}])

    with pytest.raises(ValueError, match="ghost"):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com")

    item.refresh_from_db()
    assert item.state == Item.OPEN
    assert item.decided_at is None
    assert Turn.objects.count() == 0


def test_a_partly_bad_dispatch_enqueues_nothing(ada):
    """All-or-nothing: entry 0 must not survive entry 1 failing."""
    item = _item(ada, dispatch=[
        {"prompt": "/ada:conduct"},
        {"target_agent": "ghost", "prompt": "/ghost:turn"},
    ])

    with pytest.raises(ValueError):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com")

    item.refresh_from_db()
    assert item.state == Item.OPEN
    assert Turn.objects.count() == 0


def test_dismiss_never_dispatches_even_with_a_decision_set(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}], decision=Item.IMPLEMENT)

    item = services.dismiss_item(item, by="jj@dimagi.com")

    assert item.state == Item.DISMISSED
    assert Turn.objects.count() == 0


def test_create_items_is_idempotent_per_key(ada):
    payload = [{"kind": "review", "title": "a", "origin": "audit", "idempotency_key": "dupe"}]

    first = services.create_items(agent=ada, payloads=payload)
    second = services.create_items(agent=ada, payloads=payload)

    assert [i.id for i in first] == [i.id for i in second]
    assert Item.objects.count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_item_services.py -q`
Expected: FAIL — `AttributeError: module 'apps.harness.services' has no attribute 'decide_item'`

- [ ] **Step 3: Write the implementation**

Append to `apps/harness/services.py`. **No new imports needed** — it already has `IntegrityError`, `transaction` (`services.py:12`) and `timezone` (`services.py:14`); only the model import changes (see the end of this step).

```python
class AlreadyDecided(Exception):
    """An item can be decided once. A second decision is a conflict (409), not a
    second dispatch."""


def create_items(*, agent, payloads: list[dict]) -> list[Item]:
    """Create items for an agent, idempotent per idempotency_key. A producer that
    re-posts its batch (a retried audit) gets the same rows back, not duplicates."""
    out: list[Item] = []
    for p in payloads:
        key = p["idempotency_key"]
        existing = Item.objects.filter(idempotency_key=key).first()
        if existing is not None:
            out.append(existing)
            continue
        try:
            with transaction.atomic():
                out.append(Item.objects.create(
                    agent=agent,
                    kind=p.get("kind") or Item.REVIEW,
                    title=p["title"],
                    body=p.get("body") or "",
                    origin=p.get("origin") or Turn.ORIGIN_API,
                    origin_ref=p.get("origin_ref") or {},
                    dispatch=p.get("dispatch") or [],
                    batch_key=p.get("batch_key") or "",
                    idempotency_key=key,
                    raised_by_id=p.get("raised_by") or None,
                ))
        except IntegrityError:
            replay = Item.objects.filter(idempotency_key=key).first()
            if replay is None:
                raise
            out.append(replay)
    return out


def decide_item(item: Item, *, decision: str, comment: str, by: str) -> tuple[Item, list[Turn]]:
    """Resolve an open item. Only IMPLEMENT dispatches.

    A review needs a decision from the closed set; a question needs a non-empty
    answer (its `decision` stays blank). Deciding twice raises AlreadyDecided —
    the guard that stops a double-click becoming a second dispatch.
    """
    from .dispatch import dispatch as dispatch_item  # local: dispatch imports services

    if item.state != Item.OPEN:
        raise AlreadyDecided(f"item {item.id} is already {item.state}")

    if item.kind == Item.QUESTION:
        if not (comment or "").strip():
            raise ValueError("a question is resolved by its answer — comment must not be empty")
        decision = ""
    elif decision not in (Item.IMPLEMENT, Item.SKIP, Item.DEFER):
        raise ValueError(
            f"decision must be one of implement|skip|defer, got {decision!r}"
        )

    # ATOMIC, and this is the whole ballgame. dispatch() raises on a bad spec
    # (unknown target_agent). Committing the decision first would leave the item
    # DECIDED but undispatched — and since deciding twice is a 409, permanently
    # unfixable: the work silently never happens while the UI says you approved it.
    # That is the exact failure this design exists to end. Rolling back instead
    # means a bad spec is a 422 on an item that is still OPEN, retryable the moment
    # the producer fixes it.
    with transaction.atomic():
        item.state = Item.DECIDED
        item.decision = decision
        item.comment = comment or ""
        item.decided_by = by
        item.decided_at = timezone.now()

        turns: list[Turn] = []
        if decision == Item.IMPLEMENT:
            turns = dispatch_item(item)
            item.dispatched_at = timezone.now()

        item.save(update_fields=[
            "state", "decision", "comment", "decided_by", "decided_at", "dispatched_at",
        ])
    return item, turns


def dismiss_item(item: Item, *, by: str) -> Item:
    """Retire an item without acting. Never dispatches, whatever `decision` holds —
    a producer that raised it in error, or a subject that changed under it."""
    item.state = Item.DISMISSED
    item.decided_by = by
    item.decided_at = timezone.now()
    item.save(update_fields=["state", "decided_by", "decided_at"])
    return item
```

Update the model import at the top of `services.py` from `from .models import Turn` (or similar) to include `Item`:

```python
from .models import Item, Runner, SessionLink, Turn, TurnEvent
```

(Check the existing import line and add only what is missing — do not drop names it already imports.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_item_services.py -q`
Expected: `10 passed`

- [ ] **Step 5: Run the harness suite (nothing regressed)**

Run: `uv run pytest tests/test_harness_api.py tests/test_harness_models.py tests/test_harness_authz.py tests/test_item_dispatch.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add apps/harness/services.py tests/test_item_services.py
git commit -m "feat(harness): item state machine — decide once, implement dispatches"
```

---

### Task 4: Item schemas

**Files:**
- Modify: `apps/harness/schemas.py`
- Test: `tests/test_item_schemas.py`

**Interfaces:**
- Produces: `TurnSpecIn`, `ItemIn`, `ItemOut`, `ItemDecideIn`

- [ ] **Step 1: Write the failing test**

Create `tests/test_item_schemas.py`:

```python
"""Item schemas — the wire contract producers and the inbox share."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.harness.schemas import ItemDecideIn, ItemIn, TurnSpecIn


def test_turnspec_target_agent_defaults_to_self():
    spec = TurnSpecIn(prompt="/ada:conduct")
    assert spec.target_agent == ""


def test_item_requires_a_title_and_key():
    with pytest.raises(ValidationError):
        ItemIn(kind="review", origin="audit")


def test_item_rejects_a_notify_kind():
    """notify is not an item — it is the timeline."""
    with pytest.raises(ValidationError):
        ItemIn(kind="notify", title="a sync posted", origin="api", idempotency_key="k")


def test_decide_rejects_a_verb_outside_the_closed_set():
    with pytest.raises(ValidationError):
        ItemDecideIn(decision="yolo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_item_schemas.py -q`
Expected: FAIL — `ImportError: cannot import name 'ItemIn'`

- [ ] **Step 3: Write the schemas**

Append to `apps/harness/schemas.py` (match the file's existing base-class style — if it uses a `StrictModel`/`Schema` base, use that instead of `BaseModel`):

```python
class TurnSpecIn(Schema):
    """One deferred Turn enqueue. `target_agent=""` means the item's own agent."""

    prompt: str
    target_agent: str = ""
    origin: str = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    routing: str = "prefer_local"


class ItemIn(Schema):
    kind: Literal["review", "question"] = "review"
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    origin: str = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    dispatch: list[TurnSpecIn] = Field(default_factory=list)
    batch_key: str = ""
    idempotency_key: str = Field(min_length=1, max_length=128)
    raised_by: uuid.UUID | None = None


class ItemOut(Schema):
    id: uuid.UUID
    agent_slug: str
    # Echoed back so a producer can reconcile its batch against what landed, and so
    # the UI has a stable, human-readable key for test ids.
    idempotency_key: str
    kind: str
    title: str
    body: str
    origin: str
    origin_ref: dict[str, Any]
    state: str
    decision: str
    comment: str
    decided_by: str
    decided_at: dt.datetime | None = None
    dispatch: list[dict[str, Any]]
    dispatched_at: dt.datetime | None = None
    batch_key: str
    created_at: dt.datetime


class ItemDecideIn(Schema):
    # "" is valid for a question, whose answer is the comment.
    decision: Literal["implement", "skip", "defer", ""] = ""
    comment: str = ""
```

Ensure the imports at the top of `schemas.py` cover `uuid`, `datetime as dt`, `Any`, `Literal`, and `Field` — add only what is missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_item_schemas.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add apps/harness/schemas.py tests/test_item_schemas.py
git commit -m "feat(harness): item wire schemas — closed decision set, no notify kind"
```

---

### Task 5: Items API + mount + generated types

**Files:**
- Create: `apps/harness/items_api.py`
- Modify: `apps/api/api.py`
- Modify: `frontend/src/api/generated.ts` (generated — do not hand-edit)
- Test: `tests/test_items_api.py`

**Interfaces:**
- Consumes: `create_items`, `decide_item`, `dismiss_item`, `AlreadyDecided` (Task 3); `ItemIn`/`ItemOut`/`ItemDecideIn` (Task 4)
- Produces: `agent_items_router`, `items_router`
- Routes:
  - `GET  /api/agents/{slug}/items/` — `?state=`, `?kind=`, `?batch=`
  - `POST /api/agents/{slug}/items/` — batch create
  - `GET  /api/items/{id}/`
  - `POST /api/items/{id}/decide`
  - `POST /api/items/{id}/dismiss`

**`GET /api/agents/items/` (fleet-wide) is deliberately NOT here.** The spec lists it for the supervisor home, but nothing in Phases 0+1 consumes it — the batch view is per-agent. It ships in Phase 2 with `needs_you`.

Two traps for whoever adds it, both already paid for elsewhere in this codebase:
1. Declare it **before** `/{slug}/items/`, or `items` resolves as a slug — the trap `/agents/needs-you` navigates (`apps/agents/api.py`).
2. Scope it in **Python**, `workspace_id in visible`, exactly as `list_agents` does — **not** a `workspace_id__in=` queryset filter. `_visible_agent_workspace_ids` can contain `None` (the unhomed-agent case) and SQL `IN` never matches NULL, so the filter would hide unhomed agents' items while `list_agents` still lists their agents. That list-shows-it/action-404s-on-it split is the exact drift `_runner_visibility_q`'s docstring was written about.

- [ ] **Step 1: Write the failing test**

Create `tests/test_items_api.py`:

```python
"""Items API — authz (404 not 403), batch create, decide-once."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return User.objects.create_user(username="jj@dimagi.com", email="jj@dimagi.com")


@pytest.fixture
def outsider():
    return User.objects.create_user(username="nope@dimagi.com", email="nope@dimagi.com")


@pytest.fixture
def ada(member):
    ws = Workspace.objects.create(slug="tenant-a", display_name="A", created_by=member)
    WorkspaceMembership.objects.create(workspace=ws, user=member, role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


@pytest.fixture
def client_member(member):
    c = Client()
    c.force_login(member)
    return c


@pytest.fixture
def client_outsider(outsider):
    c = Client()
    c.force_login(outsider)
    return c


def _post_batch(client, slug="ada"):
    return client.post(
        f"/api/agents/{slug}/items/",
        data=[{
            "kind": "review",
            "title": "hal: discard 81 junk/stale unread emails",
            "body": "All 81 are automated or older than 1 week.",
            "origin": "audit",
            "batch_key": "fleet-audit-2026-07-14",
            "idempotency_key": "fa-hal-inbox",
            "dispatch": [{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}],
        }],
        content_type="application/json",
    )


def test_create_then_list_by_batch(client_member, ada):
    assert _post_batch(client_member).status_code == 201

    rows = client_member.get("/api/agents/ada/items/?batch=fleet-audit-2026-07-14").json()
    assert [r["title"] for r in rows] == ["hal: discard 81 junk/stale unread emails"]
    assert rows[0]["state"] == "open"


def test_create_is_idempotent(client_member, ada):
    _post_batch(client_member)
    _post_batch(client_member)
    assert Item.objects.count() == 1


def test_non_member_gets_404_not_403(client_outsider, ada):
    assert client_outsider.get("/api/agents/ada/items/").status_code == 404
    assert _post_batch(client_outsider).status_code == 404


def test_non_member_cannot_read_or_decide_an_item(client_member, client_outsider, ada):
    _post_batch(client_member)
    item_id = Item.objects.get().id

    assert client_outsider.get(f"/api/items/{item_id}/").status_code == 404
    assert client_outsider.post(
        f"/api/items/{item_id}/decide", data={"decision": "implement"},
        content_type="application/json",
    ).status_code == 404


def test_implement_dispatches_to_the_named_agent(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id

    resp = client_member.post(
        f"/api/items/{item_id}/decide",
        data={"decision": "implement", "comment": "do it"},
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "decided"
    turn = Turn.objects.get()
    assert turn.agent.slug == "hal"
    assert turn.prompt == "/hal:turn"


def test_deciding_twice_is_409(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id
    body = {"decision": "implement"}
    client_member.post(f"/api/items/{item_id}/decide", data=body, content_type="application/json")

    resp = client_member.post(
        f"/api/items/{item_id}/decide", data=body, content_type="application/json",
    )

    assert resp.status_code == 409
    assert Turn.objects.count() == 1


def test_dismiss_never_dispatches(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id

    resp = client_member.post(f"/api/items/{item_id}/dismiss", content_type="application/json")

    assert resp.status_code == 200
    assert resp.json()["state"] == "dismissed"
    assert Turn.objects.count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_items_api.py -q`
Expected: FAIL — 404s from unrouted URLs

- [ ] **Step 3: Write the API**

Create `apps/harness/items_api.py`:

```python
"""Django Ninja routers for Items — the supervisor's queue.

Kept out of api.py, which already owns the runner + turn lifecycle. Two routers
because the collection is agent-scoped (whose queue?) while the resource is not
(an item id is globally unique).

Authz mirrors apps/agents: an item is visible iff its agent is. A non-member gets
404, never 403 — no existence leak.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.api import _get_agent_or_404, _visible_agent_workspace_ids
from apps.api.auth import session_auth

from . import services
from .models import Item
from .schemas import ItemDecideIn, ItemIn, ItemOut

agent_items_router = Router(auth=session_auth, tags=["items"])
items_router = Router(auth=session_auth, tags=["items"])


def _payload(item: Item) -> dict:
    return {
        "id": item.id,
        "agent_slug": item.agent.slug,
        "idempotency_key": item.idempotency_key,
        "kind": item.kind,
        "title": item.title,
        "body": item.body,
        "origin": item.origin,
        "origin_ref": item.origin_ref,
        "state": item.state,
        "decision": item.decision,
        "comment": item.comment,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at,
        "dispatch": item.dispatch,
        "dispatched_at": item.dispatched_at,
        "batch_key": item.batch_key,
        "created_at": item.created_at,
    }


def _item_or_404(request: HttpRequest, item_id: uuid.UUID) -> Item:
    """An item is reachable iff its agent's workspace is visible to the caller.
    Built from _visible_agent_workspace_ids so this can never drift from the
    agents list — the failure that helper exists to prevent."""
    item = Item.objects.filter(pk=item_id).select_related("agent").first()
    if item is None or item.agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, "item not found")
    return item


@agent_items_router.get("/{slug}/items/", response=list[ItemOut], summary="List an agent's items",
                        openapi_extra={"x-mcp-expose": True})
def list_items(
    request: HttpRequest, slug: str, state: str = "", kind: str = "", batch: str = "",
) -> list[dict]:
    agent = _get_agent_or_404(request, slug)
    qs = agent.items.select_related("agent")
    if state:
        qs = qs.filter(state=state)
    if kind:
        qs = qs.filter(kind=kind)
    if batch:
        qs = qs.filter(batch_key=batch)
    return [_payload(i) for i in qs]


@agent_items_router.post("/{slug}/items/", response={201: list[ItemOut]},
                         summary="Raise items for an agent (batch, idempotent)",
                         openapi_extra={"x-mcp-expose": True})
def create_items(request: HttpRequest, slug: str, payload: list[ItemIn]):
    agent = _get_agent_or_404(request, slug)
    items = services.create_items(
        agent=agent, payloads=[p.dict() for p in payload],
    )
    return 201, [_payload(i) for i in items]


@items_router.get("/{item_id}/", response=ItemOut, summary="Get an item")
def get_item(request: HttpRequest, item_id: uuid.UUID) -> dict:
    return _payload(_item_or_404(request, item_id))


@items_router.post("/{item_id}/decide", response=ItemOut,
                   summary="Decide an item (implement dispatches its work)")
def decide_item(request: HttpRequest, item_id: uuid.UUID, payload: ItemDecideIn) -> dict:
    item = _item_or_404(request, item_id)
    try:
        item, _turns = services.decide_item(
            item, decision=payload.decision, comment=payload.comment,
            by=request.user.email or request.user.get_username(),
        )
    except services.AlreadyDecided as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return _payload(item)


@items_router.post("/{item_id}/dismiss", response=ItemOut, summary="Dismiss an item")
def dismiss_item(request: HttpRequest, item_id: uuid.UUID) -> dict:
    item = _item_or_404(request, item_id)
    return _payload(services.dismiss_item(
        item, by=request.user.email or request.user.get_username(),
    ))
```

- [ ] **Step 4: Mount the routers**

In `apps/api/api.py`, import and mount them. Find the existing `add_router` calls and add:

```python
from apps.harness.items_api import agent_items_router, items_router
...
api.add_router("/agents", agent_items_router)
api.add_router("/items", items_router)
```

Mount `agent_items_router` **after** the existing agents router so `/agents/needs-you` keeps resolving before `/{slug}/items/` — verify by running the agents tests in Step 5.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_items_api.py tests/test_agents_fleet_needs_you.py -q`
Expected: all pass (7 new + the fleet tests, proving the mount did not shadow `/agents/needs-you`)

- [ ] **Step 6: Regenerate the OpenAPI types**

```bash
uv run python -c "
import django, json, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
django.setup()
from apps.api.api import api
with open('frontend/openapi.json', 'w') as f:
    json.dump(api.get_openapi_schema(), f, indent=2)
print('schema dumped')
"
cd frontend && npx openapi-typescript openapi.json --output src/api/generated.ts --immutable && rm openapi.json
```

Expected: `generated.ts` gains `ItemOut` / `ItemIn` / `ItemDecideIn` paths.

- [ ] **Step 7: Full suite + commit**

Run: `uv run pytest -q`
Expected: all pass

```bash
git add apps/harness/items_api.py apps/api/api.py frontend/src/api/generated.ts tests/test_items_api.py
git commit -m "feat(harness): items API — 404 for non-members, 409 on re-decide"
```

---

## Phase 1 — Ada's findings as the first producer

### Task 6: The batch view

**Files:**
- Create: `frontend/src/api/items.ts`
- Create: `frontend/src/pages/agents/ItemsBatchSection.tsx`
- Modify: `frontend/src/router.tsx`
- Test: `frontend/e2e/items.spec.ts`, `frontend/e2e/seed.py`

**Interfaces:**
- Consumes: `GET /api/agents/{slug}/items/?batch=`, `POST /api/items/{id}/decide` (Task 5)
- Produces: route `/w/:workspace/agents/:slug/items?batch=<key>`

- [ ] **Step 1: Write the failing e2e test**

Add to `frontend/e2e/seed.py`, before the `session = SessionStore()` line (and add `from apps.harness.models import Item` to its imports):

```python
# A decided-nothing fleet-audit batch: two open items in Ada's queue, one of which
# dispatches to another agent. Mirrors what Ada's audit posts.
ada_agent, _ = Agent.objects.update_or_create(slug="ada", defaults=dict(
    name="Ada", email="ada@dimagi-ai.com", description="Fleet conductor.",
    persona="Conducts the fleet.", workspace=ws))
Item.objects.filter(agent=ada_agent).delete()
Item.objects.create(
    agent=ada_agent, kind="review", origin="audit", batch_key=FLEET_AUDIT_BATCH,
    idempotency_key="fa-hal-inbox", title="hal: discard 81 junk/stale unread emails",
    body="All 81 are automated or older than 1 week.",
    dispatch=[{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}],
)
Item.objects.create(
    agent=ada_agent, kind="review", origin="audit", batch_key=FLEET_AUDIT_BATCH,
    idempotency_key="fa-lily", title="hal: ONE buried HUMAN email — Lily Olson",
    body="A real person who never got an answer.",
    dispatch=[{"target_agent": "hal", "prompt": "/hal:turn --thread lily", "origin": "email"}],
)
```

And near `FLEET_AUDIT_RUN_ID`, add:

```python
FLEET_AUDIT_BATCH = "fleet-audit-2026-07-14"
```

Create `frontend/e2e/items.spec.ts`:

```typescript
import { test, expect } from '@playwright/test'

// The batch view: a fleet audit reviewed in one sitting. This is the surface that
// replaces the borrowed DDD review page — it belongs to the agent, not a narrative.

const BATCH = 'fleet-audit-2026-07-14'

test('a batch renders its items and decides one', async ({ page }) => {
  await page.goto(`/w/dimagi/agents/ada/items?batch=${BATCH}`)

  await expect(page.getByText('hal: discard 81 junk/stale unread emails')).toBeVisible()
  await expect(page.getByText('hal: ONE buried HUMAN email — Lily Olson')).toBeVisible()

  // No DDD chrome anywhere near it.
  await expect(page.getByText('DDD runs, grouped by narrative')).toHaveCount(0)

  const first = page.getByTestId('item-fa-hal-inbox')
  await first.getByRole('button', { name: 'Implement' }).click()

  await expect(first.getByText('decided')).toBeVisible()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx playwright test items.spec.ts --reporter=list`
Expected: FAIL — the route 404s / renders nothing

- [ ] **Step 3: Write the API client**

Create `frontend/src/api/items.ts`:

```typescript
// Typed against the generated OpenAPI types — this file cannot drift from the
// server. Workspace scoping is handled by apiV2's middleware (see
// WS_SCOPED_API_PREFIXES in ./client.v2), not here.
import { apiV2 } from './client.v2'
import type { components } from './generated'

type Schemas = components['schemas']

export type ItemOut = Schemas['ItemOut']
export type ItemDecision = 'implement' | 'skip' | 'defer'

// 401 never reaches here: apiV2's middleware redirects to login first.
function unwrap<T>(res: { data?: T; error?: unknown }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    throw new Error(`${what} failed: ${JSON.stringify(res.error ?? 'no data')}`)
  }
  return res.data
}

export async function listItems(
  slug: string,
  query: { batch?: string; state?: string; kind?: string } = {},
): Promise<ItemOut[]> {
  return unwrap(
    await apiV2.GET('/agents/{slug}/items/', { params: { path: { slug }, query } }),
    'listItems',
  )
}

export async function decideItem(
  itemId: string,
  decision: ItemDecision | '',
  comment = '',
): Promise<ItemOut> {
  return unwrap(
    await apiV2.POST('/items/{item_id}/decide', {
      params: { path: { item_id: itemId } },
      body: { decision, comment },
    }),
    'decideItem',
  )
}
```

**The client is `apiV2`, not `api`** — and `unwrap()` is copied here deliberately rather than imported, because `agents.ts` defines its own private copy (`agents.ts:45`). If you would rather share it, extract it to `client.v2.ts` and update both call sites in the same commit; do not import a private helper across api modules. `agents.ts` is the reference for this style (migrated onto the generated types in #212).

**Add `"/api/items"` to `WS_SCOPED_API_PREFIXES`** in `client.v2.ts` (`client.v2.ts:40`) in this task:

```typescript
const WS_SCOPED_API_PREFIXES = [
  "/api/projects",
  "/api/walkthroughs",
  "/api/reviews",
  "/api/shareouts",
  "/api/ddd",
  "/api/timeline",
  "/api/agents",
  "/api/items",
];
```

`/api/agents` is already there, so `listItems` is rewritten to `/api/w/{ws}/agents/{slug}/items/` for free — **do not add the tenant prefix by hand.** `/api/items` is a new top-level prefix and is *not* in the list, so without this line `decideItem` would hit the flat mount unpinned: it would still be safe (the flat branch of `_visible_agent_workspace_ids` scopes to the caller's memberships, and a non-member still 404s), but one of the two calls would be tenant-pinned and the other not — the kind of split that reads as correct until it isn't.

- [ ] **Step 4: Write the batch view**

Create `frontend/src/pages/agents/ItemsBatchSection.tsx`:

```tsx
import { useEffect, useState, type JSX } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { listItems, decideItem, type ItemOut, type ItemDecision } from '@/api/items'

// A batch of Items reviewed in one sitting — the fleet audit's home. It belongs to
// the agent whose queue it is, NOT to a DDD narrative: the old findings review
// borrowed that surface and conjured a phantom narrative doing it.
const DECISIONS: ItemDecision[] = ['implement', 'skip', 'defer']

function ItemCard({ item, onDecided }: { item: ItemOut; onDecided: (i: ItemOut) => void }) {
  const [busy, setBusy] = useState(false)
  const [comment, setComment] = useState('')
  const decided = item.state !== 'open'

  return (
    <article
      data-testid={`item-${item.idempotency_key}`}
      className="rounded-lg border border-border bg-card p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{item.title}</h3>
        {decided && (
          <span className="rounded bg-muted px-1.5 py-px text-[10px] uppercase tracking-wide text-muted-foreground">
            {item.state}
          </span>
        )}
      </div>
      {item.body && <p className="mt-2 text-[13px] text-foreground-secondary">{item.body}</p>}
      {(item.dispatch ?? []).length > 0 && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          Implement dispatches to{' '}
          {(item.dispatch ?? [])
            .map((d) => (d as { target_agent?: string }).target_agent || item.agent_slug)
            .join(', ')}
        </p>
      )}
      {!decided && (
        <>
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Comment (optional) — what to change, or why skip…"
            className="mt-3 w-full rounded-md border border-input bg-input p-2 text-[13px] text-foreground"
          />
          <div className="mt-2 flex gap-2">
            {DECISIONS.map((d) => (
              <button
                key={d}
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  try {
                    onDecided(await decideItem(item.id, d, comment))
                  } finally {
                    setBusy(false)
                  }
                }}
                className="rounded-md border border-border px-3 py-1 text-[13px] capitalize text-foreground hover:bg-muted disabled:opacity-50"
              >
                {d}
              </button>
            ))}
          </div>
        </>
      )}
    </article>
  )
}

export default function ItemsBatchSection(): JSX.Element {
  const { slug = '' } = useParams()
  const [params] = useSearchParams()
  const batch = params.get('batch') ?? ''
  const [items, setItems] = useState<ItemOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listItems(slug, batch ? { batch } : {})
      .then((rows) => !cancelled && setItems(rows))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Failed'))
    return () => {
      cancelled = true
    }
  }, [slug, batch])

  if (error) return <p className="p-4 text-[13px] text-destructive">{error}</p>
  if (!items) return <p className="p-4 text-[13px] text-muted-foreground">Loading…</p>

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-3 p-4" data-testid="items-batch">
      <header>
        <h1 className="text-lg font-semibold text-foreground">
          {batch || 'Items'} <span className="text-muted-foreground">· {items.length}</span>
        </h1>
      </header>
      {items.length === 0 ? (
        <p className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
          Nothing here.
        </p>
      ) : (
        items.map((i) => (
          <ItemCard
            key={i.id}
            item={i}
            onDecided={(updated) =>
              setItems((prev) => (prev ?? []).map((p) => (p.id === updated.id ? updated : p)))
            }
          />
        ))
      )}
    </div>
  )
}
```

The `data-testid` uses `item.idempotency_key` (`item-fa-hal-inbox`), which Task 4's `ItemOut` returns for exactly this reason — a stable, readable key the e2e selector can name, unlike a generated UUID.

- [ ] **Step 5: Add the route**

In `frontend/src/router.tsx`, add a lazy route beside the other agent sections:

```tsx
{ path: 'items', element: <ItemsBatchSection /> },
```

Read the surrounding agent sub-routes (`needs-you`, `tasks`, `turns`) and match their lazy-import + nesting style exactly.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd frontend && npx playwright test items.spec.ts --reporter=list`
Expected: `1 passed`

- [ ] **Step 7: Typecheck + full e2e**

Run: `cd frontend && npm run build && npx playwright test --reporter=list`
Expected: build clean; all e2e pass (including `review.spec.ts` and `supervisor.spec.ts`)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/items.ts frontend/src/api/client.v2.ts frontend/src/pages/agents/ItemsBatchSection.tsx frontend/src/router.tsx frontend/e2e/items.spec.ts frontend/e2e/seed.py
git commit -m "feat(agents): item batch view — the audit's own home, not a narrative's"
```

---

### Task 7: Document the surface

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the Items section**

In `CLAUDE.md`, under the Harness section, add:

```markdown
### Items (`apps/harness`, mounted at `/api/agents/{slug}/items/` + `/api/items/{id}/`)
An `Item` is a thing that needs addressing — **the dual of `Turn`**: `Turn` is work an agent does, `Item` is work *you* do. They cycle: a turn raises items → you decide → an approved item's `dispatch` enqueues turns. `TurnSpec.target_agent=""` means **self** (the default); Ada's cross-agent fan-out is that field set — a parameter, not a code path. The Item **carries its own text** (message semantics, like an email) rather than resolving a subject, which is what keeps it free of a source registry, of drift, and of any framework→product import. `origin_ref` is provenance, not identity. Decisions are a **closed set** (`implement | skip | defer`); only `implement` dispatches. `kind ∈ {review, question}` — there is no `notify` item (that is `/timeline`). See `docs/superpowers/specs/2026-07-15-item-and-turn-design.md`.
- `GET /api/agents/{slug}/items/` — List an agent's items (`?state=`, `?kind=`, `?batch=`)
- `POST /api/agents/{slug}/items/` — Raise items (batch; idempotent per `idempotency_key`)
- `GET /api/items/{id}/` — Get an item
- `POST /api/items/{id}/decide` — Decide (`implement` dispatches; 409 if already decided)
- `POST /api/items/{id}/dismiss` — Dismiss (never dispatches)
```

Add to the tenant-scoped URL list:

```markdown
- `/w/:workspace/agents/:slug/items` (+ `?batch=<key>`) — Items batch view: a set of items reviewed in one sitting (e.g. a fleet audit)
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: Item — the dual of Turn"
```

---

## Done criteria

- [ ] `uv run pytest -q` passes; `cd frontend && npm run build` clean; `npx playwright test` all pass.
- [ ] An item with `target_agent=""` dispatches to its own agent; with `target_agent="hal"` to Hal — **the same call, no branch**.
- [ ] Deciding twice → 409, one turn. Dismiss → no turn, ever.
- [ ] A non-member gets 404 (not 403) from list, detail, and decide.
- [ ] `product_findings`, `apps/reviews`, and `reviews.py` still work untouched — this plan is additive.

## Follow-ups (do not start without the gate)

1. **Ada emits Items** (`~/emdash/repositories/ada`) — her audit posts to `POST /api/agents/ada/items/` with a `batch_key` instead of creating a `product_findings` review.
2. **Retire `product_findings`** — *gated on (1) shipping.* Delete the gate, the runner's `reviews.py`, and PR #213's `RUN_CHILD_GATES` / attach-but-never-create / nullable-`narrative_slug` code. Deleting before Ada moves would strand her next audit with no decision surface.
3. **Phase 2** — `needs_you` over Items; `notify` → timeline (needs a new `?agent=` filter on `/api/timeline/`, added via the `_call_source` opt-in seam).
