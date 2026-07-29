# Agent Scheduled Turns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human declare recurring turns per agent (Eva's goal review, Echo's weekly manager report), fire them onto the existing harness turn path, and nag via the supervisor inbox when a fired occurrence goes unfinished.

**Architecture:** A new `AgentSchedule` model in `apps/harness` holds cron config server-side. The laptop runner syncs schedules, evaluates them locally with `croniter`, and POSTs a `fire` — the server materializes a `Turn` through the **existing** `enqueue_turn()`. The scheduler is a *producer of turns*, not a second execution engine. Firing slot N+1 supersedes slot N's unfinished turn as `MISSED`, which also unwedges the `one_executing_turn_per_agent` constraint. The nag is a projection inside `needs_you()`, not a new object.

**Tech Stack:** Django 5, Django Ninja 1.x + Pydantic v2, PostgreSQL, pytest, React 19 + Vite + Tailwind 4, `canopy-ui`, `openapi-fetch`.

**Spec:** `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md` — read it before Task 1.

## Global Constraints

- **Framework tier.** `apps/harness` must never import product apps (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`). `tests/test_architecture_boundary.py` fails CI otherwise.
- **`capabilities` is NOT a security boundary.** It is a caller-supplied routing hint. The workspace is the gate. Every runner-facing route intersects both. (Established by commit b4f5ead, *Critical*.)
- **404, never 403,** for non-membership — no existence leak.
- **Errors** are RFC 7807 `application/problem+json` via `apps/api/errors.py`.
- **Every route** carries `summary=` and `openapi_extra={"x-mcp-expose": True}`.
- **Design tokens only** in frontend — `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`. **Never** raw palette literals (`stone-*`, `orange-*`, `zinc-*`, `amber-*`).
- **`AgentSchedule` pk is an int** (AutoField), not UUID — `NeedsYouItem.ref_id` is typed `int` on a `StrictModel`.
- **Run backend tests** with `uv run pytest`. **Frontend typecheck** with `cd frontend && npm run build`.
- **Concurrent branch `emdash/mobile-l2vmm`** is unmerged and touches `apps/harness/models.py`. If it lands first, renumber this plan's migration and rebase. See the spec's "Concurrent work" table.

---

### Task 1: `Turn.MISSED` — a terminal status for "you skipped this"

**Files:**
- Modify: `apps/harness/models.py` (Turn.STATUS_CHOICES, Turn.TERMINAL)
- Modify: `apps/harness/services.py:189` (`finish_turn` guard)
- Create: `apps/harness/migrations/0004_turn_missed.py`
- Test: `tests/test_harness_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Turn.MISSED == "missed"`; `Turn.MISSED in Turn.TERMINAL`; `finish_turn(turn, status=Turn.MISSED, result_note=str) -> Turn`.

`LOST` is not reusable — it means "lease expired, we lost track." Conflating it with "you skipped your goal review" would make the ledger and UI lie.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_models.py`:

```python
def test_finish_turn_accepts_missed():
    """MISSED is a terminal status distinct from LOST (infra failure)."""
    agent = Agent.objects.create(slug="eva", name="Eva")
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_CRON, idempotency_key="k1", status=Turn.RUNNING
    )
    out = services.finish_turn(turn, status=Turn.MISSED, result_note="superseded")

    assert out.status == Turn.MISSED
    assert Turn.MISSED in Turn.TERMINAL
    assert out.finished_at is not None
    assert out.result_note == "superseded"
```

Ensure the file's imports include:

```python
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_models.py::test_finish_turn_accepts_missed -v`
Expected: FAIL — `AttributeError: type object 'Turn' has no attribute 'MISSED'`

- [ ] **Step 3: Add the status to the model**

In `apps/harness/models.py`, inside `class Turn`, change the status block to:

```python
    QUEUED, CLAIMED, RUNNING, NEEDS_HUMAN = "queued", "claimed", "running", "needs_human"
    DONE, FAILED, LOST, MISSED = "done", "failed", "lost", "missed"
    STATUS_CHOICES = [
        (QUEUED, "Queued"), (CLAIMED, "Claimed"), (RUNNING, "Running"),
        (NEEDS_HUMAN, "Needs human"), (DONE, "Done"), (FAILED, "Failed"),
        (LOST, "Lost"), (MISSED, "Missed"),
    ]
    TERMINAL = {DONE, FAILED, LOST, MISSED}
    NON_TERMINAL = {QUEUED, CLAIMED, RUNNING, NEEDS_HUMAN}
```

- [ ] **Step 4: Widen the `finish_turn` guard**

In `apps/harness/services.py`, in `finish_turn`, replace the guard line:

```python
    if status not in (Turn.DONE, Turn.FAILED):
        raise ValueError(f"finish status must be done|failed, got {status!r}")
```

with:

```python
    if status not in (Turn.DONE, Turn.FAILED, Turn.MISSED):
        raise ValueError(f"finish status must be done|failed|missed, got {status!r}")
```

Also update its docstring first line to:

```python
    """Transition CLAIMED|RUNNING|NEEDS_HUMAN -> DONE|FAILED|MISSED. A no-op (no
```

- [ ] **Step 5: Generate the migration**

Run: `uv run python manage.py makemigrations harness -n turn_missed`
Expected: `Migrations for 'harness': apps/harness/migrations/0004_turn_missed.py - Alter field status on turn`

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness_models.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add apps/harness/models.py apps/harness/services.py apps/harness/migrations/0004_turn_missed.py tests/test_harness_models.py
git commit -m "feat(harness): Turn.MISSED — 'you skipped this', distinct from LOST"
```

---

### Task 2: The `AgentSchedule` model

**Files:**
- Modify: `apps/harness/models.py`
- Create: `apps/harness/migrations/0005_agentschedule.py`
- Modify: `apps/harness/admin.py`
- Test: `tests/test_schedule_models.py`

**Interfaces:**
- Consumes: `Turn.ROUTING_CHOICES`, `Turn.PREFER_LOCAL` (Task 1's file).
- Produces: `AgentSchedule` with fields `agent`, `name`, `prompt`, `cron`, `timezone`, `enabled`, `routing`, `grace_minutes`, `notify`, `last_slot`, `created_at`, `updated_at`; property `agent_slug`; `Meta.ordering = ["name"]`; unique `(agent, name)`.

Note: **int pk** (Django default) — not UUID. See Global Constraints.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_models.py`:

```python
"""Model-level tests for AgentSchedule — the recurring-turn declaration."""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn

pytestmark = pytest.mark.django_db


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="eva", name="Eva")


def test_schedule_defaults(agent):
    s = AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="/eva:goal-review",
        cron="0 9 1 * *", timezone="America/New_York",
    )
    assert s.enabled is True
    assert s.routing == Turn.PREFER_LOCAL
    assert s.grace_minutes == 120
    assert s.notify == ["inbox"]
    assert s.last_slot is None
    assert s.agent_slug == "eva"
    assert isinstance(s.pk, int)  # NeedsYouItem.ref_id is typed int


def test_schedule_name_unique_per_agent(agent):
    AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="p", cron="0 9 1 * *",
        timezone="UTC",
    )
    with pytest.raises(IntegrityError):
        AgentSchedule.objects.create(
            agent=agent, name="Goal review", prompt="p2", cron="0 9 2 * *",
            timezone="UTC",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'AgentSchedule' from 'apps.harness.models'`

- [ ] **Step 3: Write the model**

Append to `apps/harness/models.py`:

```python
def _default_notify() -> list:
    """Callable default — a mutable literal would be shared across rows."""
    return ["inbox"]


class AgentSchedule(models.Model):
    """A recurring turn declaration — "Echo's weekly manager report, Fridays 9am ET".

    Config lives here (server-side, so it is visible and editable in the Agent
    UI); the *firing* is done by the runner, which syncs these rows, evaluates
    the cron locally, and POSTs back a slot. The server then materializes a
    normal harness Turn via services.enqueue_turn — the scheduler is a producer
    of turns, not a second execution engine.

    The Turn IS the occurrence (origin=cron, origin_ref={schedule_id, slot},
    idempotency_key="sched:<id>:<slot>"); there is deliberately no occurrence
    table. See docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md.

    int pk (not UUID like Runner/Turn): this projects into needs_you, whose
    NeedsYouItem.ref_id is typed int on a StrictModel.

    No workspace FK: a schedule is agent-owned and derives its tenant via
    agent.workspace, exactly as Turn does.
    """

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="schedules")
    name = models.CharField(max_length=200, help_text='e.g. "Weekly manager report"')
    prompt = models.TextField(help_text="What the turn is seeded with, e.g. /echo:manager-report")
    cron = models.CharField(max_length=120, help_text="5-field cron expression, e.g. '0 9 * * 5'")
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA tz, e.g. America/New_York")
    enabled = models.BooleanField(default=True, help_text="Pause without deleting.")
    routing = models.CharField(max_length=15, choices=Turn.ROUTING_CHOICES, default=Turn.PREFER_LOCAL)
    grace_minutes = models.PositiveIntegerField(
        default=120,
        help_text="How long an unattended fired turn may hold the agent before it is "
        "released as MISSED. Guards one_executing_turn_per_agent: an abandoned "
        "session would otherwise wedge the agent indefinitely.",
    )
    notify = models.JSONField(
        default=_default_notify, blank=True,
        help_text='Channel ids resolved through the notify registry, e.g. ["inbox"].',
    )
    last_slot = models.DateTimeField(
        null=True, blank=True,
        help_text="Newest slot fired. The supersede + no-backfill anchor.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["agent", "enabled"])]
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_schedule_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"sched:{self.agent.slug}:{self.name}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations harness -n agentschedule`
Expected: `Migrations for 'harness': apps/harness/migrations/0005_agentschedule.py - Create model AgentSchedule`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Register in admin**

In `apps/harness/admin.py`, add the import to the existing models import line and append:

```python
@admin.register(AgentSchedule)
class AgentScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "agent", "cron", "timezone", "enabled", "last_slot")
    list_filter = ("enabled", "agent")
    search_fields = ("name", "prompt")
```

- [ ] **Step 7: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/0005_agentschedule.py apps/harness/admin.py tests/test_schedule_models.py
git commit -m "feat(harness): AgentSchedule — recurring turn declarations, config server-side"
```

---

### Task 3: `croniter` dependency + the slot calculator

**Files:**
- Modify: `pyproject.toml`
- Create: `apps/harness/cron.py`
- Test: `tests/test_schedule_cron.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `validate_cron(expr: str) -> str` — returns the expr, raises `ValueError` on a bad one.
  - `validate_timezone(name: str) -> str` — returns the name, raises `ValueError`.
  - `due_slot(cron: str, tz: str, *, after: datetime | None, now: datetime) -> datetime | None` — the single most recent slot at or before `now` that is strictly after `after`; `None` if none. **Never backfills** — returns at most one slot.
  - `next_slots(cron: str, tz: str, *, now: datetime, count: int = 3) -> list[datetime]` — the next `count` fire times (drives the UI preview).

All returns are timezone-aware UTC datetimes.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to the main `dependencies` list (keep alphabetical order):

```toml
    "croniter>=3.0",
```

Run: `uv sync --extra dev`
Expected: `croniter` resolves and installs; `uv.lock` updates.

- [ ] **Step 2: Write the failing test**

Create `tests/test_schedule_cron.py`:

```python
"""Slot math for AgentSchedule. No DB, no Django — pure functions."""
from __future__ import annotations

import datetime as dt

import pytest

from apps.harness.cron import due_slot, next_slots, validate_cron, validate_timezone

FRIDAYS_9AM = "0 9 * * 5"
NY = "America/New_York"


def _utc(y, m, d, h, mi=0) -> dt.datetime:
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc)


def test_validate_cron_accepts_and_rejects():
    assert validate_cron(FRIDAYS_9AM) == FRIDAYS_9AM
    with pytest.raises(ValueError):
        validate_cron("not a cron")
    with pytest.raises(ValueError):
        validate_cron("")


def test_validate_timezone_accepts_and_rejects():
    assert validate_timezone(NY) == NY
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus_Mons")


def test_due_slot_none_before_first_fire():
    # 2026-07-15 is a Wednesday; the next Friday 9am ET has not happened.
    assert due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 7, 15, 12)) is None


def test_due_slot_returns_the_slot_once_passed():
    # Friday 2026-07-17 09:00 ET == 13:00 UTC (EDT, UTC-4).
    slot = due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 7, 17, 14))
    assert slot == _utc(2026, 7, 17, 13)


def test_due_slot_respects_after_no_refire():
    slot = _utc(2026, 7, 17, 13)
    assert due_slot(FRIDAYS_9AM, NY, after=slot, now=_utc(2026, 7, 17, 14)) is None


def test_due_slot_never_backfills():
    """Three weeks offline yields ONE slot — the newest — not three."""
    slot = due_slot(FRIDAYS_9AM, NY, after=_utc(2026, 6, 26, 13), now=_utc(2026, 7, 17, 14))
    assert slot == _utc(2026, 7, 17, 13)  # newest, not 2026-07-03 or 2026-07-10


def test_due_slot_dst_holds_local_9am():
    """9am ET stays 9am across the DST shift: EDT=13:00Z, EST=14:00Z."""
    edt = due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 10, 30, 20))
    assert edt == _utc(2026, 10, 30, 13)
    # 2026-11-06 is after the US shift back to EST (UTC-5).
    est = due_slot(FRIDAYS_9AM, NY, after=_utc(2026, 10, 30, 13), now=_utc(2026, 11, 6, 20))
    assert est == _utc(2026, 11, 6, 14)


def test_next_slots_previews_three():
    out = next_slots(FRIDAYS_9AM, NY, now=_utc(2026, 7, 15, 12), count=3)
    assert out == [_utc(2026, 7, 17, 13), _utc(2026, 7, 24, 13), _utc(2026, 7, 31, 13)]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_cron.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.harness.cron'`

- [ ] **Step 4: Write the implementation**

Create `apps/harness/cron.py`:

```python
"""Slot math for AgentSchedule — cron parsing, validation, and due-slot lookup.

Pure functions, no Django models, so they are cheap to test exhaustively (DST is
the part that bites). All datetimes in and out are timezone-aware UTC; the local
wall-clock interpretation happens inside, against the schedule's IANA zone.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def validate_cron(expr: str) -> str:
    """Return `expr` unchanged, or raise ValueError. A cron typo that silently
    never fires is the worst failure mode a scheduler has — reject at edit time."""
    expr = (expr or "").strip()
    if not expr:
        raise ValueError("cron expression is required")
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expression: {expr!r}")
    return expr


def validate_timezone(name: str) -> str:
    """Return `name` unchanged, or raise ValueError if it is not an IANA zone."""
    name = (name or "").strip()
    if not name:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid timezone: {name!r}") from exc
    return name


def due_slot(
    cron: str, tz: str, *, after: dt.datetime | None, now: dt.datetime
) -> dt.datetime | None:
    """The most recent slot at or before `now` that is strictly after `after`.

    Returns AT MOST ONE slot — never a backfill. Three weeks offline yields the
    newest occurrence only, which is the supersede rule applied at firing time:
    you only ever owe the latest goal review.
    """
    zone = ZoneInfo(validate_timezone(tz))
    local_now = now.astimezone(zone)
    # Walk backwards from now: the first previous fire time IS the newest slot.
    prev = croniter(validate_cron(cron), local_now).get_prev(dt.datetime)
    slot = prev.astimezone(dt.timezone.utc)
    if after is not None and slot <= after:
        return None
    return slot


def next_slots(cron: str, tz: str, *, now: dt.datetime, count: int = 3) -> list[dt.datetime]:
    """The next `count` fire times after `now` — drives the UI's preview, which is
    what makes a raw cron expression trustworthy without a docs trip."""
    zone = ZoneInfo(validate_timezone(tz))
    itr = croniter(validate_cron(cron), now.astimezone(zone))
    return [itr.get_next(dt.datetime).astimezone(dt.timezone.utc) for _ in range(count)]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_cron.py -v`
Expected: PASS (8 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock apps/harness/cron.py tests/test_schedule_cron.py
git commit -m "feat(harness): cron slot math — validation, due_slot (no backfill), DST-correct"
```

---

### Task 4: `fire_schedule` + `release_stale_cron_turns` services

**Files:**
- Modify: `apps/harness/services.py`
- Test: `tests/test_schedule_services.py`

**Interfaces:**
- Consumes: `enqueue_turn`, `finish_turn` (Task 1), `AgentSchedule` (Task 2).
- Produces:
  - `fire_schedule(schedule: AgentSchedule, slot: datetime) -> tuple[Turn, bool]` — supersedes the prior unfinished cron turn, enqueues the new one, advances `last_slot`. `bool` is `created`.
  - `run_schedule_now(schedule: AgentSchedule) -> Turn` — manual trigger; `origin="manual"`, never collides with nor satisfies a real slot.
  - `release_stale_cron_turns(schedule: AgentSchedule, *, now: datetime | None = None) -> int` — releases turns held past `grace_minutes`.
  - `latest_occurrence_turn(schedule: AgentSchedule) -> Turn | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_services.py`:

```python
"""fire_schedule / release_stale_cron_turns — supersede, idempotency, unwedging."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import AgentSchedule, Turn

pytestmark = pytest.mark.django_db

SLOT_A = dt.datetime(2026, 7, 10, 13, tzinfo=dt.timezone.utc)
SLOT_B = dt.datetime(2026, 7, 17, 13, tzinfo=dt.timezone.utc)


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="echo", name="Echo")


@pytest.fixture()
def schedule(agent):
    return AgentSchedule.objects.create(
        agent=agent, name="Weekly manager report", prompt="/echo:manager-report",
        cron="0 9 * * 5", timezone="America/New_York",
    )


def test_fire_creates_a_cron_turn_and_advances_last_slot(schedule):
    turn, created = services.fire_schedule(schedule, SLOT_A)

    assert created is True
    assert turn.origin == Turn.ORIGIN_CRON
    assert turn.status == Turn.QUEUED
    assert turn.prompt == "/echo:manager-report"
    assert turn.origin_ref == {"schedule_id": schedule.id, "slot": SLOT_A.isoformat()}
    assert turn.idempotency_key == f"sched:{schedule.id}:{SLOT_A.isoformat()}"
    schedule.refresh_from_db()
    assert schedule.last_slot == SLOT_A


def test_fire_is_idempotent_two_runners_one_turn(schedule):
    """Both macOS-account runners may fire the same slot. Exactly one turn."""
    first, created_1 = services.fire_schedule(schedule, SLOT_A)
    second, created_2 = services.fire_schedule(schedule, SLOT_A)

    assert created_1 is True
    assert created_2 is False
    assert first.id == second.id
    assert Turn.objects.filter(origin=Turn.ORIGIN_CRON).count() == 1


def test_fire_supersedes_the_prior_unfinished_turn(schedule):
    old, _ = services.fire_schedule(schedule, SLOT_A)

    services.fire_schedule(schedule, SLOT_B)

    old.refresh_from_db()
    assert old.status == Turn.MISSED
    assert "superseded" in old.result_note
    assert Turn.objects.filter(status=Turn.QUEUED).count() == 1  # only the newest is owed


def test_fire_does_not_touch_a_finished_turn(schedule):
    done, _ = services.fire_schedule(schedule, SLOT_A)
    services.finish_turn(done, status=Turn.DONE)

    services.fire_schedule(schedule, SLOT_B)

    done.refresh_from_db()
    assert done.status == Turn.DONE  # not rewritten to missed


def test_release_stale_unwedges_the_agent(schedule, agent):
    """The one_executing_turn_per_agent finding: an abandoned session must not
    block the agent's next turn forever."""
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    # created_at is the grace anchor — it is when the slot fired, which is when
    # the window to engage starts. auto_now_add only applies on insert, so a
    # queryset .update() can backdate it.
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.RUNNING, created_at=timezone.now() - dt.timedelta(minutes=200)
    )

    released = services.release_stale_cron_turns(schedule)

    assert released == 1
    turn.refresh_from_db()
    assert turn.status == Turn.MISSED
    # Proof it is unwedged: a new executing turn is now insertable.
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="board-1", status=Turn.RUNNING
    )


