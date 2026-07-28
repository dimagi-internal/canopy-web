# Source-Aware Runner Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route an agent's turns by *where the work came from* — ace-web work to the cloud runner, email to the laptop — via a per-agent source rule (one priority runner + a strict toggle) layered onto the existing `RunnerAssignment` cascade.

**Architecture:** `Turn.origin` becomes the source vocabulary (six values, no second column). `RunnerAssignment` gains `source` + `strict`: rows with `source=""` are the agent's default ordered list exactly as today; a non-empty `source` is that source's single priority runner. One pure helper composes the ordered list for a `(agent, origin)` pair, and both `claim_next_turn` and `unclaimable_queued_turns` cascade over whatever it returns — the cascade, ranks, `enabled`, grace and drills are untouched.

**Tech Stack:** Django 5 ASGI, Django Ninja 1.x + Pydantic v2, PostgreSQL, pytest; React 19 + Vite + Tailwind 4, vitest, `openapi-fetch` against generated types.

**Spec:** `docs/superpowers/specs/2026-07-27-source-aware-runner-routing-design.md`

## Global Constraints

- **Framework/product boundary:** `harness` and `agents` are both **framework** tier. Framework code must never import product code (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`, `storyboards`). `tests/test_architecture_boundary.py` fails CI on a violation.
- **Tenancy:** never widen a tenant predicate. Runner tenancy is `services.runner_tenant_slugs` (derived from `runner.paired_by`, NULL fails closed) — never `Runner.workspace`. Never add a `workspace_id IS NULL` "allow" leg.
- **Design tokens only** in frontend code: `bg-card`, `border-border`, `text-foreground`, `text-foreground-secondary`, `text-muted-foreground`, `text-primary`, `bg-muted`, `bg-input`, `border-input`, and status tokens `success`/`warning`/`info`/`special`/`destructive`. **No raw Tailwind palette literals** (`stone-*`, `orange-*`, `amber-*`, `red-*`, …).
- **Regenerate API types** after any `apps/**/schemas.py` or `api.py` change: `cd frontend && npm run gen:api` (backend on :8000) or `npm run gen:api:local`. The `regen-openapi.yml` workflow fails the PR if `frontend/src/api/generated.ts` is stale. It does **not** commit for you.
- **Backend tests:** `uv run pytest`. **Frontend:** `cd frontend && npm run build` (type check) and `npx vitest run <file>`.
- **Migrations:** write them the obvious way; destructive is fine. They run before cutover.
- **Commit after every task.** Open the PR with auto-merge armed: `gh pr merge <n> --auto --squash`.

---

### Task 1: The origin vocabulary

Replaces `board`/`cron`/`manual`/`drill` with the six-value source vocabulary, widens both `origin` columns, remaps existing rows, and repoints every producer. Nothing routes differently yet — this task only makes the *record* honest.

**Files:**
- Modify: `apps/harness/models.py:178-185` (the ORIGIN block), `:236` (`Turn.origin` field), `:526` (`Item.origin` field — line is inside `class Item`, find `origin = models.CharField(max_length=10, choices=Turn.ORIGIN_CHOICES)`)
- Create: `apps/harness/migrations/0030_source_vocabulary.py`
- Modify: `apps/harness/schemas.py:18` (the `Origin` literal)
- Modify: `apps/harness/services.py:153`, `:854`, `:904` (drop drill origin pre-filters), `:967`, `:995` (schedule origins), `:1648`, `:1654` (nag origins), `:1714` (drill origin)
- Modify: `apps/canopy_sessions/services.py:711`, `:795` (chat sends)
- Modify: `frontend/src/components/activity/turnLog.ts:20-29`
- Test: `tests/test_origin_vocabulary.py` (new), `frontend/src/components/activity/turnLog.test.ts`
- Update existing: `tests/test_harness_cancel_turn.py`, `tests/test_schedule_api.py`, `tests/test_schedule_services.py`, `tests/test_schedule_services_crud.py`, `tests/test_runner_assignments.py`, `tests/test_realtime_groups.py`, `tests/test_harness_claim_projects.py`, `tests/test_harness_claim_sessions.py`, `apps/mcp/tests/test_schedule_tools.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Turn.ORIGIN_API`, `Turn.ORIGIN_ACE_WEB`, `Turn.ORIGIN_CANOPY_WEB_CHAT`, `Turn.ORIGIN_CANOPY_SCHEDULER`, `Turn.ORIGIN_EMAIL`, `Turn.ORIGIN_SLACK`; `Turn.POSTABLE_ORIGINS: set[str]`; `Turn.ROUTABLE_ORIGINS: list[str]`; `Turn.LEGACY_ORIGIN_ALIASES: dict[str, str]`. In `apps/harness/schemas.py`: `Origin` (postable literal, normalizing) and `RoutableSource` (literal).

- [ ] **Step 1: Write the failing test**

Create `tests/test_origin_vocabulary.py`:

```python
"""The source vocabulary: six values, legacy aliases normalized at the boundary.

`origin` is now a ROUTING input (spec 2026-07-27), so the set of values a caller
may supply is deliberately narrower than the set the column holds.
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness.models import Item, Turn
from apps.harness.schemas import ItemIn, TurnIn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def test_the_vocabulary_is_exactly_six_values():
    assert {v for v, _label in Turn.ORIGIN_CHOICES} == {
        "api", "ace_web", "canopy_web_chat", "canopy_scheduler", "email", "slack",
    }


def test_server_only_origins_are_not_postable():
    assert Turn.POSTABLE_ORIGINS == {"api", "ace_web", "email", "slack"}
    for server_only in ("canopy_web_chat", "canopy_scheduler"):
        with pytest.raises(ValueError):
            TurnIn(agent_slug="echo", origin=server_only, idempotency_key="k")


def test_a_caller_may_post_a_source_value():
    assert TurnIn(agent_slug="echo", origin="ace_web", idempotency_key="k").origin == "ace_web"


@pytest.mark.parametrize(
    "legacy,expected",
    [("board", "api"), ("manual", "api"), ("drill", "api"), ("cron", "canopy_scheduler")],
)
def test_legacy_origins_normalize_rather_than_422(legacy, expected):
    """The live fleet posts these today. Rejecting them would 422 Echo/Ada mid-flight,
    so they normalize to their migration target for one release."""
    assert TurnIn(agent_slug="echo", origin=legacy, idempotency_key="k").origin == expected
    assert ItemIn(title="t", origin=legacy, idempotency_key="k").origin == expected


def test_an_unknown_origin_is_still_rejected():
    with pytest.raises(ValueError):
        TurnIn(agent_slug="echo", origin="wat", idempotency_key="k")


def test_routable_sources_exclude_nothing_produced_and_include_the_real_cases():
    assert set(Turn.ROUTABLE_ORIGINS) == {
        "ace_web", "email", "canopy_scheduler", "canopy_web_chat", "slack", "api",
    }


def test_both_origin_columns_hold_the_longest_value():
    """`canopy_scheduler` is 16 chars; the column was max_length=10."""
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="k1"
    )
    item = Item.objects.create(
        agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, title="t", idempotency_key="i1"
    )
    turn.refresh_from_db()
    item.refresh_from_db()
    assert turn.origin == item.origin == "canopy_scheduler"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_origin_vocabulary.py -v`
Expected: FAIL — `ImportError` / `AttributeError: type object 'Turn' has no attribute 'POSTABLE_ORIGINS'`.

- [ ] **Step 3: Rewrite the ORIGIN block in `apps/harness/models.py`**

Replace lines 178-185 (`ORIGIN_BOARD, ORIGIN_API, … ORIGIN_DRILL = (…)` through the end of `ORIGIN_CHOICES`) with:

```python
    # THE SOURCE VOCABULARY (spec 2026-07-27-source-aware-runner-routing).
    # `origin` is not just provenance any more — it is what per-agent routing
    # rules key on, so the values are the words the operator sees in the routing
    # UI and the turn log. `api` stays the honest catch-all; everything that had
    # a real producer got named.
    ORIGIN_API, ORIGIN_ACE_WEB, ORIGIN_CANOPY_WEB_CHAT = "api", "ace_web", "canopy_web_chat"
    ORIGIN_CANOPY_SCHEDULER, ORIGIN_EMAIL, ORIGIN_SLACK = "canopy_scheduler", "email", "slack"
    ORIGIN_CHOICES = [
        (ORIGIN_API, "API"), (ORIGIN_ACE_WEB, "ace-web"),
        (ORIGIN_CANOPY_WEB_CHAT, "canopy-web chat"),
        (ORIGIN_CANOPY_SCHEDULER, "canopy scheduler"),
        (ORIGIN_EMAIL, "Email"), (ORIGIN_SLACK, "Slack"),
    ]
    # What an external caller may POST. The rest are set by exactly one in-repo
    # producer each, and letting a caller spell them would let it borrow another
    # source's routing rule. Enforced at the request boundary (schemas.Origin),
    # NOT in TurnSpec.from_dict — server-authored dispatch specs (the schedule
    # nag) legitimately carry `canopy_scheduler`.
    POSTABLE_ORIGINS = {ORIGIN_API, ORIGIN_ACE_WEB, ORIGIN_EMAIL, ORIGIN_SLACK}
    # Retired values the live fleet may still be posting. Normalized at the
    # boundary (same mapping migration 0030 applied to existing rows) rather than
    # 422'd, so shipping this does not break Echo/Ada mid-flight. Remove one
    # release after the fleet is confirmed clean.
    LEGACY_ORIGIN_ALIASES = {
        "board": ORIGIN_API, "manual": ORIGIN_API, "drill": ORIGIN_API,
        "cron": ORIGIN_CANOPY_SCHEDULER,
    }
    # What a per-agent routing rule may name. Every value: a rule on a source
    # nothing produces is inert, not harmful, and `slack` is deliberately
    # reserved for the producer that does not exist yet.
    ROUTABLE_ORIGINS = [
        ORIGIN_ACE_WEB, ORIGIN_EMAIL, ORIGIN_CANOPY_SCHEDULER,
        ORIGIN_CANOPY_WEB_CHAT, ORIGIN_SLACK, ORIGIN_API,
    ]
```

Then widen the two columns. In `class Turn`, change:

```python
    origin = models.CharField(max_length=10, choices=ORIGIN_CHOICES)
```

to:

```python
    origin = models.CharField(max_length=32, choices=ORIGIN_CHOICES)
```

In `class Item`, change:

```python
    origin = models.CharField(max_length=10, choices=Turn.ORIGIN_CHOICES)
```

to:

```python
    origin = models.CharField(max_length=32, choices=Turn.ORIGIN_CHOICES)
```

- [ ] **Step 4: Write the migration**

Create `apps/harness/migrations/0030_source_vocabulary.py`:

```python
"""The source vocabulary (spec 2026-07-27): widen both origin columns and remap
the retired values. Widening a varchar in Postgres is metadata-only — no rewrite.

The reverse is deliberately lossy: `api` fans back out to board/manual/drill with
no way to tell which, so it maps everything back to `api` and only un-renames
canopy_scheduler. Reversing this migration restores a runnable schema, not the
exact prior labels.
"""
from django.db import migrations, models

FORWARD = {"cron": "canopy_scheduler", "manual": "api", "drill": "api", "board": "api"}
BACKWARD = {"canopy_scheduler": "cron", "canopy_web_chat": "api"}


def _remap(apps, mapping):
    Turn = apps.get_model("harness", "Turn")
    Item = apps.get_model("harness", "Item")
    for old, new in mapping.items():
        Turn.objects.filter(origin=old).update(origin=new)
        Item.objects.filter(origin=old).update(origin=new)


def forwards(apps, schema_editor):
    _remap(apps, FORWARD)


def backwards(apps, schema_editor):
    _remap(apps, BACKWARD)


class Migration(migrations.Migration):
    dependencies = [("harness", "0029_turntranscript_last_batch_id_and_more")]

    operations = [
        migrations.AlterField(
            model_name="turn",
            name="origin",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("api", "API"), ("ace_web", "ace-web"),
                    ("canopy_web_chat", "canopy-web chat"),
                    ("canopy_scheduler", "canopy scheduler"),
                    ("email", "Email"), ("slack", "Slack"),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="item",
            name="origin",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("api", "API"), ("ace_web", "ace-web"),
                    ("canopy_web_chat", "canopy-web chat"),
                    ("canopy_scheduler", "canopy scheduler"),
                    ("email", "Email"), ("slack", "Slack"),
                ],
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
```

- [ ] **Step 5: Normalize at the request boundary in `apps/harness/schemas.py`**

Replace line 18 (`Origin = Literal[...]`) with:

```python
# POST-able sources only. `canopy_web_chat` / `canopy_scheduler` are server-set
# (one in-repo producer each) — a caller spelling them would borrow that source's
# routing rule. Retired spellings are normalized, not rejected: the live fleet
# posts `cron`/`manual` today and 422'ing them would break Echo/Ada mid-flight.
Origin = Literal[
    "api", "ace_web", "email", "slack",           # postable
    "board", "cron", "manual", "drill",           # legacy, normalized below
]
RoutableSource = Literal[
    "ace_web", "email", "canopy_scheduler", "canopy_web_chat", "slack", "api",
]


def normalize_origin(value: str) -> str:
    """Map a retired spelling onto its replacement. Shared by every input schema
    carrying an `origin`, so the boundary can't normalize inconsistently."""
    from apps.harness.models import Turn

    return Turn.LEGACY_ORIGIN_ALIASES.get(value, value)
```

Add the validator to both input schemas that carry an origin. In `class TurnIn` (after the field declarations):

```python
    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))