def test_release_spares_a_turn_inside_its_grace(schedule):
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.RUNNING, created_at=timezone.now() - dt.timedelta(minutes=5)
    )

    assert services.release_stale_cron_turns(schedule) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.RUNNING


def test_run_now_never_satisfies_a_real_slot(schedule):
    manual = services.run_schedule_now(schedule)

    assert manual.origin == Turn.ORIGIN_MANUAL
    assert manual.idempotency_key.startswith(f"sched:{schedule.id}:manual:")
    schedule.refresh_from_db()
    assert schedule.last_slot is None  # a manual run does not consume a slot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_services.py -v`
Expected: FAIL — `AttributeError: module 'apps.harness.services' has no attribute 'fire_schedule'`

- [ ] **Step 3: Write the implementation**

Append to `apps/harness/services.py`:

```python
# --------------------------------------------------------------------------------------
# AgentSchedule — recurring turns. The runner evaluates the cron and calls fire_schedule;
# the server materializes a normal Turn. See models.AgentSchedule.
# --------------------------------------------------------------------------------------

def latest_occurrence_turn(schedule) -> Turn | None:
    """The newest turn this schedule produced, whatever its status.

    AS SHIPPED (Task 4, commit 24ade53) this selects by `origin_ref__schedule_id`
    regardless of origin, NOT by `origin=cron`. A 'Run now' writes origin=manual,
    and an origin=cron filter meant completing one could never clear the nag it
    was launched from. Read apps/harness/services.py for the current source.
    """
    return (
        Turn.objects.filter(
            agent_id=schedule.agent_id,
            origin=Turn.ORIGIN_CRON,
            origin_ref__schedule_id=schedule.id,
        )
        .order_by("-created_at")
        .first()
    )


def _supersede_open_turns(schedule, *, reason: str) -> int:
    """Terminate this schedule's non-terminal turns as MISSED. Supersede and
    grace-release are the same operation at two timescales."""
    open_turns = Turn.objects.filter(
        agent_id=schedule.agent_id,
        origin=Turn.ORIGIN_CRON,
        origin_ref__schedule_id=schedule.id,
        status__in=list(Turn.NON_TERMINAL),
    )
    count = 0
    for turn in open_turns:
        finish_turn(turn, status=Turn.MISSED, result_note=reason)
        count += 1
    return count


def fire_schedule(schedule, slot: dt.datetime) -> tuple[Turn, bool]:
    """Materialize `slot` as a queued Turn. Supersedes any still-open occurrence
    of the same schedule first — you only ever owe the newest.

    Safe to call concurrently from both macOS-account runners: the slot-derived
    idempotency_key collapses the race inside enqueue_turn.
    """
    key = f"sched:{schedule.id}:{slot.isoformat()}"
    with transaction.atomic():
        if not Turn.objects.filter(idempotency_key=key).exists():
            _supersede_open_turns(schedule, reason=f"superseded by slot {slot.isoformat()}")
        turn, created = enqueue_turn(
            agent=schedule.agent,
            origin=Turn.ORIGIN_CRON,
            idempotency_key=key,
            prompt=schedule.prompt,
            origin_ref={"schedule_id": schedule.id, "slot": slot.isoformat()},
            routing=schedule.routing,
        )
        if created and (schedule.last_slot is None or slot > schedule.last_slot):
            schedule.last_slot = slot
            schedule.save(update_fields=["last_slot", "updated_at"])
    return turn, created


def run_schedule_now(schedule) -> Turn:
    """Manual off-cycle trigger. origin=manual with a uuid-suffixed key, so an
    ad-hoc run never collides with — nor satisfies — a real slot, and last_slot
    is untouched."""
    turn, _ = enqueue_turn(
        agent=schedule.agent,
        origin=Turn.ORIGIN_MANUAL,
        idempotency_key=f"sched:{schedule.id}:manual:{uuid.uuid4()}",
        prompt=schedule.prompt,
        origin_ref={"schedule_id": schedule.id, "manual": True},
        routing=schedule.routing,
    )
    return turn


def release_stale_cron_turns(schedule, *, now: dt.datetime | None = None) -> int:
    """Release turns this schedule fired that a human has held past grace_minutes.

    This is what keeps a forgotten session from wedging the agent: an executing
    turn holds one_executing_turn_per_agent, and the runner's heartbeat keeps
    renewing its lease for as long as the emdash session is open, so the ordinary
    lease sweep never rescues it.
    """
    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=schedule.grace_minutes)
    stale = Turn.objects.filter(
        agent_id=schedule.agent_id,
        origin=Turn.ORIGIN_CRON,
        origin_ref__schedule_id=schedule.id,
        status__in=list(Turn.NON_TERMINAL),
        created_at__lt=cutoff,
    )
    count = 0
    for turn in stale:
        finish_turn(
            turn, status=Turn.MISSED,
            result_note=f"released after {schedule.grace_minutes}m unattended",
        )
        count += 1
    return count
```