```

Add the identical line to `class ItemIn`. Also add it to `class TurnSpecIn` if that schema declares an `origin` field — check with `grep -n "class TurnSpecIn" -A 10 apps/harness/schemas.py`; if it types `origin` as `Origin`, add the validator there too.

- [ ] **Step 6: Repoint the producers**

`apps/harness/services.py` — three edits in `fire_schedule` / `run_schedule_now` / `_raise_schedule_nag`:

```python
# ~line 967, in fire_schedule
            origin=Turn.ORIGIN_CANOPY_SCHEDULER,
# ~line 995, in run_schedule_now
            origin=Turn.ORIGIN_CANOPY_SCHEDULER,
# ~line 1648, the nag Item
        "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
# ~line 1654, the nag's dispatch spec — an `implement` re-runs the schedule, so it
# is the scheduler firing off-cycle, exactly like run_schedule_now.
            "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
# ~line 1714, in start_drill — a drill is an api turn that names its runner; the
# RunnerDrill row is what identifies it.
            origin=Turn.ORIGIN_API,
```

`apps/canopy_sessions/services.py` — both send paths (lines ~711 and ~795):

```python
            origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
```

- [ ] **Step 7: Drop the three drill origin pre-filters**

Each one guards a `RunnerDrill.objects.filter(turn=turn, …)` query — the FK is the real identity, so the origin check is redundant now that drills are `api` turns.

In `apps/harness/services.py:153` (inside `sweep_expired_leases`), change:

```python
            if turn.origin == Turn.ORIGIN_DRILL and status in (Turn.LOST, Turn.CANCELLED):
```

to:

```python
            # A drill turn is identified by its RunnerDrill FK, not by its origin
            # (drills are ordinary `api` turns that name a runner). The filter
            # below no-ops for a non-drill turn.
            if status in (Turn.LOST, Turn.CANCELLED):
```

At `:854` (inside `finish_turn`), change `if turn.origin == Turn.ORIGIN_DRILL and status in (Turn.FAILED, Turn.CANCELLED):` to `if status in (Turn.FAILED, Turn.CANCELLED):`.

At `:904` (inside `cancel_turn`), delete the `if turn.origin == Turn.ORIGIN_DRILL:` line and dedent its body one level so the `RunnerDrill.objects.filter(...)` call runs unconditionally.

- [ ] **Step 8: Update the frontend turn-log labels**

In `frontend/src/components/activity/turnLog.ts`, replace `originLabel` and its docblock (lines 16-29):

```ts
/** The Trigger column: what caused this turn.
 * - canopy_scheduler → "canopy_scheduler · <fired slot>" (slot lives in origin_ref.slot)
 * - anything with a launcher → "<origin> · <who enqueued it>" (the old `manual`
 *   branch — "manual" is now just an api turn, so the launcher is the signal)
 * - otherwise → the bare origin string */
export function originLabel(turn: Turn): string {
  if (turn.origin === "canopy_scheduler") {
    const slot = turn.origin_ref?.slot;
    return typeof slot === "string" ? `canopy_scheduler · ${slot}` : "canopy_scheduler";
  }
  if (turn.enqueued_by_email) {
    return `${turn.origin} · ${turn.enqueued_by_email}`;
  }
  return turn.origin;
}
```

- [ ] **Step 9: Update the frontend test**

In `frontend/src/components/activity/turnLog.test.ts`, replace the three `originLabel` assertions:

```ts
  it("surfaces the fired slot for a scheduler turn", () => {
    const t = turn({ origin: "canopy_scheduler", origin_ref: { slot: "2026-07-27T06:00:00Z" } });
    expect(originLabel(t)).toContain("canopy_scheduler");
    expect(originLabel(t)).toContain("2026");
  });
  it("names the launcher when a human enqueued it", () => {
    expect(originLabel(turn({ origin: "api", enqueued_by_email: "jj@dimagi.com" })))
      .toContain("jj@dimagi.com");
  });
  it("passes email / api through as the bare origin", () => {
    expect(originLabel(turn({ origin: "email", enqueued_by_email: null }))).toBe("email");
  });
```

Also update the module-level `turn()` factory default at line 12 from `origin: "manual"` to `origin: "api"`.

- [ ] **Step 10: Update the backend tests that name retired origins**

Mechanical rename across the test suite — these assert on or construct turns with the old constants:

```bash
grep -rln "ORIGIN_MANUAL\|ORIGIN_CRON\|ORIGIN_DRILL" tests apps
```

In each hit: `Turn.ORIGIN_MANUAL` → `Turn.ORIGIN_API`, `Turn.ORIGIN_CRON` → `Turn.ORIGIN_CANOPY_SCHEDULER`, `Turn.ORIGIN_DRILL` → `Turn.ORIGIN_API`. Two need more than a rename:

- `apps/mcp/tests/test_schedule_tools.py:86` — `Turn.objects.filter(origin=Turn.ORIGIN_MANUAL).count() == 1` becomes `Turn.objects.filter(origin=Turn.ORIGIN_CANOPY_SCHEDULER).count() == 1` (run-now is a scheduler turn now, not a manual one).
- `tests/test_runner_assignments.py:42` — rename the test `test_turn_pinned_runner_nullable_and_origin_drill` to `test_turn_pinned_runner_degrades_to_normal_routing_on_delete` and use `Turn.ORIGIN_API`.
- `tests/test_schedule_services.py:239` — `assert manual.origin == Turn.ORIGIN_CANOPY_SCHEDULER`.
- `tests/test_item_dispatch.py:53` already asserts `"email"`, which survives unchanged.

- [ ] **Step 11: Run the whole backend suite**

Run: `uv run pytest -q`
Expected: PASS. If a test fails with `value too long for type character varying(10)`, migration 0030 was not applied — check `uv run python manage.py showmigrations harness | tail -3`.

- [ ] **Step 12: Run the frontend check**

Run: `cd frontend && npx vitest run src/components/activity/turnLog.test.ts && npm run build`
Expected: PASS, clean type check.

- [ ] **Step 13: Regenerate API types**

The `Origin` literal changed, so `generated.ts` is stale.

Run: `cd frontend && npm run gen:api:local`
Expected: `src/api/generated.ts` shows the new origin union in its diff.

- [ ] **Step 14: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/0030_source_vocabulary.py \
        apps/harness/schemas.py apps/harness/services.py apps/canopy_sessions/services.py \
        frontend/src/components/activity/turnLog.ts frontend/src/components/activity/turnLog.test.ts \
        frontend/src/api/generated.ts tests apps/mcp/tests
git commit -m "feat(harness): name the source vocabulary on Turn.origin

Six values. ace-web and chat sends stop hiding inside the api catch-all;
cron becomes canopy_scheduler (one-shot schedules are coming and are not
cron); manual, board and drill retire into api. Drill turns are identified
by their RunnerDrill FK, which was always their real identity."
```