Add `import uuid` to the top-level imports of `apps/harness/services.py` (after `import logging`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_services.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Run the whole harness suite for regressions**

Run: `uv run pytest tests/test_harness_api.py tests/test_harness_models.py tests/test_schedule_services.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add apps/harness/services.py tests/test_schedule_services.py
git commit -m "feat(harness): fire_schedule + grace release — supersede is the give-up"
```

---

### Task 5: Schemas — with cron validation at the boundary

**Files:**
- Modify: `apps/harness/schemas.py`
- Test: `tests/test_schedule_schemas.py`

**Interfaces:**
- Consumes: `validate_cron`, `validate_timezone`, `next_slots` (Task 3).
- Produces: `ScheduleIn`, `SchedulePatch`, `ScheduleOut`, `ScheduleFireIn`.

Validation is a **field validator**, not handler code: it then 422s as real `problem+json` through the existing `ValidationError` handler in `apps/api/api.py`, with no per-route work.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_schemas.py`:

```python
"""Cron/timezone validation happens in the schema, so it 422s at the boundary."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.harness.schemas import ScheduleIn


def _payload(**over):
    base = dict(
        name="Weekly manager report", prompt="/echo:manager-report",
        cron="0 9 * * 5", timezone="America/New_York",
    )
    base.update(over)
    return base


def test_valid_payload():
    s = ScheduleIn(**_payload())
    assert s.cron == "0 9 * * 5"
    assert s.grace_minutes == 120
    assert s.notify == ["inbox"]


def test_bad_cron_rejected():
    with pytest.raises(ValidationError):
        ScheduleIn(**_payload(cron="every friday please"))


def test_bad_timezone_rejected():
    with pytest.raises(ValidationError):
        ScheduleIn(**_payload(timezone="Mars/Olympus_Mons"))


def test_blank_prompt_rejected():
    with pytest.raises(ValidationError):
        ScheduleIn(**_payload(prompt="   "))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_schemas.py -v`
Expected: FAIL — `ImportError: cannot import name 'ScheduleIn' from 'apps.harness.schemas'`

- [ ] **Step 3: Write the schemas**

Append to `apps/harness/schemas.py` (match the file's existing base-class convention — if its schemas subclass `Schema` from `ninja`, use that; the code below assumes `ninja.Schema`, already imported there):

```python
import datetime as dt

from pydantic import field_validator

from .cron import next_slots, validate_cron, validate_timezone


class ScheduleIn(Schema):
    """Create payload. Cron + tz validate here so a bad expression 422s as
    problem+json at edit time — a typo that silently never fires is the worst
    failure mode a scheduler has."""

    name: str
    prompt: str
    cron: str
    timezone: str = "UTC"
    enabled: bool = True
    routing: str = "prefer_local"
    grace_minutes: int = 120
    notify: list[str] = ["inbox"]

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("name", "prompt")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("must not be blank")
        return v.strip()


class SchedulePatch(Schema):
    """Partial update. Every field optional; the same validators apply to any
    field actually supplied."""

    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    routing: str | None = None
    grace_minutes: int | None = None
    notify: list[str] | None = None

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v is not None else v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        return validate_timezone(v) if v is not None else v


class ScheduleOut(Schema):
    id: int
    agent_slug: str
    name: str
    prompt: str
    cron: str
    timezone: str
    enabled: bool
    routing: str
    grace_minutes: int
    notify: list[str]
    last_slot: dt.datetime | None = None
    # The anchor the runner MUST pass as due_slot(after=...). Server-computed as
    # `last_slot or created_at` so the runner cannot get the fallback wrong.
    # Without it a fresh schedule (last_slot=None) fires once for the slot BEFORE
    # it existed — a schedule created Wednesday would immediately owe last
    # Friday's report. See the runner-side section.
    fire_after: dt.datetime
    next_runs: list[dt.datetime] = []
    last_status: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime


class ScheduleFireIn(Schema):
    """The runner's report that a slot came due. The server re-derives nothing —
    but the slot is only honored as an idempotency anchor, never as a claim of
    authority: tenant scoping gates the route."""

    slot: dt.datetime
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_schemas.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/harness/schemas.py tests/test_schedule_schemas.py
git commit -m "feat(harness): schedule schemas — cron validated at the API boundary"
```

---

### Task 6: The human CRUD router (`/api/agents/{slug}/schedules/`)

**Files:**
- Create: `apps/harness/api_schedules.py`
- Modify: `apps/api/api.py`
- Test: `tests/test_schedule_api.py`

**Interfaces:**
- Consumes: `_agent_or_404` from `apps/harness/api.py`; `ScheduleIn`/`SchedulePatch`/`ScheduleOut` (Task 5); `run_schedule_now`, `latest_occurrence_turn` (Task 4); `next_slots` (Task 3).
- Produces: `schedules_router` (importable as `from apps.harness.api_schedules import router as schedules_router`); `_serialize(schedule) -> ScheduleOut`.

A separate module from `api.py` because the two have different audiences and different auth reasoning — human CRUD vs the machine control plane. `api.py` is already 260+ lines.

**Note on `_agent_or_404`:** the harness-local helper exists on the concurrent `emdash/mobile-l2vmm` branch (commit 43f61ae), **not on `main`**. If it is absent, add it to `apps/harness/api.py` verbatim from `apps/agents/api.py:42`, adapted to take `(request, slug)` — do not import across api modules, and do not re-roll the check.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_api.py`:

```python
"""API tests for /api/agents/{slug}/schedules — human CRUD + run-now."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn

pytestmark = pytest.mark.django_db


@pytest.fixture()
def client():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="echo", name="Echo")