---

### Task 2: Rule columns and the composition helper

Adds `source` + `strict` to `RunnerAssignment` and the pure function that composes an ordered runner list for a `(agent, origin)` pair. Nothing calls it yet — this task is the unit-tested core.

**Files:**
- Modify: `apps/harness/models.py:608-631` (`class RunnerAssignment`)
- Create: `apps/harness/migrations/0031_runnerassignment_source_rules.py`
- Modify: `apps/harness/services.py` (add the two helpers just above `_kind_allows`, ~line 165)
- Test: `tests/test_source_rules.py` (new)

**Interfaces:**
- Consumes: Task 1's `Turn.ORIGIN_*` constants.
- Produces:
  - `RunnerAssignment.source: str` (`""` = default list), `RunnerAssignment.strict: bool`
  - `services.load_assignment_rows(agent_ids) -> tuple[dict[int, list[tuple[int, Runner]]], dict[tuple[int, str], RunnerAssignment]]` — `(defaults_by_agent, priority_by_agent_source)`, enabled rows only
  - `services.assignment_rows_for(agent_id, origin, defaults, priorities) -> list[tuple[int, Runner]]` — the composed, re-ranked list

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_rules.py`:

```python
"""Source rules: one priority runner per (agent, source), plus a strict toggle.

The composition helper is pure — it takes the loaded rows and returns the ordered
list the existing cascade walks. Everything about ranks, availability and the
grace stays in claim_next_turn; this only decides WHICH list gets cascaded.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=a_workspace())


def _runner(name):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, capabilities={})


def _rows(agent, origin):
    defaults, priorities = services.load_assignment_rows([agent.id])
    return [r for _rank, r in services.assignment_rows_for(agent.id, origin, defaults, priorities)]


def test_one_priority_runner_per_agent_and_source():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, source=Turn.ORIGIN_EMAIL)
    with pytest.raises(IntegrityError):
        RunnerAssignment.objects.create(agent=a, runner=r2, rank=0, source=Turn.ORIGIN_EMAIL)


def test_the_same_runner_may_be_a_default_and_a_priority():
    """A runner is normally BOTH: rank 2 by default, first for email."""
    a, r1 = _agent(), _runner("r1")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, source=Turn.ORIGIN_EMAIL)
    assert RunnerAssignment.objects.filter(agent=a).count() == 2


def test_no_rule_returns_the_default_list_in_rank_order():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    assert _rows(a, Turn.ORIGIN_EMAIL) == [r1, r2]


def test_a_non_strict_rule_puts_its_runner_first_then_the_defaults():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    assert _rows(a, Turn.ORIGIN_ACE_WEB) == [cloud, laptop]
    assert _rows(a, Turn.ORIGIN_EMAIL) == [laptop]  # other sources untouched


def test_a_priority_runner_already_in_the_defaults_is_not_duplicated():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    assert _rows(a, Turn.ORIGIN_ACE_WEB) == [cloud, laptop]


def test_the_composed_list_is_renumbered_from_zero():
    """The cascade compares ranks to decide who blocks whom, so a composed list
    that kept its source rows' rank=0 alongside a default rank=0 would make two
    runners each other's better rank."""
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    defaults, priorities = services.load_assignment_rows([a.id])
    composed = services.assignment_rows_for(a.id, Turn.ORIGIN_ACE_WEB, defaults, priorities)
    assert [rank for rank, _r in composed] == [0, 1]


def test_a_strict_rule_returns_only_its_runner():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    assert _rows(a, Turn.ORIGIN_EMAIL) == [laptop]


def test_a_disabled_rule_falls_back_to_the_default_list():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True, enabled=False
    )
    assert _rows(a, Turn.ORIGIN_EMAIL) == [cloud]


def test_a_disabled_default_row_is_excluded_as_before():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, enabled=False)
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    assert _rows(a, Turn.ORIGIN_API) == [r2]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_source_rules.py -v`
Expected: FAIL — `TypeError: RunnerAssignment() got unexpected keyword arguments: 'source'`.

- [ ] **Step 3: Add the columns and constraints**

In `apps/harness/models.py`, `class RunnerAssignment`, add after the `enabled` field:

```python
    # THE SOURCE RULE (spec 2026-07-27). "" = this row belongs to the agent's
    # DEFAULT ordered list (the pre-existing behaviour). Non-empty = this row is
    # the single priority runner for that source, and `rank` is meaningless on it
    # (the uniqueness constraint below allows exactly one). The column is kept
    # rather than dropped so an ordered per-source list needs no second migration.
    source = models.CharField(max_length=32, blank=True, default="")
    # Only meaningful on a source row. False: the priority runner goes first and
    # the default list follows beneath it. True: that runner or nothing — the turn
    # waits rather than degrading, which is the point for a source whose work can
    # only happen on one box (mailbox credentials, local files).
    strict = models.BooleanField(default=False)
```

Replace the `constraints` list:

```python
        constraints = [
            # One DEFAULT row per runner (what one_assignment_per_agent_runner
            # meant before source rules existed) …
            models.UniqueConstraint(
                fields=["agent", "runner"],
                condition=models.Q(source=""),
                name="one_default_assignment_per_agent_runner",
            ),
            # … and exactly one priority runner per (agent, source).
            models.UniqueConstraint(
                fields=["agent", "source"],
                condition=~models.Q(source=""),
                name="one_priority_runner_per_agent_source",
            ),
        ]
```

- [ ] **Step 4: Write the migration**

Create `apps/harness/migrations/0031_runnerassignment_source_rules.py`:

```python
"""Per-source routing rules on RunnerAssignment (spec 2026-07-27).

Splitting the old unique constraint cannot fail on existing data: every current
row has source="" and so lands in the first constraint, which is the old one
plus a condition that all of them satisfy.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0030_source_vocabulary")]

    operations = [
        migrations.AddField(
            model_name="runnerassignment",
            name="source",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="runnerassignment",
            name="strict",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(
            model_name="runnerassignment", name="one_assignment_per_agent_runner",
        ),
        migrations.AddConstraint(
            model_name="runnerassignment",
            constraint=models.UniqueConstraint(
                fields=("agent", "runner"),
                condition=models.Q(source=""),
                name="one_default_assignment_per_agent_runner",
            ),
        ),
        migrations.AddConstraint(
            model_name="runnerassignment",
            constraint=models.UniqueConstraint(
                fields=("agent", "source"),
                condition=models.Q(("source", ""), _negated=True),
                name="one_priority_runner_per_agent_source",
            ),
        ),
    ]
```

- [ ] **Step 5: Write the composition helpers**

In `apps/harness/services.py`, insert directly above `def _kind_allows` (~line 165):

```python
def load_assignment_rows(agent_ids) -> tuple[dict, dict]:
    """Load every ENABLED assignment row for these agents in one query, split into
    the two shapes routing needs: the per-agent default list (rank-ordered) and the
    per-(agent, source) priority row.

    enabled=False is filtered here, once, so a disabled row can neither claim nor
    count as a better-ranked availability blocker — and a disabled SOURCE row means
    the rule is simply off, falling back to the default list.
    """
    defaults: dict = {}
    priorities: dict = {}
    if not agent_ids:
        return defaults, priorities
    rows = (
        RunnerAssignment.objects.filter(agent_id__in=agent_ids, enabled=True)
        .select_related("runner").order_by("rank")
    )
    for row in rows:
        if row.source:
            priorities[(row.agent_id, row.source)] = row
        else:
            defaults.setdefault(row.agent_id, []).append((row.rank, row.runner))
    return defaults, priorities


def assignment_rows_for(agent_id, origin: str, defaults: dict, priorities: dict) -> list:
    """THE ordered runner list for one (agent, source) pair — what the availability
    cascade then walks. Pure: no queries, no clock.

    Ranks are renumbered from 0 because the cascade compares them to decide who
    blocks whom; a source row's stored rank is meaningless (one row per source) and
    leaving it would put two runners at rank 0, each apparently blocking the other.
    """
    base = defaults.get(agent_id) or []
    row = priorities.get((agent_id, origin))
    if row is None:
        return [(i, r) for i, (_rank, r) in enumerate(base)]
    if row.strict:
        # That runner or nothing: the turn waits rather than degrading. Everyone
        # else is absent from the list, so the wedged-runner grace cannot promote
        # them either — which is what makes "and nowhere else" actually hold.
        return [(0, row.runner)]
    rest = [r for _rank, r in base if r.id != row.runner_id]
    return [(0, row.runner)] + [(i + 1, r) for i, r in enumerate(rest)]
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_source_rules.py -v`
Expected: PASS (9 tests).

- [ ] **Step 7: Run the full suite for regressions**

Run: `uv run pytest -q`
Expected: PASS. `tests/test_runner_assignments.py::test_assignment_unique_per_agent_runner` still passes — the renamed constraint still rejects a duplicate default row.

- [ ] **Step 8: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/0031_runnerassignment_source_rules.py \
        apps/harness/services.py tests/test_source_rules.py
git commit -m "feat(harness): source rules on RunnerAssignment, plus the composition helper

One priority runner per (agent, source) and a strict toggle, stored on the
table that is already the routing authority. assignment_rows_for composes the
ordered list; the cascade that walks it is unchanged."
```

---

### Task 3: Route by source in `claim_next_turn`

Wires the composed list into claiming. This is the task that makes routing actually source-aware.

**Files:**
- Modify: `apps/harness/services.py:179-193` (`_assignment_allows_for_agent`, `_assignment_allows`), `:504-530` (the assignment_map build and the per-candidate loop in `claim_next_turn`)
- Test: `tests/test_claim_source_routing.py` (new)

**Interfaces:**
- Consumes: `services.load_assignment_rows`, `services.assignment_rows_for` (Task 2).
- Produces: `_assignment_allows_for_agent(runner, agent_id, turn, defaults, priorities, now) -> bool` (signature changed: the `assignment_map` parameter becomes the two dicts).

- [ ] **Step 1: Write the failing test**

Create `tests/test_claim_source_routing.py`:

```python
"""Claim routing keyed on Turn.origin (spec 2026-07-27).