def _create(client, **over):
    body = {
        "name": "Weekly manager report", "prompt": "/echo:manager-report",
        "cron": "0 9 * * 5", "timezone": "America/New_York",
    }
    body.update(over)
    return client.post(
        "/api/agents/echo/schedules/", body, content_type="application/json"
    )


def test_create_and_list(client, agent):
    resp = _create(client)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["agent_slug"] == "echo"
    assert body["cron"] == "0 9 * * 5"
    assert len(body["next_runs"]) == 3  # the UI preview

    listing = client.get("/api/agents/echo/schedules/")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1


def test_bad_cron_is_422_problem_json(client, agent):
    resp = _create(client, cron="every friday please")
    assert resp.status_code == 422
    assert resp["content-type"] == "application/problem+json"


def test_patch_toggles_enabled(client, agent):
    sid = _create(client).json()["id"]
    resp = client.patch(
        f"/api/agents/echo/schedules/{sid}", {"enabled": False},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_patch_with_explicit_null_does_not_500(client, agent):
    """An explicit {"cron": null} must not setattr None onto a non-nullable
    column. SchedulePatch's validator short-circuits on None, so this can only
    be caught here."""
    sid = _create(client).json()["id"]
    resp = client.patch(
        f"/api/agents/echo/schedules/{sid}", {"cron": None},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["cron"] == "0 9 * * 5"  # unchanged, not nulled


def test_delete(client, agent):
    sid = _create(client).json()["id"]
    assert client.delete(f"/api/agents/echo/schedules/{sid}").status_code == 204
    assert AgentSchedule.objects.count() == 0


def test_run_now_enqueues_a_manual_turn(client, agent):
    sid = _create(client).json()["id"]
    resp = client.post(f"/api/agents/echo/schedules/{sid}/run-now")
    assert resp.status_code == 202, resp.content
    turn = Turn.objects.get()
    assert turn.origin == Turn.ORIGIN_MANUAL
    assert turn.prompt == "/echo:manager-report"


def test_unknown_agent_404s(client):
    assert client.get("/api/agents/nope/schedules/").status_code == 404


def test_fire_after_defaults_to_created_at_not_null(client, agent):
    """A fresh schedule must never fire for a slot that predates it.

    last_slot is NULL until the first fire, and due_slot(after=None) looks
    backward with no lower bound — so a schedule created Wednesday would
    immediately owe LAST Friday's report. fire_after is the server-computed
    anchor that closes that hole; the runner passes it straight to due_slot.
    """
    body = _create(client).json()
    schedule = AgentSchedule.objects.get(pk=body["id"])

    assert body["last_slot"] is None
    assert body["fire_after"] == schedule.created_at.isoformat().replace("+00:00", "Z")


def test_fire_after_tracks_last_slot_once_fired(client, agent):
    from apps.harness import services

    schedule = AgentSchedule.objects.get(pk=_create(client).json()["id"])
    slot = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC)
    services.fire_schedule(schedule, slot)

    body = client.get("/api/agents/echo/schedules/").json()["items"][0]

    assert body["fire_after"] == slot.isoformat().replace("+00:00", "Z")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_api.py -v`
Expected: FAIL — 404 on every route (the router is not mounted).

- [ ] **Step 3: Write the router**

Create `apps/harness/api_schedules.py`:

```python
"""Django Ninja router for /api/agents/{slug}/schedules — the human-facing CRUD
for recurring turns.

Deliberately separate from api.py: that module is the machine control plane
(runner pairing, claim, lease), this one is the supervisor's editing surface.
Mounted on the /agents namespace exactly as agent_runs already is, so the
tenant path /api/w/{ws}/agents/... works via WorkspaceResolveMiddleware.
"""
from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import Page, paginate

from . import services
from .api import _agent_or_404
from .cron import next_slots
from .models import AgentSchedule
from .schemas import ScheduleIn, ScheduleOut, SchedulePatch

router = Router(auth=session_auth, tags=["schedules"])


def _serialize(schedule: AgentSchedule) -> ScheduleOut:
    latest = services.latest_occurrence_turn(schedule)
    return ScheduleOut(
        id=schedule.id,
        agent_slug=schedule.agent_slug,
        name=schedule.name,
        prompt=schedule.prompt,
        cron=schedule.cron,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
        routing=schedule.routing,
        grace_minutes=schedule.grace_minutes,
        notify=schedule.notify,
        last_slot=schedule.last_slot,
        # last_slot is NULL until the first fire. Falling back to created_at is
        # what stops a fresh schedule from firing for a slot that predates it.
        fire_after=schedule.last_slot or schedule.created_at,
        next_runs=next_slots(schedule.cron, schedule.timezone, now=timezone.now(), count=3),
        last_status=latest.status if latest else "",
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def _schedule_or_404(request: HttpRequest, slug: str, schedule_id: int) -> AgentSchedule:
    agent = _agent_or_404(request, slug)
    schedule = AgentSchedule.objects.filter(pk=schedule_id, agent=agent).first()
    if schedule is None:
        raise HttpError(404, f"schedule {schedule_id} not found")
    return schedule


@router.get("/{slug}/schedules/", response=Page[ScheduleOut],
            summary="List an agent's recurring schedules",
            openapi_extra={"x-mcp-expose": True})
def list_schedules(request: HttpRequest, slug: str, limit: int = 100) -> Page[ScheduleOut]:
    agent = _agent_or_404(request, slug)
    limit = min(limit, 500)
    items = [_serialize(s) for s in agent.schedules.all()]
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/schedules/", response={201: ScheduleOut},
             summary="Create a recurring schedule",
             openapi_extra={"x-mcp-expose": True})
def create_schedule(request: HttpRequest, slug: str, payload: ScheduleIn) -> Status:
    agent = _agent_or_404(request, slug)
    schedule = AgentSchedule.objects.create(agent=agent, **payload.dict())
    return 201, _serialize(schedule)


@router.patch("/{slug}/schedules/{schedule_id}", response=ScheduleOut,
              summary="Update a recurring schedule",
              openapi_extra={"x-mcp-expose": True})
def update_schedule(
    request: HttpRequest, slug: str, schedule_id: int, payload: SchedulePatch
) -> ScheduleOut:
    schedule = _schedule_or_404(request, slug, schedule_id)
    # exclude_none as well as exclude_unset: SchedulePatch's validators are
    # `validate_cron(v) if v is not None else v`, so an explicit {"cron": null}
    # slips past validation AND counts as "set" — setattr'ing None onto a
    # non-nullable column would 500 where a 422 belongs. No AgentSchedule field
    # is nullable via PATCH, so dropping None is always the right reading.
    fields = payload.dict(exclude_unset=True, exclude_none=True)
    for key, value in fields.items():
        setattr(schedule, key, value)
    if fields:
        schedule.save()
    return _serialize(schedule)


@router.delete("/{slug}/schedules/{schedule_id}", response={204: None},
               summary="Delete a recurring schedule",
               openapi_extra={"x-mcp-expose": True})
def delete_schedule(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    schedule = _schedule_or_404(request, slug, schedule_id)
    schedule.delete()
    return 204, None


@router.post("/{slug}/schedules/{schedule_id}/run-now", response={202: ScheduleOut},
             summary="Trigger a schedule off-cycle, now",
             openapi_extra={"x-mcp-expose": True})
def run_now(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    schedule = _schedule_or_404(request, slug, schedule_id)
    services.run_schedule_now(schedule)
    return 202, _serialize(schedule)
```

- [ ] **Step 4: Mount the router**

In `apps/api/api.py`, add to the import block at the bottom (after the `harness_router` import):

```python
from apps.harness.api_schedules import router as schedules_router  # noqa: E402
```

and add the mount immediately after `api.add_router("/agents", agent_runs_router)`:

```python
api.add_router("/agents", schedules_router)  # recurring turns, under the agents namespace
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_api.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api_schedules.py apps/api/api.py tests/test_schedule_api.py
git commit -m "feat(harness): /api/agents/{slug}/schedules — human CRUD + run-now"
```

---

### Task 6a: The cron preview route

**Files:**
- Modify: `apps/harness/api_schedules.py`
- Modify: `apps/harness/schemas.py`
- Test: `tests/test_schedule_api.py`

**Interfaces:**
- Consumes: `next_slots` (Task 3); `_agent_or_404`.
- Produces: `POST /api/agents/{slug}/schedules/preview` → `SchedulePreviewOut{next_runs: list[datetime]}`.

The editor must preview a cron the user is **still typing** — one that has no row
yet — so `ScheduleOut.next_runs` cannot serve it. The preview is computed
server-side by the same `next_slots()` the firing path uses: a second cron
implementation in TypeScript could say "Fridays" while the server fires
Thursdays, which is exactly what the preview exists to prevent.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schedule_api.py`:

```python
def test_preview_returns_three_fire_times(client, agent):
    resp = client.post(
        "/api/agents/echo/schedules/preview",
        {"cron": "0 9 * * 5", "timezone": "America/New_York"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert len(resp.json()["next_runs"]) == 3


def test_preview_rejects_a_bad_cron_without_saving_anything(client, agent):
    resp = client.post(
        "/api/agents/echo/schedules/preview",
        {"cron": "every friday please", "timezone": "UTC"},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert resp["content-type"] == "application/problem+json"
    assert AgentSchedule.objects.count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_api.py::test_preview_returns_three_fire_times -v`
Expected: FAIL — 404 (route does not exist).

- [ ] **Step 3: Add the schemas**

Append to `apps/harness/schemas.py`:

```python
class SchedulePreviewIn(Schema):
    """Preview a cron the user is still typing — no row exists yet."""

    cron: str
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)


class SchedulePreviewOut(Schema):
    next_runs: list[dt.datetime]
```

- [ ] **Step 4: Add the route**

In `apps/harness/api_schedules.py`, add `SchedulePreviewIn, SchedulePreviewOut` to the
`.schemas` import, and append:

```python
@router.post("/{slug}/schedules/preview", response=SchedulePreviewOut,
             summary="Preview the next fire times for a cron expression",
             openapi_extra={"x-mcp-expose": True})
def preview_schedule(
    request: HttpRequest, slug: str, payload: SchedulePreviewIn
) -> SchedulePreviewOut:
    """Answer 'when would this actually run?' at edit time. Computed with the same
    next_slots() the firing path uses — the client must never re-implement cron."""
    _agent_or_404(request, slug)
    return SchedulePreviewOut(
        next_runs=next_slots(payload.cron, payload.timezone, now=timezone.now(), count=3)
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_api.py -v`
Expected: PASS (8 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api_schedules.py apps/harness/schemas.py tests/test_schedule_api.py
git commit -m "feat(harness): cron preview route — verify the expression at edit time"
```

---

### Task 7: The runner-facing sync + fire routes — tenant-scoped

**Files:**
- Modify: `apps/harness/api.py`
- Test: `tests/test_schedule_authz.py`

**Interfaces:**
- Consumes: `_runner_or_404` (existing, `apps/harness/api.py`); `fire_schedule`, `release_stale_cron_turns` (Task 4); `ScheduleOut`, `ScheduleFireIn` (Task 5); `_serialize` (Task 6).
- Produces: `GET /api/harness/schedules/`, `POST /api/harness/schedules/{schedule_id}/fire`.

**This is the security-critical task.** Commit b4f5ead (*Critical*) established that `Runner.capabilities` is caller-supplied at pairing and never validated — scoping by `runner.agent_slugs()` would let any authenticated user pair a runner declaring another tenant's agent and read its schedules (leaking `prompt`) or fire its turns. The workspace is the boundary; capabilities is a hint. **They intersect; one never substitutes for the other.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_authz.py`:

```python
"""The b4f5ead regression: capabilities is a routing hint, NOT a security boundary.

A runner paired by an outsider, declaring a victim agent's slug, must see zero
of that agent's schedules and must not be able to fire them.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Runner, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SLOT = dt.datetime(2026, 7, 17, 13, tzinfo=dt.timezone.utc)


@pytest.fixture()
def victim_ws():
    # auto_join_domains=[] is load-bearing: both users below are @dimagi.com, and
    # a domain auto-join would silently make the attacker a member — the test
    # would pass while testing nothing.
    return Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", auto_join_domains=[]
    )


@pytest.fixture()
def attacker_ws():
    return Workspace.objects.create(
        slug="evilcorp", display_name="Evil Corp", auto_join_domains=[]
    )


@pytest.fixture()
def victim_agent(victim_ws):
    return Agent.objects.create(slug="echo", name="Echo", workspace=victim_ws)


@pytest.fixture()
def victim_schedule(victim_agent):
    return AgentSchedule.objects.create(
        agent=victim_agent, name="Weekly manager report",
        prompt="/echo:manager-report — CONFIDENTIAL",
        cron="0 9 * * 5", timezone="America/New_York",
    )


@pytest.fixture()
def attacker_client(attacker_ws, victim_ws):
    user = User.objects.create_user("mallory", "mallory@dimagi.com", "pw")
    wsvc.ensure_member(attacker_ws, user, WorkspaceMembership.OWNER)
    assert not wsvc.is_member(user, victim_ws.slug)  # guard: the test must mean something
    c = Client()
    c.force_login(user)
    return c


def _pair_and_online(client) -> str:
    """Pair a runner DECLARING THE VICTIM'S AGENT SLUG, then heartbeat it online.

    The heartbeat matters: without it the runner is not ONLINE and the paths
    short-circuit, so the test would pass while proving nothing (b4f5ead's
    message notes the old claim-authz test had exactly that hole).

    No workspace is set on the Runner — tenancy derives from paired_by, which the
    server assigns from request.user at pairing.
    """
    resp = client.post(
        "/api/harness/runners/",
        {"name": "evil-box", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    rid = resp.json()["id"]
    hb = client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200, hb.content
    assert Runner.objects.get(pk=rid).status == Runner.ONLINE
    return rid


def test_cross_tenant_sync_leaks_nothing(attacker_client, attacker_ws, victim_schedule):
    rid = _pair_and_online(attacker_client)

    resp = attacker_client.get(f"/api/harness/schedules/?runner_id={rid}")

    assert resp.status_code == 200
    assert resp.json()["items"] == []  # capabilities claimed echo; tenancy denied it


def test_cross_tenant_fire_404s(attacker_client, attacker_ws, victim_schedule):
    rid = _pair_and_online(attacker_client)

    resp = attacker_client.post(
        f"/api/harness/schedules/{victim_schedule.id}/fire?runner_id={rid}",
        {"slot": SLOT.isoformat()},
        content_type="application/json",
    )

    assert resp.status_code == 404  # 404 not 403 — no existence leak
    assert Turn.objects.count() == 0  # and no turn was materialized


def test_same_tenant_runner_syncs_and_fires(victim_ws, victim_schedule):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(victim_ws, user, WorkspaceMembership.OWNER)
    client = Client()
    client.force_login(user)
    rid = _pair_and_online(client)

    listing = client.get(f"/api/harness/schedules/?runner_id={rid}")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    fired = client.post(
        f"/api/harness/schedules/{victim_schedule.id}/fire?runner_id={rid}",
        {"slot": SLOT.isoformat()},
        content_type="application/json",
    )
    assert fired.status_code == 201, fired.content
    assert Turn.objects.get().origin == Turn.ORIGIN_CRON
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_authz.py -v`
Expected: FAIL — 404s on `/api/harness/schedules/` (routes don't exist).

- [ ] **Step 3: Write the routes**

Append to `apps/harness/api.py`:

```python
def _runner_schedule_qs(runner: Runner):
    """Schedules this runner may see, gated by TENANT — never by capabilities.

    capabilities is a caller-supplied routing hint declared at pairing and never
    validated (see b4f5ead, Critical): scoping by it would let anyone pair a
    runner declaring a victim's agent slug and read that agent's schedules,
    leaking `prompt`. The workspace is the boundary.

    The tenant is derived from `paired_by` — the human who paired the runner —
    rather than a Runner.workspace field, deliberately:
      * Runner.workspace does not exist on main; it is added by the concurrent
        canopy-mobile branch (0004_runner_workspace). Adding it here would
        collide with that migration for no gain.
      * paired_by is server-assigned at pairing (request.user), so unlike
        capabilities it is not attacker-controlled — which is the whole point.
    When Runner.workspace lands, this may narrow to it; the predicate below is
    the conservative superset of that rule, never a wider one.
    """
    from apps.workspaces import services as wsvc

    from .models import AgentSchedule

    qs = AgentSchedule.objects.filter(enabled=True).select_related("agent")
    if runner.paired_by_id is None:
        return qs.none()  # an orphaned runner has no identity to derive tenancy from
    slugs = wsvc.user_workspace_slugs(runner.paired_by)
    # Same-tenant agents, or legacy null-workspace agents (the pre-tenancy path
    # the existing suite covers). Mirrors claim_next_turn's Q-based predicate.
    return qs.filter(Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True))


@router.get("/schedules/", response=Page[ScheduleOut],
            summary="Schedules this runner may fire (tenant-scoped)")
def sync_schedules(request: HttpRequest, runner_id: uuid.UUID, limit: int = 200) -> Page[ScheduleOut]:
    """The runner's schedule sync. It caches these locally, evaluates the cron
    itself, and POSTs /fire when a slot comes due."""
    from .api_schedules import _serialize

    runner = _runner_or_404(runner_id)
    items = [_serialize(s) for s in _runner_schedule_qs(runner)]
    return paginate(items, offset=0, limit=min(limit, 500))


@router.post("/schedules/{schedule_id}/fire", response={201: TurnOut},
             summary="Report a due slot; the server materializes the turn")
def fire_schedule_route(
    request: HttpRequest, schedule_id: int, runner_id: uuid.UUID, payload: ScheduleFireIn
) -> Status:
    runner = _runner_or_404(runner_id)
    schedule = _runner_schedule_qs(runner).filter(pk=schedule_id).first()
    if schedule is None:
        # 404 whether it is missing, disabled, or another tenant's — no existence leak.
        raise HttpError(404, f"schedule {schedule_id} not found")
    # Deliberately does NOT call release_stale_cron_turns: fire_schedule already
    # supersedes every open occurrence, so release would add nothing here except
    # a self-destruct on same-slot re-fire (fire skips supersede when the key
    # exists, but release would already have killed the turn this route returns).
    # Release runs on the CLAIM tick instead — see claim_next_turn.
    turn, _ = services.fire_schedule(schedule, payload.slot)
    return 201, turn
```

Add to the imports at the top of `apps/harness/api.py`:

```python
from apps.api.pagination import Page, paginate
```

and add `ScheduleFireIn, ScheduleOut` to the existing `from .schemas import (...)` block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_authz.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full backend suite**

Run: `uv run pytest`
Expected: PASS (all) — including `tests/test_architecture_boundary.py`.

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api.py tests/test_schedule_authz.py
git commit -m "feat(harness): tenant-scoped schedule sync + fire (capabilities is not a boundary)"
```

---

### Task 8: The nag — a `needs_you` projection + the notify registry

**Files:**
- Create: `apps/harness/notify.py`
- Modify: `apps/agents/services.py` (`needs_you`)
- Modify: `apps/agents/schemas.py` (`NeedsYouItem.ref_kind`)
- Test: `tests/test_schedule_nag.py`

**Interfaces:**
- Consumes: `latest_occurrence_turn` (Task 4), `AgentSchedule` (Task 2).
- Produces: `apps.harness.notify.CHANNELS: dict[str, Callable]`; `schedule_nag_items(agent) -> list[dict]`.

Hooking the **per-agent** `needs_you()` is what makes this reach every surface: the fleet-wide `GET /agents/needs-you` calls it per agent, so `/supervisor`, `total_waiting`, and the mobile PWA all light up with no extra work.

**Boundary note:** `apps/agents` importing `apps.harness` — both are framework tier, so this does not violate `test_architecture_boundary.py`. Import **inside the function**, not at module load: `apps.harness.models` imports `apps.agents.models`, so a module-level import would cycle. (Same reason `AgentTask.run` uses a string FK ref.)

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_nag.py`:

```python
"""The nag: an unfinished scheduled occurrence projects into needs_you."""
from __future__ import annotations

import datetime as dt

import pytest

from apps.agents import services as asvc
from apps.agents.models import Agent
from apps.harness import services as hsvc
from apps.harness.models import AgentSchedule, Turn

pytestmark = pytest.mark.django_db

SLOT = dt.datetime(2026, 7, 17, 13, tzinfo=dt.timezone.utc)


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="eva", name="Eva")


@pytest.fixture()
def schedule(agent):
    return AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="/eva:goal-review",
        cron="0 9 1 * *", timezone="America/New_York",
    )


def test_never_fired_schedule_does_not_nag(agent, schedule):
    out = asvc.needs_you(agent)
    assert out["waiting_count"] == 0


def test_unfinished_occurrence_nags(agent, schedule):
    hsvc.fire_schedule(schedule, SLOT)

    out = asvc.needs_you(agent)

    items = [i for i in out["items"] if i["ref_kind"] == "schedule"]
    assert len(items) == 1
    assert items[0]["type"] == "review"
    assert items[0]["ref_id"] == schedule.id
    assert items[0]["title"] == "Goal review"
    assert out["waiting_count"] == 1  # counts toward the 'N waiting on you' badge


def test_finished_occurrence_clears_the_nag(agent, schedule):
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    hsvc.finish_turn(turn, status=Turn.DONE)

    out = asvc.needs_you(agent)

    assert [i for i in out["items"] if i["ref_kind"] == "schedule"] == []
    assert out["waiting_count"] == 0


def test_missed_occurrence_still_nags_until_superseded(agent, schedule):
    """Released-as-missed is not done: you still owe it until the next slot."""
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    hsvc.finish_turn(turn, status=Turn.MISSED, result_note="released")

    out = asvc.needs_you(agent)

    assert len([i for i in out["items"] if i["ref_kind"] == "schedule"]) == 1


def test_disabled_schedule_does_not_nag(agent, schedule):
    hsvc.fire_schedule(schedule, SLOT)
    schedule.enabled = False
    schedule.save()

    assert asvc.needs_you(agent)["waiting_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_nag.py -v`
Expected: FAIL — `test_unfinished_occurrence_nags` finds 0 schedule items.

- [ ] **Step 3: Write the notify registry**

Create `apps/harness/notify.py`:

```python
"""Notification channels for schedule nags — a string registry, so new channels
are an entry plus a function, never a model change.

Copies the indirection apps/timeline/sources.py uses. Exactly ONE channel ships
today: "inbox" (the needs_you projection). Email / macOS / Slack land here later
without touching AgentSchedule.notify's shape.
"""
from __future__ import annotations

from typing import Callable


def _inbox(agent, schedule, turn) -> dict:
    """The default channel: a typed needs_you item. Passive but omnipresent —
    it rides the 'N waiting on you' badge the supervisor surfaces already show."""
    subtitle = "Scheduled — not finished" if turn else "Scheduled"
    return {
        "type": "review",
        "ref_kind": "schedule",
        "ref_id": schedule.id,
        "title": schedule.name,
        "subtitle": subtitle,
        "url": "",
        "created_at": turn.created_at if turn else schedule.updated_at,
    }


# channel id -> builder. Unknown ids in AgentSchedule.notify are ignored, so a
# half-rolled-out channel can never 500 the supervisor's inbox.
CHANNELS: dict[str, Callable] = {"inbox": _inbox}


def schedule_nag_items(agent) -> list[dict]:
    """Every enabled schedule of `agent` whose latest occurrence isn't done."""
    from . import services
    from .models import AgentSchedule, Turn

    items: list[dict] = []
    for schedule in AgentSchedule.objects.filter(agent=agent, enabled=True):
        turn = services.latest_occurrence_turn(schedule)
        if turn is None or turn.status == Turn.DONE:
            continue  # never fired, or you finished it — nothing owed
        for channel_id in schedule.notify:
            builder = CHANNELS.get(channel_id)
            if builder is not None:
                items.append(builder(agent, schedule, turn))
    return items
```

- [ ] **Step 4: Hook it into `needs_you`**

In `apps/agents/services.py`, inside `needs_you`, immediately after the run-state projection block (the three `run_review`/`run_question`/`run_notify` extends) and **before** `items: list[dict] = review + question`, insert:

```python
    # Scheduled occurrences you haven't finished. Imported inside the function:
    # apps.harness.models imports apps.agents.models, so a module-level import
    # would cycle (same reason AgentTask.run uses a string FK ref). Both apps are
    # framework tier, so the boundary test is satisfied.
    from apps.harness.notify import schedule_nag_items

    review.extend(schedule_nag_items(agent))
```

- [ ] **Step 5: Widen the `ref_kind` type**

In `apps/agents/schemas.py`, in `class NeedsYouItem`, change the `ref_kind` line to:

```python
    ref_kind: str  # 'task' | 'sync' | 'work_product' | 'run' | 'schedule'
```

> **After the `emdash/mobile-l2vmm` merge:** that branch retypes this as
> `Literal["task", "sync", "work_product", "run"]` (commit 2ce30c1). It must
> become `Literal["task", "sync", "work_product", "run", "schedule"]` or the nag
> 500s at serialization.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_nag.py -v`
Expected: PASS (5 passed)

- [ ] **Step 7: Verify no regression in the boundary + agents suites**

Run: `uv run pytest tests/test_architecture_boundary.py tests/test_agents.py tests/test_schedule_nag.py -v`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add apps/harness/notify.py apps/agents/services.py apps/agents/schemas.py tests/test_schedule_nag.py
git commit -m "feat(agents): nag unfinished scheduled turns via needs_you (one pluggable channel)"
```

---

### Task 9: Regenerate the OpenAPI types

**Files:**
- Modify: `frontend/src/api/generated.ts` (generated — never hand-edit)

**Interfaces:**
- Consumes: every route from Tasks 6 and 7.
- Produces: `paths["/api/agents/{slug}/schedules/"]` and the `ScheduleOut`/`ScheduleIn` component types, consumed by Task 10.

- [ ] **Step 1: Regenerate**

Run: `cd frontend && npm run gen:api`
Expected: `frontend/src/api/generated.ts` updates with the schedules paths.

- [ ] **Step 2: Verify the new paths landed**

Run: `grep -c "schedules" frontend/src/api/generated.ts`
Expected: a non-zero count.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/generated.ts
git commit -m "chore(api): regenerate types for the schedules surface"
```

---

### Task 10: The API client + `useSchedules` hook

**Files:**
- Create: `frontend/src/api/schedules.ts`
- Test: typecheck-only (`npm run build`).

**Why no vitest here:** the repo runs vitest (`npm run test`, 5 suites) but every
one tests a **pure function** — `src/api/base.test.ts` covers `normalizeBase`/
`joinBase`, not fetch wrappers. There is no `@testing-library` dependency and no
component/render test in the codebase. These client functions are thin I/O over
`openapi-fetch` with no branching logic, so testing them would mean introducing a
mocking paradigm the repo has never used. Task 11's `describeCron` **is** pure
logic and **does** get a vitest test, per that convention.

**Interfaces:**
- Consumes: `generated.ts` types (Task 9); the existing `client.v2.ts` `openapi-fetch` client.
- Produces:
  - `type Schedule = components["schemas"]["ScheduleOut"]`
  - `listSchedules(slug: string): Promise<Schedule[]>`
  - `createSchedule(slug: string, body: ScheduleIn): Promise<Schedule>`
  - `updateSchedule(slug: string, id: number, body: SchedulePatch): Promise<Schedule>`
  - `deleteSchedule(slug: string, id: number): Promise<void>`
  - `runScheduleNow(slug: string, id: number): Promise<Schedule>`

- [ ] **Step 1: Read the existing client convention**

Run: `sed -n '1,40p' frontend/src/api/harness.ts`
Expected: shows how the module imports the shared client and unwraps `{ data, error }`. **Match it exactly** — do not invent a second fetch style.

- [ ] **Step 2: Write the client**

Create `frontend/src/api/schedules.ts`, following the import + error-unwrapping style you just read:

```ts
import { client } from "./client.v2";
import type { components } from "./generated";

export type Schedule = components["schemas"]["ScheduleOut"];
export type ScheduleIn = components["schemas"]["ScheduleIn"];
export type SchedulePatch = components["schemas"]["SchedulePatch"];

export async function listSchedules(slug: string): Promise<Schedule[]> {
  const { data, error } = await client.GET("/api/agents/{slug}/schedules/", {
    params: { path: { slug } },
  });
  if (error) throw error;
  return data?.items ?? [];
}

export async function createSchedule(slug: string, body: ScheduleIn): Promise<Schedule> {
  const { data, error } = await client.POST("/api/agents/{slug}/schedules/", {
    params: { path: { slug } },
    body,
  });
  if (error) throw error;
  return data!;
}

export async function updateSchedule(
  slug: string,
  id: number,
  body: SchedulePatch,
): Promise<Schedule> {
  const { data, error } = await client.PATCH("/api/agents/{slug}/schedules/{schedule_id}", {
    params: { path: { slug, schedule_id: id } },
    body,
  });
  if (error) throw error;
  return data!;
}

export async function deleteSchedule(slug: string, id: number): Promise<void> {
  const { error } = await client.DELETE("/api/agents/{slug}/schedules/{schedule_id}", {
    params: { path: { slug, schedule_id: id } },
  });
  if (error) throw error;
}

export async function runScheduleNow(slug: string, id: number): Promise<Schedule> {
  const { data, error } = await client.POST(
    "/api/agents/{slug}/schedules/{schedule_id}/run-now",
    { params: { path: { slug, schedule_id: id } } },
  );
  if (error) throw error;
  return data!;
}

export async function previewCron(
  slug: string,
  cron: string,
  timezone: string,
): Promise<string[]> {
  const { data, error } = await client.POST("/api/agents/{slug}/schedules/preview", {
    params: { path: { slug } },
    body: { cron, timezone },
  });
  if (error) throw error;
  return data?.next_runs ?? [];
}
```

If the exact client import name differs from what Step 1 showed, use the one from the file — the names above assume `client.v2.ts` exports `client`.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/schedules.ts
git commit -m "feat(frontend): typed schedules client on the generated types"
```

---

### Task 11: The Schedules rail section

**Files:**
- Create: `frontend/src/components/agents/cronDescribe.ts`
- Create: `frontend/src/components/agents/cronDescribe.test.ts`
- Create: `frontend/src/components/agents/SchedulesSection.tsx`
- Create: `frontend/src/components/agents/ScheduleEditor.tsx`
- Modify: the agent workspace rail (find it — see Step 1)

**Interfaces:**
- Consumes: everything from Task 10.
- Produces: `<SchedulesSection agentSlug={string} />`; `describeCron(cron, tz)` and `relative(iso)` from `cronDescribe.ts`.

`describeCron`/`relative` live in their own module, not inline in the component:
they are pure functions with real branching, and the repo's vitest convention
tests pure functions only (`src/api/base.test.ts`). A function inside a `.tsx`
component file could not be tested without adding `@testing-library`, which this
repo has deliberately never adopted.

Dense table, not cards. **Semantic tokens only** — a raw `stone-*`/`orange-*` literal is a review rejection.

- [ ] **Step 1: Find the rail and copy its section convention**

Run: `ls frontend/src/pages/agents/ && grep -rn "NeedsYouSection" frontend/src/pages/agents/ | head`
Expected: shows the rail page and how an existing section (`NeedsYouSection`) is registered. **Read that file** and mirror its data-loading + empty-state + heading structure rather than inventing one.

- [ ] **Step 2: Write the failing test for the pure helpers**

Create `frontend/src/components/agents/cronDescribe.test.ts`:

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { describeCron, relative } from "./cronDescribe";

describe("describeCron", () => {
  it("renders a weekly schedule", () => {
    expect(describeCron("0 9 * * 5", "America/New_York")).toBe("Fridays at 09:00 · New York");
  });

  it("renders a monthly schedule", () => {
    expect(describeCron("0 9 1 * *", "America/New_York")).toBe(
      "Day 1 monthly at 09:00 · New York",
    );
  });

  it("renders a daily schedule", () => {
    expect(describeCron("30 6 * * *", "UTC")).toBe("Daily at 06:30 · UTC");
  });

  it("falls back to the raw expression for shapes it cannot name", () => {
    // Always correct, if not always pretty — never claim a wrong cadence.
    expect(describeCron("0 9 * * 1-5", "UTC")).toBe("0 9 * * 1-5 · UTC");
  });
});

describe("relative", () => {
  afterEach(() => vi.useRealTimers());

  it("renders an em dash for no value", () => {
    expect(relative(null)).toBe("—");
  });

  it("renders future days", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-07-15T12:00:00Z"));
    expect(relative("2026-07-17T13:00:00Z")).toBe("in 2d");
  });

  it("renders past hours", () => {
    vi.useFakeTimers().setSystemTime(new Date("2026-07-15T12:00:00Z"));
    expect(relative("2026-07-15T09:00:00Z")).toBe("3h ago");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/agents/cronDescribe.test.ts`
Expected: FAIL — cannot resolve `./cronDescribe`.

- [ ] **Step 4: Write the pure helpers**

Create `frontend/src/components/agents/cronDescribe.ts`:

```ts
const DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

/** Friendly rendering for the common cron shapes; falls back to the raw
 * expression, which is always correct if not always pretty. Naming a cadence
 * wrongly would be worse than not naming it. */
export function describeCron(cron: string, tz: string): string {
  const [min, hour, dom, mon, dow] = cron.trim().split(/\s+/);
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  const zone = tz.split("/").pop()?.replace(/_/g, " ") ?? tz;
  if (dom === "*" && mon === "*" && /^[0-6]$/.test(dow)) {
    return `${DAYS[Number(dow)]} at ${time} · ${zone}`;
  }
  if (dow === "*" && mon === "*" && /^\d+$/.test(dom)) {
    return `Day ${dom} monthly at ${time} · ${zone}`;
  }
  if (dom === "*" && mon === "*" && dow === "*") return `Daily at ${time} · ${zone}`;
  return `${cron} · ${zone}`;
}

/** Coarse relative time — "in 2d", "3h ago". Deliberately not a full i18n
 * formatter: the table only needs to answer "soon or not?". */
export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  const days = Math.round(ms / 86_400_000);
  if (Math.abs(days) >= 1) return days > 0 ? `in ${days}d` : `${-days}d ago`;
  const hours = Math.round(ms / 3_600_000);
  if (Math.abs(hours) >= 1) return hours > 0 ? `in ${hours}h` : `${-hours}h ago`;
  return "now";
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/agents/cronDescribe.test.ts`
Expected: PASS (7 passed)

- [ ] **Step 6: Write the section**

Create `frontend/src/components/agents/SchedulesSection.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";
import type { Schedule } from "@/api/schedules";
import { listSchedules, runScheduleNow, updateSchedule } from "@/api/schedules";
import { describeCron, relative } from "./cronDescribe";
import { ScheduleEditor } from "./ScheduleEditor";

function StatusChip({ status }: { status: string }) {
  if (!status) return <span className="text-foreground-subtle">—</span>;
  const tone =
    status === "done"
      ? "bg-success/10 text-success border-success/30"
      : status === "missed" || status === "failed" || status === "lost"
        ? "bg-warning/10 text-warning border-warning/30"
        : "bg-info/10 text-info border-info/30";
  return (
    <span className={`inline-flex rounded border px-1.5 py-0.5 text-xs ${tone}`}>{status}</span>
  );
}

export function SchedulesSection({ agentSlug }: { agentSlug: string }) {
  const [rows, setRows] = useState<Schedule[]>([]);
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    setRows(await listSchedules(agentSlug));
  }, [agentSlug]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onRunNow(id: number) {
    setBusy(id);
    try {
      await runScheduleNow(agentSlug, id);
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function onToggle(row: Schedule) {
    await updateSchedule(agentSlug, row.id, { enabled: !row.enabled });
    await load();
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-medium text-foreground">Schedules</h2>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90"
        >
          New schedule
        </button>
      </header>

      {rows.length === 0 ? (
        <p className="px-4 py-6 text-sm text-muted-foreground">
          No recurring activities yet. Add one to have {agentSlug} start it on a cadence.
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2 font-normal">Schedule</th>
              <th className="px-4 py-2 font-normal">When</th>
              <th className="px-4 py-2 font-normal">Next</th>
              <th className="px-4 py-2 font-normal">Last</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-border/50 last:border-0">
                <td className="px-4 py-2">
                  <span className={row.enabled ? "text-foreground" : "text-foreground-subtle"}>
                    {row.name}
                  </span>
                </td>
                <td className="px-4 py-2 text-foreground-secondary">
                  {describeCron(row.cron, row.timezone)}
                </td>
                <td className="px-4 py-2 text-muted-foreground">
                  {row.enabled ? relative(row.next_runs?.[0]) : "paused"}
                </td>
                <td className="px-4 py-2">
                  <StatusChip status={row.last_status} />
                </td>
                <td className="px-4 py-2 text-right whitespace-nowrap">
                  <button
                    type="button"
                    disabled={busy === row.id}
                    onClick={() => void onRunNow(row.id)}
                    className="rounded border border-input px-2 py-0.5 text-xs text-foreground-secondary hover:bg-muted disabled:opacity-50"
                  >
                    {busy === row.id ? "Starting…" : "Run now"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void onToggle(row)}
                    className="ml-2 rounded border border-input px-2 py-0.5 text-xs text-foreground-secondary hover:bg-muted"
                  >
                    {row.enabled ? "Pause" : "Resume"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditing(row)}
                    className="ml-2 rounded border border-input px-2 py-0.5 text-xs text-foreground-secondary hover:bg-muted"
                  >
                    Edit
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {editing && (
        <ScheduleEditor
          agentSlug={agentSlug}
          schedule={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      )}
    </section>
  );
}
```

- [ ] **Step 7: Write the editor**

Create `frontend/src/components/agents/ScheduleEditor.tsx`:

```tsx
import { useEffect, useState } from "react";
import type { Schedule } from "@/api/schedules";
import { createSchedule, deleteSchedule, previewCron, updateSchedule } from "@/api/schedules";

const PRESETS: { label: string; cron: string }[] = [
  { label: "Weekly — Friday 9am", cron: "0 9 * * 5" },
  { label: "Weekly — Monday 9am", cron: "0 9 * * 1" },
  { label: "Monthly — 1st, 9am", cron: "0 9 1 * *" },
  { label: "Daily — 9am", cron: "0 9 * * *" },
];

export function ScheduleEditor({
  agentSlug,
  schedule,
  onClose,
  onSaved,
}: {
  agentSlug: string;
  schedule: Schedule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(schedule?.name ?? "");
  const [prompt, setPrompt] = useState(schedule?.prompt ?? "");
  const [cron, setCron] = useState(schedule?.cron ?? "0 9 * * 5");
  const [tz, setTz] = useState(
    schedule?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState("");

  // Ask the SERVER when this cron would actually run — debounced, because the
  // user is mid-type. Never re-implement cron here: a client parser that says
  // "Fridays" while the server fires Thursdays is the exact failure the preview
  // exists to catch.
  useEffect(() => {
    const timer = setTimeout(() => {
      previewCron(agentSlug, cron, tz)
        .then((runs) => {
          setPreview(runs);
          setPreviewError("");
        })
        .catch(() => {
          setPreview([]);
          setPreviewError("Not a valid schedule.");
        });
    }, 300);
    return () => clearTimeout(timer);
  }, [agentSlug, cron, tz]);

  async function onSave() {
    setSaving(true);
    setError("");
    try {
      if (schedule) {
        await updateSchedule(agentSlug, schedule.id, { name, prompt, cron, timezone: tz });
      } else {
        await createSchedule(agentSlug, { name, prompt, cron, timezone: tz });
      }
      onSaved();
    } catch (err) {
      // The server validates the cron and returns RFC 7807 problem+json; show its
      // detail rather than a generic failure, since a bad expression is the
      // likeliest reason to land here.
      const problem = err as { detail?: string; title?: string };
      setError(problem.detail ?? problem.title ?? "Could not save the schedule.");
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!schedule) return;
    setSaving(true);
    try {
      await deleteSchedule(agentSlug, schedule.id);
      onSaved();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-lg rounded-lg border border-border bg-card p-4">
        <h3 className="mb-3 text-sm font-medium text-foreground">
          {schedule ? "Edit schedule" : "New schedule"}
        </h3>

        <label className="mb-1 block text-xs text-muted-foreground">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Weekly manager report"
          className="mb-3 w-full rounded border border-input bg-input px-2 py-1 text-sm text-foreground"
        />

        <label className="mb-1 block text-xs text-muted-foreground">Prompt</label>
        <input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="/echo:manager-report"
          className="mb-3 w-full rounded border border-input bg-input px-2 py-1 font-mono text-sm text-foreground"
        />

        <label className="mb-1 block text-xs text-muted-foreground">When</label>
        <div className="mb-2 flex flex-wrap gap-1">
          {PRESETS.map((p) => (
            <button
              key={p.cron}
              type="button"
              onClick={() => setCron(p.cron)}
              className={`rounded border px-2 py-0.5 text-xs ${
                cron === p.cron
                  ? "border-primary text-primary"
                  : "border-input text-foreground-secondary hover:bg-muted"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="mb-3 flex gap-2">
          <input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            className="w-40 rounded border border-input bg-input px-2 py-1 font-mono text-sm text-foreground"
          />
          <input
            value={tz}
            onChange={(e) => setTz(e.target.value)}
            className="flex-1 rounded border border-input bg-input px-2 py-1 text-sm text-foreground"
          />
        </div>

        <div className="mb-3 rounded border border-border bg-muted/40 px-2 py-1.5">
          <p className="mb-0.5 text-xs text-muted-foreground">Next runs</p>
          {previewError ? (
            <p className="text-xs text-destructive">{previewError}</p>
          ) : preview.length === 0 ? (
            <p className="text-xs text-foreground-subtle">—</p>
          ) : (
            <ul className="text-xs text-foreground-secondary">
              {preview.map((iso) => (
                <li key={iso}>
                  {new Date(iso).toLocaleString(undefined, {
                    weekday: "short",
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <p className="mb-3 text-xs text-destructive">{error}</p>}

        <div className="flex items-center justify-between">
          <div>
            {schedule && (
              <button
                type="button"
                onClick={() => void onDelete()}
                className="text-xs text-destructive hover:underline"
              >
                Delete
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-input px-3 py-1 text-xs text-foreground-secondary hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || !name.trim() || !prompt.trim()}
              onClick={() => void onSave()}
              className="rounded bg-primary px-3 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
```

**The preview is what makes a hand-typed cron trustworthy** (spec, § UI): it
verifies the expression at edit time instead of next Friday. It calls the
`POST /api/agents/{slug}/schedules/preview` route from Task 6a, debounced, and
renders the server's answer — never a second cron implementation in TypeScript.
A divergent client-side parser saying "Fridays" while the server fires Thursdays
is precisely the failure the preview exists to prevent.

- [ ] **Step 8: Mount the section in the rail**

Add `<SchedulesSection agentSlug={slug} />` to the agent workspace rail beside the existing sections, following exactly the registration pattern you read in Step 1.

- [ ] **Step 9: Typecheck**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 10: Verify no raw palette literals**

Run: `grep -nE "stone-|orange-|zinc-|slate-|amber-|emerald-|sky-|violet-" frontend/src/components/agents/SchedulesSection.tsx frontend/src/components/agents/ScheduleEditor.tsx`
Expected: no output.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/components/agents/ frontend/src/pages/agents/
git commit -m "feat(frontend): Schedules rail section — dense table, run-now, cron editor"
```

---

### Task 12: Documentation

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: the shipped surface.
- Produces: nothing code-facing.

- [ ] **Step 1: Document the endpoints**

In `CLAUDE.md`, under the **Agents** section's endpoint list, add after the tasks/commands lines:

```markdown
- `GET|POST /api/agents/{slug}/schedules/` — list / create a **recurring turn** (cron + IANA tz). Fires onto the normal harness turn path; the `Turn` *is* the occurrence (`origin=cron`, `idempotency_key="sched:<id>:<slot>"`). Firing slot N+1 supersedes slot N's unfinished turn as `MISSED` — you only ever owe the newest.
- `PATCH|DELETE /api/agents/{slug}/schedules/{id}` — edit / remove
- `POST /api/agents/{slug}/schedules/{id}/run-now` — trigger off-cycle (`origin=manual`; never satisfies a real slot)
```

And under the **Agent runs** / harness area:

```markdown
- `GET /api/harness/schedules/?runner_id=…` — runner syncs the schedules it may fire. **Tenant-scoped, never scoped by `capabilities`** (a caller-supplied hint, not a boundary — see b4f5ead).
- `POST /api/harness/schedules/{id}/fire?runner_id=…` — the runner reports a due slot; the server materializes the turn.
```

- [ ] **Step 2: Add the design decision**

In `CLAUDE.md` under **Design Decisions**, add:

```markdown
- **Scheduled turns are runner-fired, server-configured, and self-superseding:** `AgentSchedule` (`apps/harness`) holds cron config server-side so it is visible/editable in the Agent UI, but the runner evaluates the cron and POSTs a due slot — the scheduler is a *producer of turns*, not a second execution engine (no celery, no beat, no new deploy surface). Both macOS-account runners may fire the same slot safely: the slot-derived `idempotency_key` collapses the race inside `enqueue_turn`. There is deliberately **no occurrence table** — the `Turn` is the occurrence. Unattended occurrences are released as `MISSED` after `grace_minutes`, because an abandoned session otherwise wedges the agent forever via `one_executing_turn_per_agent` (the runner's heartbeat keeps renewing its lease, so the lease sweep never rescues it). The nag is a `needs_you()` projection, so it reaches `/supervisor` and the mobile PWA for free. See `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md`.
```

- [ ] **Step 3: Link the spec**

Under **Reference Docs**, add:

```markdown
- `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md` — Agent scheduled turns (recurring turns, supersede-as-give-up, the nag projection)
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: scheduled turns — endpoints, the design decision, spec link"
```

---

## Final Verification

- [ ] **Full backend suite**

Run: `uv run pytest`
Expected: PASS (all), including `tests/test_architecture_boundary.py`.

- [ ] **Frontend build + unit tests**

Run: `cd frontend && npm run build && npm run test`
Expected: build succeeds; vitest passes (existing 5 suites + cronDescribe).

- [ ] **Migrations are complete**

Run: `uv run python manage.py makemigrations --check --dry-run`
Expected: `No changes detected`.

- [ ] **Drive it end to end** — per `superpowers:verification-before-completion`, tests alone don't close this out:

```bash
uv run python manage.py migrate
uv run honcho start -f Procfile.dev
```

Then: create a schedule in the agent's rail → click **Run now** → confirm a `manual` turn is queued and the nag appears in Needs You → mark the turn done → confirm the nag clears.

## Deferred (spec's "Open for iteration")

Not in this plan, by design: per-schedule escalation policy, catch-up/backfill, and notify channels beyond `inbox`.

## Runner-side work — a separate plan

This plan ships the **server + UI**, which is independently useful: schedules are creatable, editable, and **Run now** works end to end. The `canopy-runner` sync/evaluate/fire loop is **not** in this plan and gets its own, against the API frozen here.

`packages/canopy_runner` lives **in this repo**, but it declares `dependencies = []` and never imports `apps.*` — deliberately, so the runner stays an installable, Django-free client of the HTTP API. So it cannot import `due_slot` (which lives in `apps/harness/cron.py`), and **nothing on this branch fires a schedule automatically yet**: `Run now` is the only trigger. That deferral is intentional — the slot math, the fire route, and their tests are frozen server-side first, and the runner loop below consumes them over HTTP:

1. On each poll, `GET /api/harness/schedules/?runner_id=…`, cache locally.
2. For each schedule, `due_slot(cron, tz, after=fire_after, now=now())` — pass
   the server's **`fire_after`**, never `last_slot`. `last_slot` is NULL until
   the first fire, and `due_slot(after=None)` looks backward with no lower
   bound, so a schedule created Wednesday would immediately fire for the
   previous Friday. The server computes `fire_after = last_slot or created_at`
   precisely so the runner cannot get this wrong.
3. If a slot is due, `POST /api/harness/schedules/{id}/fire?runner_id=…`.

Until that lands, schedules fire only via **Run now** — which is exactly the "try it then iterate" starting point, and it exercises every server path except the cron evaluation (which Task 3 covers directly).