Tenancy here is deliberately real rather than stubbed: runner_tenant_slugs derives
from runner.paired_by, and a runner whose pairer is not in the agent's workspace
claims nothing regardless of any rule.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    return {"user": jj, "ws": ws, "agent": echo, "laptop": laptop, "cloud": cloud}


def _turn(agent, origin, key, *, age_seconds=0):
    turn = Turn.objects.create(agent=agent, origin=origin, idempotency_key=key)
    if age_seconds:
        Turn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=age_seconds)
        )
        turn.refresh_from_db()
    return turn


def test_a_source_rule_sends_its_work_to_the_priority_runner(fleet):
    """ace_web work goes to the cloud box even though the laptop is rank 0."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(laptop) is None
    claimed = services.claim_next_turn(cloud)
    assert claimed is not None and claimed.origin == Turn.ORIGIN_ACE_WEB


def test_other_sources_still_follow_the_default_order(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None
    assert services.claim_next_turn(laptop) is not None


def test_a_non_strict_rule_degrades_when_its_runner_is_gone(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    Runner.objects.filter(pk=cloud.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(laptop) is not None


def test_a_strict_rule_parks_the_turn_rather_than_degrading(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    turn = _turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_strictness_survives_the_cascade_grace(fleet):
    """A turn queued past CASCADE_GRACE_SECONDS opens to lower ranks — but a strict
    rule has no lower ranks, so there is nobody for the grace to promote."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    _turn(a, Turn.ORIGIN_EMAIL, "k-mail",
          age_seconds=services.CASCADE_GRACE_SECONDS + 30)

    assert services.claim_next_turn(cloud) is None


def test_a_pin_beats_a_contradicting_rule(fleet):
    """Precedence: explicit runner > source rule."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB, strict=True
    )
    Turn.objects.create(
        agent=a, origin=Turn.ORIGIN_ACE_WEB, idempotency_key="k-pin", pinned_runner=laptop
    )

    assert services.claim_next_turn(laptop) is not None


def test_a_runner_named_only_by_a_rule_can_claim_that_source(fleet):
    """The cloud box is in no default list at all — the rule alone routes to it."""
    a, cloud = fleet["agent"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=fleet["laptop"], rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(cloud) is not None


def test_a_rule_never_crosses_the_tenant_boundary(fleet):
    """An outsider's runner named by a rule still claims nothing: the rule composes
    the list, the workspace gate decides who may act at all."""
    a, cloud = fleet["agent"], fleet["cloud"]
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=a, runner=theirs, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(theirs) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claim_source_routing.py -v`
Expected: FAIL — `test_a_source_rule_sends_its_work_to_the_priority_runner` fails at `assert services.claim_next_turn(laptop) is None` (the laptop claims it today, because origin is ignored).

- [ ] **Step 3: Change the cascade predicate to take the composed list**

In `apps/harness/services.py`, replace `_assignment_allows_for_agent` and `_assignment_allows` (lines 179-193):

```python
def _assignment_allows_for_agent(runner: Runner, agent_id, turn: Turn,
                                 defaults: dict, priorities: dict, now) -> bool:
    """False when this runner is not in the agent's list FOR THIS TURN'S SOURCE;
    True when it is and either every better rank is unavailable or the turn has
    aged past the grace.

    The list is composed per (agent, origin) — a strict source rule yields a
    single-entry list, so every other runner reads as "not in the list" and the
    grace has nobody to promote.
    """
    rows = assignment_rows_for(agent_id, turn.origin, defaults, priorities)
    mine = next((rank for rank, r in rows if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for rank, r in rows if rank < mine)


def _assignment_allows(runner: Runner, turn: Turn, defaults: dict, priorities: dict, now) -> bool:
    return _assignment_allows_for_agent(runner, turn.agent_id, turn, defaults, priorities, now)
```

- [ ] **Step 4: Load the rows through the shared helper in `claim_next_turn`**

Replace the `assignment_map` block (lines ~504-514) — everything from `assignment_map: dict = {}` through the `for row in rows:` loop — with:

```python
    # One query for every candidate agent's rows, split into the default list and
    # the per-source priorities; the per-turn composition below is in-memory.
    defaults, priorities = load_assignment_rows(agent_ids)
```

Then update the three call sites in the candidate loop (lines ~522, ~529):

```python
            if turn.agent_id:
                if not _assignment_allows(runner, turn, defaults, priorities, now):
                    continue
            if turn.chat_session_id:
                sess = turn.chat_session
                binding = getattr(sess, "runner_binding", None)
                bound_to_me = binding is not None and binding.runner_id == runner.id
                if not bound_to_me and sess.agent_id:
                    if not _assignment_allows_for_agent(
                        runner, sess.agent_id, turn, defaults, priorities, now
                    ):
                        continue
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `uv run pytest tests/test_claim_source_routing.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Run the routing regression suites**

Run: `uv run pytest tests/test_harness_claim_projects.py tests/test_harness_claim_sessions.py tests/test_runner_cascade.py tests/test_claim_schedule_parity.py tests/test_chat_laptop_routing.py -q`
Expected: PASS. (If `tests/test_runner_cascade.py` does not exist, run `ls tests | grep -i cascade` and substitute the actual filename.)

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/harness/services.py tests/test_claim_source_routing.py
git commit -m "feat(harness): claim routing keys on the turn's source

The cascade now walks the list composed for (agent, origin) instead of the
agent's single default list. A strict rule yields a one-entry list, so the
wedged-runner grace has nobody to promote and 'nowhere else' holds."
```

---

### Task 4: Keep the stuck-turn warning honest

`unclaimable_queued_turns` and `claim_next_turn` disagreeing is the drift class this codebase already pins with a parity test. A strict rule pointing at an offline box must read `offline` (recoverable), not `config` (never runs).

**Files:**
- Modify: `apps/harness/services.py:306-388` (`unclaimable_queued_turns`, specifically `_covered_by`)
- Test: `tests/test_unclaimable_source_rules.py` (new)

**Interfaces:**
- Consumes: `services.load_assignment_rows`, `services.assignment_rows_for` (Task 2).
- Produces: no new symbols; `unclaimable_queued_turns` return shape is unchanged (`[{turn_id, target, prompt, created_at, reason, kind}]`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_unclaimable_source_rules.py`:

```python
"""The stuck-turn warning must agree with claiming, per (agent, source).

Before source rules, "can anyone run this?" was a per-agent question. A strict
rule makes it per-source: the cloud box is assigned the agent and online, and
still cannot take an email turn.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    return {"user": jj, "agent": echo, "laptop": laptop, "cloud": cloud}


def _stuck_turn(agent, origin, key):
    turn = Turn.objects.create(agent=agent, origin=origin, idempotency_key=key)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - services.UNCLAIMABLE_GRACE - dt.timedelta(seconds=30)
    )
    return turn


def test_a_strict_rule_with_an_offline_runner_reads_offline_not_config(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    stuck = services.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    assert stuck[0]["kind"] == "offline"


def test_an_online_runner_excluded_by_a_strict_rule_does_not_mask_the_stall(fleet):
    """The cloud box is assigned, online and idle — and still cannot take this
    turn. Reporting it as claimable would hide a real stall."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None          # claiming says no …
    assert len(services.unclaimable_queued_turns(fleet["user"])) == 1   # … so must the warning


def test_a_turn_its_rule_can_run_is_not_reported(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.unclaimable_queued_turns(fleet["user"]) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_unclaimable_source_rules.py -v`
Expected: FAIL — `test_an_online_runner_excluded_by_a_strict_rule_does_not_mask_the_stall` reports 0 stuck turns (the coarse SQL says the online cloud box covers it).

- [ ] **Step 3: Refine coverage per (turn, runner)**

In `unclaimable_queued_turns`, after the `ids = {t.id for t in queued}` line, add the row load, then replace `_covered_by`:

```python
    ids = {t.id for t in queued}
    # Same rows claim_next_turn composes from, so the two answers cannot diverge —
    # including the session leg's agent, which routes by its agent's rules when
    # the session is not yet bound.
    agent_ids = {t.agent_id for t in queued if t.agent_id} | {
        t.chat_session.agent_id
        for t in queued
        if t.chat_session_id and t.chat_session.agent_id
    }
    defaults, priorities = load_assignment_rows(agent_ids)

    def _covered_by(rs) -> set:
        out: set = set()
        for r in rs:
            # Coarse target predicate first (assignments + projects +
            # binding-sticky sessions), plus the pin arm — a turn pinned to an
            # offline standby must read "offline", not "config".
            q = runner_target_q(r) | Q(pinned_runner=r)
            for t in (
                Turn.objects.filter(pk__in=ids).filter(q)
                .select_related("agent", "chat_session")
            ):
                # Then the SAME per-source refinement the claim loop applies. A
                # runner assigned the agent but excluded by a strict rule for this
                # turn's source does NOT cover it, and saying otherwise would mask
                # a genuinely parked queue.
                if t.pinned_runner_id != r.id:
                    routed_agent = t.agent_id or (
                        t.chat_session.agent_id if t.chat_session_id else None
                    )
                    if routed_agent:
                        rows = assignment_rows_for(routed_agent, t.origin, defaults, priorities)
                        if not any(rr.id == r.id for _rank, rr in rows):
                            continue
                out.add(t.pk)
        return out
```

Note `runner_target_q` already excludes a bound session whose holder is someone else, so a bound session turn reaching this refinement is one this runner holds — its `routed_agent` check is harmless and keeps the unbound case correct.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_unclaimable_source_rules.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the parity and warning suites**

Run: `uv run pytest tests/test_claim_schedule_parity.py tests/test_unclaimable_source_rules.py tests/test_claim_source_routing.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/harness/services.py tests/test_unclaimable_source_rules.py
git commit -m "fix(harness): the stuck-turn warning agrees with source routing

Coverage is a per-(agent, source) question now. An online runner excluded by a
strict rule no longer masks a parked queue, and a strict rule pointing at an
offline box reports offline (wait) rather than config (never runs)."
```

---

### Task 5: Let a caller name the runner

`TurnIn.runner_id` sets `pinned_runner`, which is what retires the `drill` origin: a drill is an `api` turn that names its box.

**Files:**
- Modify: `apps/harness/schemas.py` (`class TurnIn`, ~line 193)
- Modify: `apps/harness/api.py` (the enqueue view around line 686 — the `services.enqueue_turn(...)` call and the validation above it)
- Test: `tests/test_turn_runner_id.py` (new)

**Interfaces:**
- Consumes: nothing from Tasks 2-4.
- Produces: `TurnIn.runner_id: uuid.UUID | None`; the enqueue view passes it as `pinned_runner`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_turn_runner_id.py`:

```python
"""POST /api/harness/turns/ may name the runner (spec 2026-07-27).

Pinning bypasses assignments and source rules — never the tenant gate, and never
a runner the caller cannot see. That last part is the whole security surface of
this field, so it is tested from the outside via the API.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    runner = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    client.force_login(jj)
    return {"client": client, "user": jj, "agent": agent, "runner": runner}


def _post(client, body):
    return client.post("/api/harness/turns/", data=body, content_type="application/json")


def test_runner_id_pins_the_turn(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k1",
        "runner_id": str(setup["runner"].id),
    })

    assert res.status_code == 201
    assert Turn.objects.get(idempotency_key="k1").pinned_runner_id == setup["runner"].id


def test_omitting_runner_id_leaves_the_turn_unpinned(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k2",
    })

    assert res.status_code == 201
    assert Turn.objects.get(idempotency_key="k2").pinned_runner is None


def test_a_runner_the_caller_cannot_see_is_rejected(setup):
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )

    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k3",
        "runner_id": str(theirs.id),
    })

    assert res.status_code == 422
    assert not Turn.objects.filter(idempotency_key="k3").exists()


def test_a_retired_runner_is_rejected(setup):
    Runner.objects.filter(pk=setup["runner"].pk).update(status=Runner.RETIRED)

    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k4",
        "runner_id": str(setup["runner"].id),
    })

    assert res.status_code == 422


def test_an_unknown_runner_id_is_rejected(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k5",
        "runner_id": "00000000-0000-0000-0000-000000000000",
    })

    assert res.status_code == 422
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_turn_runner_id.py -v`
Expected: FAIL — 422 on the first test, because `StrictModel`-style schemas reject the unknown `runner_id` field.

- [ ] **Step 3: Add the field to `TurnIn`**

In `apps/harness/schemas.py`, `class TurnIn`, add after `routing`:

```python
    # Name the box explicitly. A pin bypasses assignments and source rules — never
    # the tenant gate, never one_executing_turn_per_agent. This is what retired the
    # `drill` origin: a drill is an api turn that names its runner, identified by
    # its RunnerDrill row rather than by a magic origin value.
    runner_id: uuid.UUID | None = None
```

- [ ] **Step 4: Resolve and validate it in the enqueue view**

In `apps/harness/api.py`, immediately before the `turn, created = services.enqueue_turn(` call (~line 686), add:

```python
    pinned = None
    if payload.runner_id is not None:
        # Same visibility predicate _runner_or_404 / list_runners gate on: a runner
        # the caller cannot see must 422 as unknown, never be attachable because
        # its UUID was guessed. Retired runners are excluded — pinning to one
        # strands the turn forever.
        pinned = (
            Runner.objects.exclude(status=Runner.RETIRED)
            .filter(_runner_visibility_q(request))
            .filter(id=payload.runner_id)
            .first()
        )
        if pinned is None:
            raise HttpError(422, f"unknown or retired runner id: {payload.runner_id}")
```

and pass it through:

```python
        enqueued_by=request.user,  # the human launching a manual / composer turn
        pinned_runner=pinned,
    )
```

Confirm `Runner` and `_runner_visibility_q` are already imported at module scope in `apps/harness/api.py` (they are — `_runner_visibility_q` is defined there). If `HttpError` is not imported, add `from ninja.errors import HttpError`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_turn_runner_id.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Regenerate types and run the suite**

Run: `uv run pytest -q && cd frontend && npm run gen:api:local`
Expected: PASS; `generated.ts` gains `runner_id` on the turn-enqueue body.

- [ ] **Step 7: Commit**

```bash
git add apps/harness/schemas.py apps/harness/api.py tests/test_turn_runner_id.py \
        frontend/src/api/generated.ts
git commit -m "feat(harness): a caller may name the runner on POST /turns/

runner_id sets pinned_runner, gated by the same visibility predicate the rest
of the harness uses. This is what let the drill origin retire."
```

---

### Task 6: The rules API

Two endpoints for source rules, plus the one-word fix that stops the default-list PUT from deleting them.

**Files:**
- Modify: `apps/agents/schemas.py` (add rule schemas after `AgentRunnersIn`, ~line 78)
- Modify: `apps/agents/api.py:182-248` (`list_agent_runners`, `replace_agent_runners`; add the two rule views after them)
- Test: `tests/test_agent_runner_rules_api.py` (new)

**Interfaces:**
- Consumes: `RunnerAssignment.source` / `.strict` (Task 2), `schemas.RoutableSource` (Task 1).
- Produces: `GET|PUT /api/agents/{slug}/runner-rules` returning `list[AgentRunnerRuleOut]` with fields `source, runner_id, runner_name, kind, strict, online, ready, enabled, queued_count`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_runner_rules_api.py`:

```python
"""GET|PUT /api/agents/{slug}/runner-rules.

The wipe test is the important one: both writes live in the same table, and a
default-list save that silently deleted every rule is exactly the bug this
endpoint split exists to prevent.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    client.force_login(jj)
    return {"client": client, "agent": agent, "laptop": laptop, "cloud": cloud}


def _put_rules(client, rules):
    return client.put(
        "/api/agents/echo/runner-rules",
        data={"rules": rules}, content_type="application/json",
    )


def test_put_then_get_round_trips_a_rule(setup):
    res = _put_rules(setup["client"], [
        {"source": "ace_web", "runner_id": str(setup["cloud"].id), "strict": True},
    ])
    assert res.status_code == 200

    got = setup["client"].get("/api/agents/echo/runner-rules").json()
    assert len(got) == 1
    assert got[0]["source"] == "ace_web"
    assert got[0]["runner_name"] == "cloud-1"
    assert got[0]["strict"] is True
    assert got[0]["online"] is True


def test_saving_the_default_list_does_not_wipe_the_rules(setup):
    """RunnerAssignment holds both; PUT /runners must scope its delete to source=''."""
    _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(setup["laptop"].id), "strict": True},
    ])

    res = setup["client"].put(
        "/api/agents/echo/runners",
        data={"runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
        content_type="application/json",
    )
    assert res.status_code == 200

    assert RunnerAssignment.objects.filter(agent=setup["agent"], source="email").exists()


def test_saving_the_rules_does_not_wipe_the_default_list(setup):
    setup["client"].put(
        "/api/agents/echo/runners",
        data={"runners": [{"runner_id": str(setup["cloud"].id), "enabled": True}]},
        content_type="application/json",
    )

    _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(setup["laptop"].id), "strict": True},
    ])

    assert RunnerAssignment.objects.filter(agent=setup["agent"], source="").count() == 1


def test_put_replaces_wholesale(setup):
    _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(setup["laptop"].id), "strict": True},
    ])
    _put_rules(setup["client"], [
        {"source": "ace_web", "runner_id": str(setup["cloud"].id), "strict": False},
    ])

    rows = RunnerAssignment.objects.filter(agent=setup["agent"]).exclude(source="")
    assert [r.source for r in rows] == ["ace_web"]


def test_a_duplicate_source_is_rejected(setup):
    res = _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(setup["laptop"].id)},
        {"source": "email", "runner_id": str(setup["cloud"].id)},
    ])
    assert res.status_code == 422


def test_a_non_routable_source_is_rejected(setup):
    res = _put_rules(setup["client"], [
        {"source": "not_a_source", "runner_id": str(setup["laptop"].id)},
    ])
    assert res.status_code == 422


def test_a_runner_the_caller_cannot_see_is_rejected(setup):
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )

    res = _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(theirs.id)},
    ])

    assert res.status_code == 422
    assert not RunnerAssignment.objects.filter(agent=setup["agent"]).exclude(source="").exists()


def test_queued_count_reports_the_parked_work(setup):
    """The UI's 'N turns are parked' warning reads this."""
    _put_rules(setup["client"], [
        {"source": "email", "runner_id": str(setup["laptop"].id), "strict": True},
    ])
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="q1")
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="q2")
    Turn.objects.create(agent=setup["agent"], origin=Turn.ORIGIN_API, idempotency_key="q3")

    got = setup["client"].get("/api/agents/echo/runner-rules").json()

    assert got[0]["queued_count"] == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent_runner_rules_api.py -v`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Add the schemas**

In `apps/agents/schemas.py`, after `class AgentRunnersIn`, add:

```python
class AgentRunnerRuleOut(StrictModel):
    """One per-source routing rule: the priority runner for a source, and whether
    it is the ONLY runner allowed to take that source's work. `queued_count` is the
    agent's queued turns from this source — what the UI's parked warning reads."""

    source: str
    runner_id: uuid.UUID
    runner_name: str
    kind: str
    strict: bool
    online: bool
    ready: bool
    enabled: bool = True
    queued_count: int = 0


class AgentRunnerRuleIn(StrictModel):
    """One rule of the wholesale-replace body. `source` is typed as the routable
    literal so an unknown source is a 422 here rather than a rule that silently
    never matches anything."""

    source: RoutableSource
    runner_id: uuid.UUID
    strict: bool = False
    enabled: bool = True


class AgentRunnerRulesIn(StrictModel):
    """Wholesale replace of an agent's source rules. Scoped to non-empty-source
    rows: the default ordered list is the sibling endpoint's business, and neither
    write may clobber the other's rows."""

    rules: list[AgentRunnerRuleIn] = Field(default_factory=list)
```

Add the import at the top of the file: `from apps.harness.schemas import RoutableSource`.

- [ ] **Step 4: Scope the default-list delete**

In `apps/agents/api.py`, in `replace_agent_runners`, change:

```python
        RunnerAssignment.objects.filter(agent=agent).delete()
```

to:

```python
        # source="" ONLY. Source rules live in this table too, and an unscoped
        # delete here would destroy every one of them each time the default
        # order was saved.
        RunnerAssignment.objects.filter(agent=agent, source="").delete()
```

In `list_agent_runners`, confirm the queryset excludes rule rows — it must read the default list only. Find the `RunnerAssignment.objects.filter(agent=agent)` query in that function and add `, source=""` to the filter.

- [ ] **Step 5: Add the two rule views**

In `apps/agents/api.py`, after `replace_agent_runners`, add:

```python
@router.get("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="List the agent's per-source routing rules")
def list_agent_runner_rules(request: HttpRequest, slug: str) -> list[AgentRunnerRuleOut]:
    """The per-source overrides on top of the default ordered list. One rule per
    source, max — the priority runner, and whether it is the only one allowed."""
    from django.db.models import Count

    from apps.harness.models import RunnerAssignment, Turn

    agent = _get_agent_or_404(request, slug)
    queued = dict(
        Turn.objects.filter(agent=agent, status=Turn.QUEUED)
        .values_list("origin").annotate(n=Count("id"))
    )
    rows = (
        RunnerAssignment.objects.filter(agent=agent).exclude(source="")
        .select_related("runner").order_by("source")
    )
    return [
        AgentRunnerRuleOut(
            source=row.source,
            runner_id=row.runner.id,
            runner_name=row.runner.name,
            kind=row.runner.kind,
            strict=row.strict,
            online=row.runner.live_status == Runner.ONLINE,
            ready=row.runner.ready,
            enabled=row.enabled,
            queued_count=queued.get(row.source, 0),
        )
        for row in rows
    ]


@router.put("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="Replace the agent's per-source routing rules")
def replace_agent_runner_rules(
    request: HttpRequest, slug: str, payload: AgentRunnerRulesIn
) -> list[AgentRunnerRuleOut]:
    """Wholesale replace, scoped to non-empty-source rows — the default ordered
    list belongs to PUT /runners and is left alone."""
    from apps.harness.api import _runner_visibility_q
    from apps.harness.models import Runner, RunnerAssignment

    agent = _get_agent_or_404(request, slug)

    sources = [r.source for r in payload.rules]
    if len(sources) != len(set(sources)):
        raise HttpError(422, "one rule per source: duplicate source in list")

    ids = [r.runner_id for r in payload.rules]
    runners = list(
        Runner.objects.filter(id__in=ids)
        .exclude(status=Runner.RETIRED)
        .filter(_runner_visibility_q(request))
    )
    by_id = {r.id: r for r in runners}
    missing = [str(rid) for rid in ids if rid not in by_id]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")

    with transaction.atomic():
        RunnerAssignment.objects.filter(agent=agent).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(
                agent=agent, runner=by_id[r.runner_id], rank=0,
                source=r.source, strict=r.strict, enabled=r.enabled,
            )
            for r in payload.rules
        ])
    return list_agent_runner_rules(request, slug)
```

Add `AgentRunnerRuleOut`, `AgentRunnerRuleIn`, `AgentRunnerRulesIn` to the existing `from .schemas import (...)` block at the top of `apps/agents/api.py`, and confirm `Runner` is imported where `list_agent_runner_rules` uses it (the local import inside `replace_agent_runner_rules` does not cover the GET — add `from apps.harness.models import Runner` inside `list_agent_runner_rules` too, matching the file's existing local-import style for harness models).

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent_runner_rules_api.py -v`
Expected: PASS (8 tests).

- [ ] **Step 7: Run the existing runners-API suite and regenerate types**

Run: `uv run pytest tests/test_agent_runners_api.py -q && uv run pytest -q && cd frontend && npm run gen:api:local`
Expected: PASS; `generated.ts` gains the `/api/agents/{slug}/runner-rules` paths.

- [ ] **Step 8: Commit**

```bash
git add apps/agents/schemas.py apps/agents/api.py tests/test_agent_runner_rules_api.py \
        frontend/src/api/generated.ts
git commit -m "feat(agents): GET|PUT /api/agents/{slug}/runner-rules

Source rules get their own wholesale-replace endpoint, and PUT /runners' delete
is scoped to source='' — unscoped, saving the default order deleted every rule."
```

---

### Task 7: The Runners-tab editor

Layout A: today's chip row relabelled **Default order**, with an indented per-source exception list beneath it.

**Files:**
- Modify: `frontend/src/api/agents.ts` (add the two client functions after `putAgentRunners`, ~line 220)
- Create: `frontend/src/components/agents/RunnerSourceRules.tsx`
- Create: `frontend/src/components/agents/RunnerSourceRules.test.tsx`
- Modify: `frontend/src/components/agents/RunnerAssignments.tsx` (label the existing row; mount the rules editor)

**Interfaces:**
- Consumes: `GET|PUT /api/agents/{slug}/runner-rules` (Task 6).
- Produces: `getAgentRunnerRules(slug)`, `putAgentRunnerRules(slug, rules)`, `<RunnerSourceRules agentSlug=… />`, and the pure helpers `nextRulesForAdd`, `nextRulesForRunner`, `nextRulesForStrict`, `nextRulesForRemove`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/agents/RunnerSourceRules.test.tsx`:

```tsx
import { describe, expect, it } from 'vitest'
import {
  nextRulesForAdd,
  nextRulesForRemove,
  nextRulesForRunner,
  nextRulesForStrict,
  availableSources,
  type RuleRow,
} from './RunnerSourceRules'

const rules: RuleRow[] = [
  { source: 'ace_web', runnerId: 'r-cloud', strict: true },
  { source: 'email', runnerId: 'r-laptop', strict: false },
]

describe('rule list transforms', () => {
  it('adds a rule defaulting to fall-through', () => {
    const next = nextRulesForAdd(rules, 'canopy_scheduler', 'r-cloud')
    expect(next).toHaveLength(3)
    expect(next[2]).toEqual({ source: 'canopy_scheduler', runnerId: 'r-cloud', strict: false })
  })

  it('repoints one rule without touching the others', () => {
    const next = nextRulesForRunner(rules, 'email', 'r-cloud')
    expect(next[1].runnerId).toBe('r-cloud')
    expect(next[0]).toEqual(rules[0])
  })

  it('flips strict in place', () => {
    expect(nextRulesForStrict(rules, 'email')[1].strict).toBe(true)
  })

  it('removes by source', () => {
    expect(nextRulesForRemove(rules, 'ace_web').map((r) => r.source)).toEqual(['email'])
  })

  it('offers only sources not already ruled on', () => {
    expect(availableSources(rules)).not.toContain('ace_web')
    expect(availableSources(rules)).toContain('canopy_scheduler')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/components/agents/RunnerSourceRules.test.tsx`
Expected: FAIL — cannot resolve `./RunnerSourceRules`.

- [ ] **Step 3: Add the API client functions**

In `frontend/src/api/agents.ts`, after `putAgentRunners`:

```ts
export type AgentRunnerRuleOut = Schemas['AgentRunnerRuleOut']

// Per-source overrides on top of the default ordered list — one rule per source.
export async function getAgentRunnerRules(slug: string): Promise<AgentRunnerRuleOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/runner-rules', { params: { path: { slug } } })
  return Array.from(unwrap(res, 'getAgentRunnerRules'))
}

// Wholesale replace, scoped server-side to source rules — the default ordered
// list is putAgentRunners' business and is left untouched.
export async function putAgentRunnerRules(
  slug: string,
  rules: readonly { source: string; runnerId: string; strict: boolean }[],
): Promise<AgentRunnerRuleOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/runner-rules', {
    params: { path: { slug } },
    body: {
      rules: rules.map((r) => ({
        source: r.source as AgentRunnerRuleOut['source'],
        runner_id: r.runnerId,
        strict: r.strict,
      })),
    },
  })
  return Array.from(unwrap(res, 'putAgentRunnerRules'))
}
```

- [ ] **Step 4: Write the component**

Create `frontend/src/components/agents/RunnerSourceRules.tsx`:

```tsx
import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import {
  getAgentRunnerRules,
  putAgentRunnerRules,
  type AgentRunnerRuleOut,
} from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'

// The per-source exception list that sits under an agent's DEFAULT runner order
// (see RunnerAssignments). A rule is one priority runner plus a strict toggle —
// deliberately not a second ordered list: ordering already exists once, in the
// default list, and a source only needs to say "prefer this box" and optionally
// "and nowhere else".
//
// Mirrors RunnerAssignments' commit machinery: optimistic local state, a ref that
// keeps pace with it so a fast second click composes on top of the first, and a
// monotonic sequence so an out-of-order response can't undo a newer edit.

// Kept in the order the routing UI should offer them: the ones with real
// producers first. The server types `source` as a literal, so an unknown value
// 422s rather than silently never matching.
export const ROUTABLE_SOURCES = [
  'ace_web',
  'email',
  'canopy_scheduler',
  'canopy_web_chat',
  'slack',
  'api',
] as const

const SOURCE_LABEL: Record<string, string> = {
  ace_web: 'ace-web',
  email: 'email',
  canopy_scheduler: 'scheduler',
  canopy_web_chat: 'canopy chat',
  slack: 'slack',
  api: 'api (unclassified)',
}

export type RuleRow = { source: string; runnerId: string; strict: boolean }

function toRows(rules: readonly AgentRunnerRuleOut[]): RuleRow[] {
  return rules.map((r) => ({ source: r.source, runnerId: r.runner_id, strict: r.strict }))
}

export function nextRulesForAdd(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  return [...toRows(rules as AgentRunnerRuleOut[] as never), { source, runnerId, strict: false }]
}

export function nextRulesForRunner(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  return rules.map((r) => (r.source === source ? { ...r, runnerId } : r))
}

export function nextRulesForStrict(rules: readonly RuleRow[], source: string): RuleRow[] {
  return rules.map((r) => (r.source === source ? { ...r, strict: !r.strict } : r))
}

export function nextRulesForRemove(rules: readonly RuleRow[], source: string): RuleRow[] {
  return rules.filter((r) => r.source !== source)
}

export function availableSources(rules: readonly RuleRow[]): string[] {
  const taken = new Set(rules.map((r) => r.source))
  return ROUTABLE_SOURCES.filter((s) => !taken.has(s))
}

export function RunnerSourceRules({ agentSlug }: { agentSlug: string }): JSX.Element {
  const [rules, setRules] = useState<AgentRunnerRuleOut[] | null>(null)
  const [fleet, setFleet] = useState<RunnerOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  const rulesRef = useRef<AgentRunnerRuleOut[]>([])
  const apply = (next: AgentRunnerRuleOut[]) => {
    rulesRef.current = next
    setRules(next)
  }
  const seqRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    setRules(null)
    rulesRef.current = []
    setError(null)
    Promise.all([getAgentRunnerRules(agentSlug), listRunners()])
      .then(([r, f]) => {
        if (cancelled) return
        apply(r)
        setFleet(f)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        apply([])
      })
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  const rows = useMemo(() => toRows(rules ?? []), [rules])

  const commit = async (next: RuleRow[], prev: AgentRunnerRuleOut[]) => {
    const mySeq = ++seqRef.current
    // Optimistic: patch names/state from the fleet list so a fresh rule renders
    // immediately rather than flashing empty until the PUT returns.
    apply(
      next.map((r) => {
        const existing = prev.find((p) => p.source === r.source)
        const f = fleet.find((x) => x.id === r.runnerId)
        return {
          source: r.source,
          runner_id: r.runnerId,
          runner_name: f?.name ?? existing?.runner_name ?? r.runnerId,
          kind: f?.kind ?? existing?.kind ?? '',
          strict: r.strict,
          online: f ? f.status === 'online' : (existing?.online ?? false),
          ready: f?.ready ?? existing?.ready ?? false,
          enabled: true,
          queued_count: existing?.queued_count ?? 0,
        } as AgentRunnerRuleOut
      }),
    )
    setError(null)
    try {
      const saved = await putAgentRunnerRules(agentSlug, next)
      if (seqRef.current !== mySeq) return
      apply(saved)
    } catch (e: unknown) {
      if (seqRef.current !== mySeq) return
      apply(prev)
      setError(e instanceof Error ? e.message : 'Failed to save')
    }
  }

  const mutate = (fn: (rows: RuleRow[]) => RuleRow[]) => {
    const prev = rulesRef.current
    void commit(fn(toRows(prev)), prev)
  }

  if (rules === null) {
    return (
      <div
        className="h-6 w-40 animate-pulse rounded-md bg-muted"
        data-testid="runner-rules-loading"
      />
    )
  }

  const unruled = availableSources(rows)

  return (
    <div className="flex flex-col gap-1" data-testid={`runner-rules-${agentSlug}`}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Except when the work comes from
      </span>

      {rules.length === 0 && (
        <span className="text-[11px] text-muted-foreground">
          No exceptions — every source follows the default order.
        </span>
      )}

      {rules.map((r) => (
        <div key={r.source} data-testid={`runner-rule-${r.source}`}>
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-2 py-1">
            <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary">
              {SOURCE_LABEL[r.source] ?? r.source}
            </span>
            <span className="text-muted-foreground">→</span>

            <select
              value={r.runner_id}
              onChange={(e) => mutate((rows) => nextRulesForRunner(rows, r.source, e.target.value))}
              aria-label={`Runner for ${r.source}`}
              className="rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
            >
              {fleet.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>

            {/* The strict toggle, worded as its consequence rather than as the
                flag name: "only" parks the queue when that box is down, which is
                the point for a source that can only run in one place. */}
            <div className="flex overflow-hidden rounded border border-input text-[10px]">
              <button
                type="button"
                onClick={() => !r.strict || mutate((rows) => nextRulesForStrict(rows, r.source))}
                className={`px-2 py-0.5 ${
                  r.strict ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                }`}
                aria-pressed={r.strict}
              >
                only
              </button>
              <button
                type="button"
                onClick={() => r.strict && mutate((rows) => nextRulesForStrict(rows, r.source))}
                className={`px-2 py-0.5 ${
                  r.strict ? 'text-muted-foreground' : 'bg-primary text-primary-foreground'
                }`}
                aria-pressed={!r.strict}
              >
                fall through
              </button>
            </div>

            <button
              type="button"
              onClick={() => mutate((rows) => nextRulesForRemove(rows, r.source))}
              aria-label={`Remove the ${r.source} rule`}
              className="ml-auto px-1 text-muted-foreground hover:text-destructive"
            >
              ✕
            </button>
          </div>

          {/* Strictness parking a queue is the toggle working; parking it
              SILENTLY is the failure. Say it, with the count. */}
          {r.strict && !r.online && (
            <p className="mt-0.5 pl-2 text-[11px] text-warning" data-testid={`runner-rule-parked-${r.source}`}>
              ⚠ {r.runner_name} is offline
              {r.queued_count > 0
                ? ` — ${r.queued_count} ${r.source} turn${r.queued_count === 1 ? '' : 's'} parked, and will stay parked.`
                : ' — this source will not run until it returns.'}
            </p>
          )}
        </div>
      ))}

      <div className="relative">
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          disabled={unruled.length === 0 || fleet.length === 0}
          className="rounded-md border border-input bg-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-primary hover:text-primary disabled:opacity-40"
          data-testid="runner-rules-add-toggle"
        >
          + rule
        </button>
        {menuOpen && (
          <div className="absolute left-0 top-full z-10 mt-1 flex min-w-[11rem] flex-col gap-0.5 rounded-md border border-border bg-card p-1 shadow-md">
            {unruled.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => {
                  setMenuOpen(false)
                  mutate((rows) => nextRulesForAdd(rows, s, fleet[0].id))
                }}
                className="rounded px-2 py-1 text-left font-mono text-[11px] text-foreground hover:bg-muted"
              >
                {SOURCE_LABEL[s] ?? s}
              </button>
            ))}
          </div>
        )}
      </div>

      <p className="text-[10px] text-foreground-subtle">
        A named runner wins over a rule; a live chat stays on the box hosting it.
      </p>

      {error && <span className="text-[11px] text-destructive">{error}</span>}
    </div>
  )
}
```

- [ ] **Step 5: Fix the `nextRulesForAdd` cast**

The cast in Step 4's `nextRulesForAdd` is wrong — `toRows` takes API rows, but this helper receives `RuleRow[]`. Replace that function with:

```tsx
export function nextRulesForAdd(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  return [...rules, { source, runnerId, strict: false }]
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/components/agents/RunnerSourceRules.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 7: Mount it under the default list**

In `frontend/src/components/agents/RunnerAssignments.tsx`, import the component:

```tsx
import { RunnerSourceRules } from '@/components/agents/RunnerSourceRules'
```

Change the outer wrapper of the returned JSX from a single flex row into a column holding the labelled chip row plus the rules editor. Replace the opening wrapper:

```tsx
    <div className="flex flex-wrap items-center gap-1.5" data-testid={`runner-assignments-${agentSlug}`}>
```

with:

```tsx
    <div className="flex flex-col gap-2" data-testid={`runner-assignments-${agentSlug}`}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Default order
      </span>
      <div className="flex flex-wrap items-center gap-1.5">
```

and close the new inner div plus append the rules editor immediately before the final `</div>` of the component (after the `{error && …}` line):

```tsx
      </div>
      <RunnerSourceRules agentSlug={agentSlug} />
    </div>
```

- [ ] **Step 8: Verify the existing component tests still pass**

Run: `cd frontend && npx vitest run src/components/agents/RunnerAssignments.test.tsx`
Expected: PASS — that suite tests the pure row transforms, which are untouched.

- [ ] **Step 9: Type check and build**

Run: `cd frontend && npm run build`
Expected: clean build.

- [ ] **Step 10: Check for raw palette literals**

Run: `grep -nE "(stone|orange|zinc|slate|amber|emerald|sky|violet|red)-[0-9]" src/components/agents/RunnerSourceRules.tsx`
Expected: no output.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/api/agents.ts frontend/src/components/agents/RunnerSourceRules.tsx \
        frontend/src/components/agents/RunnerSourceRules.test.tsx \
        frontend/src/components/agents/RunnerAssignments.tsx
git commit -m "feat(runners): per-source routing rules on the Runners tab

Today's chip row becomes 'Default order'; an indented exception list beneath it
holds one rule per source — a priority runner and an only/fall-through toggle.
A strict rule whose runner is offline says so, with the parked count."
```

---

### Task 8: Prove the three real cases, and document

An end-to-end test of the operator's actual scenarios, plus the CLAUDE.md updates that keep the repo's own documentation true.

**Files:**
- Create: `tests/test_source_routing_e2e.py`
- Modify: `CLAUDE.md` (the Harness section's routing paragraph; the Agents section's runner endpoints; the Design Decisions directed-routing bullet)
- Modify: `docs/superpowers/specs/2026-07-27-source-aware-runner-routing-design.md` (status line)

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: nothing new.

- [ ] **Step 1: Write the end-to-end test**

Create `tests/test_source_routing_e2e.py`:

```python
"""The three cases this feature was built for, end to end through the API.

Deliberately driven through HTTP rather than the service layer: the operator
configures this in the Runners tab, and the thing that must work is the whole
path from that PUT to which runner claims.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={"sessions": True},
    )
    for agent in (ace, echo):
        RunnerAssignment.objects.create(agent=agent, runner=laptop, rank=0)
        RunnerAssignment.objects.create(agent=agent, runner=cloud, rank=1)
    client.force_login(jj)
    return {"client": client, "ace": ace, "echo": echo, "laptop": laptop, "cloud": cloud}


def _rule(client, slug, source, runner, strict):
    res = client.put(
        f"/api/agents/{slug}/runner-rules",
        data={"rules": [{"source": source, "runner_id": str(runner.id), "strict": strict}]},
        content_type="application/json",
    )
    assert res.status_code == 200, res.content


def test_ace_web_work_runs_on_the_cloud_runner(fleet):
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    res = fleet["client"].post(
        "/api/harness/turns/",
        data={"agent_slug": "ace", "origin": "ace_web", "idempotency_key": "e2e-ace",
              "prompt": "/ace:turn"},
        content_type="application/json",
    )
    assert res.status_code == 201

    assert services.claim_next_turn(fleet["laptop"]) is None
    claimed = services.claim_next_turn(fleet["cloud"])
    assert claimed is not None and claimed.agent_id == fleet["ace"].id


def test_email_work_stays_on_the_laptop(fleet):
    """The inbox watcher enqueues these unpinned from whichever box polled."""
    _rule(fleet["client"], "echo", "email", fleet["laptop"], True)
    Turn.objects.create(
        agent=fleet["echo"], origin=Turn.ORIGIN_EMAIL, idempotency_key="email-echo-t1-1",
        origin_ref={"thread_id": "t1", "from": "someone@example.com"},
        prompt="/echo:turn --thread t1",
    )

    assert services.claim_next_turn(fleet["cloud"]) is None
    assert services.claim_next_turn(fleet["laptop"]) is not None


def test_scheduled_work_prefers_the_cloud_but_still_degrades(fleet):
    """Non-strict: the lid can be shut at 6am, but a dead cloud box must not
    park the schedule forever."""
    _rule(fleet["client"], "echo", "canopy_scheduler", fleet["cloud"], False)
    Turn.objects.create(
        agent=fleet["echo"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
        idempotency_key="sched:1:2026-07-27T06:00", prompt="/echo:turn",
    )

    assert services.claim_next_turn(fleet["laptop"]) is None   # cloud is up, it goes first
    assert services.claim_next_turn(fleet["cloud"]) is not None


def test_the_three_rules_coexist_on_one_fleet(fleet):
    fleet["client"].put(
        "/api/agents/echo/runner-rules",
        data={"rules": [
            {"source": "email", "runner_id": str(fleet["laptop"].id), "strict": True},
            {"source": "canopy_scheduler", "runner_id": str(fleet["cloud"].id), "strict": False},
        ]},
        content_type="application/json",
    )
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    Turn.objects.create(agent=fleet["echo"], origin=Turn.ORIGIN_EMAIL, idempotency_key="m1")
    Turn.objects.create(agent=fleet["ace"], origin=Turn.ORIGIN_ACE_WEB, idempotency_key="a1")

    first = services.claim_next_turn(fleet["laptop"])
    second = services.claim_next_turn(fleet["cloud"])

    assert first is not None and first.origin == Turn.ORIGIN_EMAIL
    assert second is not None and second.origin == Turn.ORIGIN_ACE_WEB
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_source_routing_e2e.py -v`
Expected: PASS (4 tests).

- [ ] **Step 3: Update the Harness section of CLAUDE.md**

Find the paragraph beginning **"Directed routing (`RunnerAssignment`) is THE routing authority for agent turns"** and append to it:

```markdown
Since spec 2026-07-27, an assignment row also carries a **`source`**: `""` is the
agent's default ordered list (the cascade above), while a non-empty `source` is that
source's single **priority runner** plus a **`strict`** toggle. At claim time
`services.assignment_rows_for(agent_id, turn.origin, …)` composes the list the
cascade walks — priority first then the defaults, or the priority alone when strict
(so the wedged-runner grace has nobody to promote and "nowhere else" holds). One rule
per `(agent, source)`; a disabled rule falls back to the default list. Precedence:
**explicit `runner_id`/pin → session binding → source rule → default order**.
Edited on the Runners tab under each agent's "Default order" row; served by
`GET|PUT /api/agents/{slug}/runner-rules`.
```

- [ ] **Step 4: Update the origin vocabulary note in CLAUDE.md**

In the **Harness** section, immediately after the `POST /api/harness/turns/` bullet, add:

```markdown
  `origin` is the **source vocabulary** and a routing input, not just provenance:
  `ace_web` · `canopy_web_chat` · `canopy_scheduler` · `email` · `slack` · `api`.
  A caller may POST only `ace_web`/`email`/`slack`/`api` (the other two are set by
  their single in-repo producer); retired spellings (`board`/`manual`/`drill`/`cron`)
  normalize at the boundary for one release. `POST /turns/` also accepts an optional
  `runner_id`, which pins the turn — that is what retired the `drill` origin (a drill
  is an `api` turn that names its runner, identified by its `RunnerDrill` row).
```

- [ ] **Step 5: Update the Agents endpoint list in CLAUDE.md**

After the `GET|PUT /api/agents/{slug}/runners` bullet, add:

```markdown
- `GET|PUT /api/agents/{slug}/runner-rules` — the agent's **per-source** routing
  rules (one priority runner + `strict` per source). Wholesale replace, scoped to
  non-empty-source rows; `PUT /runners` is scoped to `source=""`, so neither write
  clobbers the other's rows (they share one table).
```

- [ ] **Step 6: Mark the spec shipped**

In `docs/superpowers/specs/2026-07-27-source-aware-runner-routing-design.md`, change:

```markdown
**Status:** Approved design, pre-implementation
```

to:

```markdown
**Status:** Shipped
```

- [ ] **Step 7: Full verification**

Run: `uv run pytest -q && cd frontend && npm run build && npx vitest run`
Expected: all green. Then confirm the generated types are fresh:
Run: `cd frontend && npm run gen:api:local && git diff --stat src/api/generated.ts`
Expected: no diff (already regenerated in Tasks 5 and 6).

- [ ] **Step 8: Commit and open the PR**

```bash
git add tests/test_source_routing_e2e.py CLAUDE.md \
        docs/superpowers/specs/2026-07-27-source-aware-runner-routing-design.md
git commit -m "test(harness): the three source-routing cases, end to end

ace-web to the cloud box, email pinned to the laptop, schedules preferring the
cloud but still degrading. Plus the CLAUDE.md routing and vocabulary updates."

git push -u origin HEAD
gh pr create --title "Source-aware runner routing" --body "$(cat <<'EOF'
Routes an agent's turns by WHERE THE WORK CAME FROM: ace-web work to the cloud
runner, email to the laptop, schedules to whichever box is up at 6am.

- `Turn.origin` becomes the source vocabulary — six values, with `ace_web` and
  `canopy_web_chat` no longer hiding inside the `api` catch-all. `board`,
  `manual`, `cron` and `drill` retire (legacy spellings normalize at the
  boundary for one release).
- `RunnerAssignment` gains `source` + `strict`: one priority runner per
  (agent, source), composed into the ordered list the existing cascade already
  walks. Ranks, `enabled`, the grace and drills are untouched.
- `POST /turns/` accepts `runner_id` — which is what let the `drill` origin go.
- Runners tab: today's chip row becomes "Default order" with a per-source
  exception list beneath it. A strict rule whose runner is offline says so,
  with the parked count.

Spec: docs/superpowers/specs/2026-07-27-source-aware-runner-routing-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --auto --squash
```

---

## Self-Review

**Spec coverage:** vocabulary + both column widenings + remap (Task 1); boundary enforcement incl. the `TurnSpec.from_dict` hole (Task 1, Step 5 — validation lives in the input schemas, `from_dict` untouched); rule columns + constraints + composition (Task 2); claim wiring incl. strictness-past-grace and the session leg (Task 3); `unclaimable` parity (Task 4); `runner_id` (Task 5); rules API + the scoped delete (Task 6); UI option A incl. the parked warning and the precedence line (Task 7); the three real cases + docs (Task 8). Session binding, project turns, and retire-cascade are asserted as unchanged rather than modified, matching the spec's "what is unchanged" section.

**One deliberate divergence from the spec, recorded here:** the spec says server-only origins are rejected at the boundary. The plan additionally *normalizes* the retired spellings (`board`/`manual`/`drill`/`cron`) instead of 422'ing them, because the live fleet posts them today and a hard rejection would break Echo and Ada the moment this deploys. `cron` therefore reaches `canopy_scheduler` through the alias — a one-release shim, flagged for removal in the model comment.
