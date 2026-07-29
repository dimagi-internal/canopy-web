# Directed Runner Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-agent ranked runner assignments as the single routing authority, chat turns sticky to the runner holding the live session, hard-pinned turns, and per-runner readiness drills.

**Architecture:** New `RunnerAssignment` (agent↔runner, ranked) and `RunnerDrill` (per-pair outcome) tables plus `Turn.pinned_runner` in `apps/harness`. `claim_next_turn` gains three ordered filters: pin, assignment cascade (availability + 60s grace), session-binding stickiness. New Ninja endpoints for assignments, drills, session placement; React routing-matrix + drill grid + chat placement UI.

**Tech Stack:** Django 5 + Django Ninja 1.x + Pydantic v2, PostgreSQL, pytest(-django), React 19 + openapi-fetch.

**Spec:** `docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md` (read it first).

## Global Constraints

- Framework/product boundary: everything here is framework (`apps/harness`, `apps/agents`, `apps/canopy_sessions`, frontend) — no product imports.
- Tenancy ALWAYS derives from `runner.paired_by` (see #227 comment in `claim_next_turn`); pins and drills never bypass `tenant_q`.
- Never weaken `one_executing_turn_per_agent` / `one_executing_turn_per_session` or the `chat_session__isnull=False` NULL-injection guard.
- Any change to `apps/**/schemas.py` or `api.py` ⇒ regenerate `frontend/src/api/generated.ts` (`cd frontend && npm run gen:api`, backend on :8000) and commit it.
- Design tokens only in UI (no raw palette literals); dense tables-not-cards.
- All backend tests: `uv run pytest`; frontend gate: `cd frontend && npm run build`.
- `CASCADE_GRACE_SECONDS = 60`; drill idempotency key `drill:{runner.id}:{agent.slug}:{uuid4().hex[:8]}`; drill origin string `"drill"`.

---

### Task 1: Models — RunnerAssignment, RunnerDrill, Turn.pinned_runner, drill origin

**Files:**
- Modify: `apps/harness/models.py` (Runner ~line 20, Turn ORIGIN_CHOICES ~line 160, end of file)
- Create: migration via `makemigrations harness`
- Test: `tests/test_runner_assignments.py` (new)

**Interfaces:**
- Produces: `RunnerAssignment(agent, runner, rank)` with `related_name="runner_assignments"` on Agent and `"agent_assignments"` on Runner; `RunnerDrill(runner, agent, turn, outcome, summary, started_at, finished_at)` with `OUTCOME_PENDING/PASS/FAIL = "pending"/"pass"/"fail"`, related_names `"drills"` (runner) / `"runner_drills"` (agent); `Turn.pinned_runner` FK; `Turn.ORIGIN_DRILL = "drill"`; `Runner.is_available` property.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_runner_assignments.py
import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, RunnerDrill, Turn

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    return Agent.objects.create(slug=slug, name=slug.title())


def _runner(name="r1", **kw):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, capabilities={}, **kw)


def test_assignment_orders_by_rank():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    assert [x.runner for x in a.runner_assignments.all()] == [r1, r2]


def test_assignment_unique_per_agent_runner():
    a, r1 = _agent(), _runner()
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    with pytest.raises(IntegrityError):
        RunnerAssignment.objects.create(agent=a, runner=r1, rank=1)


def test_drill_unique_per_pair_and_defaults_pending():
    a, r1 = _agent(), _runner()
    d = RunnerDrill.objects.create(runner=r1, agent=a)
    assert d.outcome == RunnerDrill.OUTCOME_PENDING
    with pytest.raises(IntegrityError):
        RunnerDrill.objects.create(runner=r1, agent=a)


def test_turn_pinned_runner_nullable_and_origin_drill():
    a, r1 = _agent(), _runner()
    t = Turn.objects.create(
        agent=a, origin=Turn.ORIGIN_DRILL, idempotency_key="k1", pinned_runner=r1
    )
    r1.delete()
    t.refresh_from_db()
    assert t.pinned_runner is None  # SET_NULL degrades pin to normal routing
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runner_assignments.py -v`
Expected: FAIL / ERROR with `ImportError: cannot import name 'RunnerAssignment'`

- [ ] **Step 3: Implement models**

In `apps/harness/models.py`, inside `class Runner`, directly under the `ready_note` field, add:

```python
    @property
    def is_available(self) -> bool:
        """Can this runner take work RIGHT NOW — online (fresh heartbeat) AND
        self-reported ready. The cascade's availability probe (spec 2026-07-24)."""
        return self.live_status == Runner.ONLINE and self.ready
```

In `class Turn`, extend the origin constants (keep `max_length=10`, `"drill"` fits):

```python
    ORIGIN_BOARD, ORIGIN_API, ORIGIN_SLACK, ORIGIN_CRON, ORIGIN_MANUAL, ORIGIN_EMAIL, ORIGIN_DRILL = (
        "board", "api", "slack", "cron", "manual", "email", "drill",
    )
    ORIGIN_CHOICES = [
        (ORIGIN_BOARD, "Board"), (ORIGIN_API, "API"), (ORIGIN_SLACK, "Slack"),
        (ORIGIN_CRON, "Cron"), (ORIGIN_MANUAL, "Manual"), (ORIGIN_EMAIL, "Email"),
        (ORIGIN_DRILL, "Drill"),
    ]
```

In `class Turn`, after the `claimed_by` field, add:

```python
    # A HARD pin: only this runner may claim (drills, chat "wait for X", directed
    # placement). Bypasses assignments/capabilities, never the tenant gate or the
    # one-executing-turn constraints. SET_NULL: a deleted runner degrades the pin
    # to normal routing instead of stranding the turn.
    pinned_runner = models.ForeignKey(
        Runner, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pinned_turns",
    )
```

At the end of `apps/harness/models.py`, add:

```python
class RunnerAssignment(models.Model):
    """One row of an agent's ordered runner list — THE routing authority for agent
    turns (spec 2026-07-24-directed-runner-routing). An agent with no rows is
    explicitly unroutable. Replaced routing-by-capabilities + kind preference."""

    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="runner_assignments"
    )
    runner = models.ForeignKey(
        Runner, on_delete=models.CASCADE, related_name="agent_assignments"
    )
    rank = models.PositiveSmallIntegerField()  # 0 = first choice

    class Meta:
        ordering = ["agent_id", "rank"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "runner"], name="one_assignment_per_agent_runner"),
        ]


class RunnerDrill(models.Model):
    """Latest readiness-drill outcome for one (runner, agent) pair. Reset to
    pending on each fan-out; resolved by the agent's report callback or by the
    drill turn failing. Freshness = finished_at age (no server TTL)."""

    OUTCOME_PENDING, OUTCOME_PASS, OUTCOME_FAIL = "pending", "pass", "fail"
    OUTCOME_CHOICES = [(OUTCOME_PENDING, "Pending"), (OUTCOME_PASS, "Pass"), (OUTCOME_FAIL, "Fail")]

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="drills")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="runner_drills")
    turn = models.ForeignKey(Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    outcome = models.CharField(max_length=8, choices=OUTCOME_CHOICES, default=OUTCOME_PENDING)
    summary = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["agent__slug"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "agent"], name="one_drill_row_per_runner_agent"),
        ]
```

- [ ] **Step 4: Make migration + run tests**

Run: `uv run python manage.py makemigrations harness && uv run pytest tests/test_runner_assignments.py -v`
Expected: 1 new migration; 4 PASS

- [ ] **Step 5: Full suite guard + commit**

Run: `uv run pytest -q` — expected: no new failures.

```bash
git add apps/harness/models.py apps/harness/migrations/ tests/test_runner_assignments.py
git commit -m "feat(harness): RunnerAssignment + RunnerDrill models, Turn.pinned_runner, drill origin"
```

---

### Task 2: Seed migration (assignments from today's live state)

**Files:**
- Create: `apps/harness/migrations/00XX_seed_runner_assignments.py` (XX = next number)
- Test: `tests/test_runner_assignments.py` (append)

**Interfaces:**
- Consumes: `RunnerAssignment` from Task 1.
- Produces: data migration seeding one assignment per (agent, runner) where `agent.slug ∈ runner.capabilities["agents"]`, rank-ordered by the agent's `runner_preference` kind order (listed kinds first in listed order, unlisted kinds after, ties by runner name).

- [ ] **Step 1: Write the seeding function as a tested helper**

The migration body must be importable logic so it's testable. Add to `apps/harness/services.py` (near the bottom, above any `__all__`):

```python
def seed_assignments_from_capabilities() -> int:
    """One-time bridge from the old two-sided routing config (runner
    capabilities.agents ∩ agent.runner_preference kind order) into explicit
    RunnerAssignment rows. Idempotent: skips (agent, runner) pairs that already
    have a row. Returns rows created. Used by the seed data migration."""
    from apps.agents.models import Agent
    from apps.harness.models import Runner, RunnerAssignment

    created = 0
    runners = list(Runner.objects.exclude(status=Runner.RETIRED))
    for agent in Agent.objects.all():
        matched = [r for r in runners if agent.slug in (r.capabilities.get("agents") or [])]
        pref = agent.runner_preference or []

        def sort_key(r):
            kind_rank = pref.index(r.kind) if r.kind in pref else len(pref)
            return (kind_rank, r.name)

        existing = set(
            RunnerAssignment.objects.filter(agent=agent).values_list("runner_id", flat=True)
        )
        next_rank = RunnerAssignment.objects.filter(agent=agent).count()
        for r in sorted(matched, key=sort_key):
            if r.id in existing:
                continue
            RunnerAssignment.objects.create(agent=agent, runner=r, rank=next_rank)
            next_rank += 1
            created += 1
    return created
```

Append test:

```python
def test_seed_assignments_from_capabilities():
    from apps.harness.services import seed_assignments_from_capabilities

    echo = _agent("echo")
    echo.runner_preference = ["cloud", "emdash"]
    echo.save()
    ada = _agent("ada")
    laptop = _runner("laptop", capabilities={"agents": ["echo", "ada"]})
    cloud = Runner.objects.create(name="cloudy", kind=Runner.CLOUD, capabilities={"agents": ["echo"]})
    retired = Runner.objects.create(
        name="old", kind=Runner.EMDASH, status=Runner.RETIRED, capabilities={"agents": ["echo"]}
    )
    assert seed_assignments_from_capabilities() == 3
    assert [x.runner for x in echo.runner_assignments.all()] == [cloud, laptop]  # cloud kind preferred
    assert [x.runner for x in ada.runner_assignments.all()] == [laptop]
    assert seed_assignments_from_capabilities() == 0  # idempotent
```

Note: `_runner` from Task 1 passes `capabilities={}` by default — update its signature to `def _runner(name="r1", capabilities=None, **kw): return Runner.objects.create(name=name, kind=Runner.EMDASH, capabilities=capabilities or {}, **kw)`.

- [ ] **Step 2: Run to verify fail, implement, pass**

Run: `uv run pytest tests/test_runner_assignments.py -v` → FAIL (helper missing) → add helper → PASS.

- [ ] **Step 3: Create the data migration**

```python
# apps/harness/migrations/00XX_seed_runner_assignments.py
from django.db import migrations


def seed(apps, schema_editor):
    # Calls the live service helper deliberately: the logic reads JSON fields and
    # sorts — historical models add nothing here, and the helper is idempotent.
    from apps.harness.services import seed_assignments_from_capabilities
    seed_assignments_from_capabilities()


class Migration(migrations.Migration):
    dependencies = [("harness", "00XX-1_previous_migration")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
```

(Replace `00XX-1_previous_migration` with the real predecessor name from `ls apps/harness/migrations/`.)

- [ ] **Step 4: Verify migrations apply cleanly**

Run: `uv run python manage.py migrate harness && uv run pytest -q`
Expected: applies; suite green.

- [ ] **Step 5: Commit**

```bash
git add apps/harness/services.py apps/harness/migrations/ tests/test_runner_assignments.py
git commit -m "feat(harness): seed RunnerAssignment rows from capabilities + kind preference"
```

---

### Task 3: Claim routing — pinned turns

**Files:**
- Modify: `apps/harness/services.py` (`claim_next_turn`, lines ~266-284)
- Test: `tests/test_runner_routing.py` (new)

**Interfaces:**
- Consumes: `Turn.pinned_runner` (Task 1); `enqueue_turn` gains `pinned_runner=None` kwarg.
- Produces: claim semantics — turn pinned elsewhere invisible; pinned-here claims regardless of capabilities/assignments; tenancy still gates.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_runner_routing.py
import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc  # follow existing harness tests' workspace setup
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(name="owner"):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password="x")


def _online_runner(name, user, **kw):
    r = Runner.objects.create(
        name=name, kind=kw.pop("kind", Runner.EMDASH), capabilities=kw.pop("capabilities", {}),
        paired_by=user, last_heartbeat_at=timezone.now(), status=Runner.ONLINE, **kw,
    )
    return r


def _agent(slug, workspace=None):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=workspace)


def test_pinned_turn_invisible_to_other_runners():
    u = _user()
    r1, r2 = _online_runner("r1", u), _online_runner("r2", u)
    a = _agent("echo")
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=0)
    turn, _ = services.enqueue_turn(agent=a, origin=Turn.ORIGIN_DRILL,
                                    idempotency_key="p1", pinned_runner=r1)
    assert services.claim_next_turn(r2) is None
    claimed = services.claim_next_turn(r1)
    assert claimed is not None and claimed.pk == turn.pk


def test_pin_bypasses_assignments_but_not_tenancy():
    u, stranger = _user("owner"), _user("stranger")
    ws = wsvc.create_workspace(owner=u, name="W1")  # mirror existing test helpers
    a = _agent("echo", workspace=ws)
    r_stranger = _online_runner("evil", stranger)
    turn, _ = services.enqueue_turn(agent=a, origin=Turn.ORIGIN_DRILL,
                                    idempotency_key="p2", pinned_runner=r_stranger)
    assert services.claim_next_turn(r_stranger) is None  # tenant gate holds even when pinned
```

NOTE: mirror the workspace/user fixture idioms already used in `tests/` for harness claim tests (grep `claim_next_turn` in tests/ and copy the setup shape) — the intent of each assertion above is normative, the fixture plumbing should match the house pattern.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runner_routing.py -v`
Expected: FAIL — `enqueue_turn() got an unexpected keyword argument 'pinned_runner'`

- [ ] **Step 3: Implement**

In `enqueue_turn`: add kwarg `pinned_runner=None` to the signature and `pinned_runner=pinned_runner,` to the `Turn.objects.create(...)` call.

In `claim_next_turn`, replace the candidate query block (currently starting `target_q = Q(agent__slug__in=slugs) ...` through `.order_by("created_at")`) with:

```python
    target_q = Q(agent__slug__in=slugs) | Q(project__in=projects)
    if session_capable:
        target_q = target_q | Q(chat_session__isnull=False)
    # A pin trumps target/routing matching (but NOTHING else): a turn pinned to
    # this runner is claimable even with empty capabilities — that is what lets a
    # warm standby be drilled. A turn pinned elsewhere is invisible.
    match_q = Q(pinned_runner=runner) | (target_q & routing_q)
    candidates = (
        Turn.objects.filter(status=Turn.QUEUED)
        .filter(Q(pinned_runner__isnull=True) | Q(pinned_runner=runner))
        .filter(match_q)
        .exclude(agent_id__in=busy_agents)
        .exclude(chat_session_id__in=busy_sessions)
        .filter(tenant_q)
        .select_related("agent")
        .order_by("created_at")
    )
```

and in the per-candidate loop, skip kind/preference checks for pinned turns:

```python
    for turn in candidates:
        pinned_here = turn.pinned_runner_id == runner.id
        if not pinned_here:
            if not _kind_allows(runner, turn.routing):
                continue
            if not _preference_allows(runner, turn, now):
                continue
```

Also delete the early-exit `if not slugs and not projects and not session_capable: return None` — a capability-less runner must still see turns pinned to it. Replace with:

```python
    has_pins = Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner).exists()
    if not slugs and not projects and not session_capable and not has_pins:
        return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_runner_routing.py tests/ -q -k "claim or routing or turn"`
Expected: new tests PASS, existing claim tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/harness/services.py tests/test_runner_routing.py
git commit -m "feat(harness): hard-pinned turns — only the pinned runner claims, tenancy still gates"
```

---

### Task 4: Claim routing — assignment cascade for agent turns

**Files:**
- Modify: `apps/harness/services.py` (`_preference_allows` → `_assignment_allows`; `claim_next_turn` target leg)
- Test: `tests/test_runner_routing.py` (append)

**Interfaces:**
- Consumes: `RunnerAssignment`, `Runner.is_available` (Task 1).
- Produces: `CASCADE_GRACE_SECONDS = 60`; `_assignment_allows(runner, turn, assignment_map, now) -> bool`; agent-turn eligibility = assignment exists AND (no better-ranked available runner OR turn older than grace). `_preference_allows` and `PREFERENCE_TIER_GRACE_SECONDS` deleted.

- [ ] **Step 1: Write failing tests** (append to `tests/test_runner_routing.py`)

```python
def _assign(agent, runner, rank):
    return RunnerAssignment.objects.create(agent=agent, runner=runner, rank=rank)


def test_rank0_available_blocks_rank1():
    u = _user()
    r0, r1 = _online_runner("r0", u), _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c1")
    assert services.claim_next_turn(r1) is None      # r0 is available → r1 waits
    assert services.claim_next_turn(r0) is not None  # r0 claims


def test_rank1_takes_over_when_rank0_offline():
    u = _user()
    r0 = Runner.objects.create(name="r0", kind=Runner.EMDASH, capabilities={}, paired_by=u)  # never heartbeat
    r1 = _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c2")
    assert services.claim_next_turn(r1) is not None


def test_not_ready_counts_as_unavailable():
    u = _user()
    r0 = _online_runner("r0", u); r0.ready = False; r0.save()
    r1 = _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c3")
    assert services.claim_next_turn(r1) is not None


def test_grace_opens_next_rank_even_when_rank0_available(monkeypatch):
    u = _user()
    r0, r1 = _online_runner("r0", u), _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    turn, _ = services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c4")
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - timezone.timedelta(seconds=services.CASCADE_GRACE_SECONDS + 1)
    )
    assert services.claim_next_turn(r1) is not None  # r0 online-but-stuck can't wedge the queue


def test_unassigned_runner_never_claims_even_with_capabilities():
    u = _user()
    r = _online_runner("r", u, capabilities={"agents": ["echo"]})
    a = _agent("echo")  # no assignments at all → unroutable
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c5")
    assert services.claim_next_turn(r) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runner_routing.py -v -k "rank or grace or unassigned"`
Expected: FAIL (capabilities still routing; no cascade).

- [ ] **Step 3: Implement**

In `apps/harness/services.py`, delete `_preference_allows` and `PREFERENCE_TIER_GRACE_SECONDS` (and the comment block above them). Add:

```python
# Rank = availability cascade (spec 2026-07-24-directed-runner-routing). A lower
# rank may claim only while every better rank is unavailable — EXCEPT after the
# grace: an online-but-wedged runner (heartbeating, never claiming) must not
# stall an agent's queue forever, so a turn queued past the grace opens to the
# next assigned rank regardless of upstream availability.
CASCADE_GRACE_SECONDS = 60


def _assignment_allows(runner: Runner, turn: Turn, assignment_map: dict, now) -> bool:
    """assignment_map: {agent_id: [(rank, Runner), ...] ordered by rank}. False when
    this runner is not in the agent's list; True when it is and either every
    better-ranked runner is unavailable or the turn has aged past the grace."""
    rows = assignment_map.get(turn.agent_id) or []
    mine = next((rank for rank, r in rows if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for rank, r in rows if rank < mine)
```

In `claim_next_turn`:

1. Replace the agent leg of `target_q`. Change

```python
    target_q = Q(agent__slug__in=slugs) | Q(project__in=projects)
```

to

```python
    # Agent turns route by RunnerAssignment — the one canopy-web source of truth.
    # capabilities.agents no longer gates agent turns (it remains the runner's
    # project/session self-declaration). exclude_slugs (per-agent local pause)
    # still applies on top of assignments.
    agent_leg = Q(agent__runner_assignments__runner=runner)
    if exclude_slugs:
        agent_leg &= ~Q(agent__slug__in=list(exclude_slugs))
    target_q = agent_leg | Q(project__in=projects)
```

and delete the earlier `slugs = runner.agent_slugs()` / `exclude_slugs` slug-filter block (keep `projects` and `session_capable`). Update the early-exit to
`has_assignments = RunnerAssignment.objects.filter(runner=runner).exists()` and bail only when `not has_assignments and not projects and not session_capable and not has_pins`.

2. Before the candidate loop, build the map in two queries:

```python
    agent_ids = {t.agent_id for t in candidates if t.agent_id}
    assignment_map: dict = {}
    if agent_ids:
        rows = (
            RunnerAssignment.objects.filter(agent_id__in=agent_ids)
            .select_related("runner").order_by("rank")
        )
        for row in rows:
            assignment_map.setdefault(row.agent_id, []).append((row.rank, row.runner))
```

(materialize `candidates = list(candidates)` first so the queryset isn't executed twice.)

3. In the loop, replace the `_preference_allows` call:

```python
        if not pinned_here and turn.agent_id:
            if not _assignment_allows(runner, turn, assignment_map, now):
                continue
```

4. Import `RunnerAssignment` in the services module header: `from apps.harness.models import Runner, RunnerAssignment, Turn, TurnEvent` (match the existing import line's actual names).

- [ ] **Step 4: Run tests; fix existing tests that asserted capability-routing**

Run: `uv run pytest -q`. Existing tests that enqueue agent turns and claim via capability slugs will now fail — for each, add the matching `RunnerAssignment.objects.create(agent=..., runner=..., rank=0)` in their setup (do NOT weaken the new behavior). The kind-preference head-start tests (grep `_preference_allows` / `PREFERENCE_TIER_GRACE`) are superseded — delete them, their behavior is covered by the cascade tests.

- [ ] **Step 5: Commit**

```bash
git add apps/harness/services.py tests/
git commit -m "feat(harness): assignment cascade replaces capability+kind routing for agent turns"
```

---

### Task 5: Claim routing — session-binding stickiness

**Files:**
- Modify: `apps/harness/services.py` (`claim_next_turn` session leg)
- Test: `tests/test_runner_routing.py` (append)

**Interfaces:**
- Consumes: `RunnerBinding` (`apps/canopy_sessions/models.py`), session fixtures per existing chat tests.
- Produces: session-turn eligibility — bound+alive → binding holder only; bound-but-runner-gone → nobody (waits for placement); unbound agent session → assignment cascade ∩ `sessions:true`; unbound project session → any sessions-capable (unchanged).

- [ ] **Step 1: Write failing tests** (append; mirror chat-session fixtures from existing tests — grep `chat_session` in tests/)

```python
def _session(agent=None, workspace=None, project=""):
    from apps.canopy_sessions.models import Session
    return Session.objects.create(agent=agent, workspace=workspace, project=project)


def _bind(session, runner):
    from apps.canopy_sessions.models import RunnerBinding
    return RunnerBinding.objects.create(session=session, runner=runner, thread_key=str(session.id))


def test_bound_session_claims_only_on_binding_holder():
    u = _user(); ws = wsvc.create_workspace(owner=u, name="W1")
    holder = _online_runner("holder", u, capabilities={"sessions": True})
    other = _online_runner("other", u, capabilities={"sessions": True})
    s = _session(workspace=ws, project="canopy-web")
    _bind(s, holder)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s1")
    assert services.claim_next_turn(other) is None
    assert services.claim_next_turn(holder) is not None


def test_bound_session_with_offline_holder_waits_for_placement():
    u = _user(); ws = wsvc.create_workspace(owner=u, name="W1")
    holder = Runner.objects.create(name="gone", kind=Runner.EMDASH,
                                   capabilities={"sessions": True}, paired_by=u)
    other = _online_runner("other", u, capabilities={"sessions": True})
    s = _session(workspace=ws, project="canopy-web")
    _bind(s, holder)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s2")
    assert services.claim_next_turn(other) is None  # nobody claims until user places


def test_unbound_agent_session_follows_assignment_order():
    u = _user(); ws = wsvc.create_workspace(owner=u, name="W1")
    a = _agent("echo", workspace=ws)
    r0 = Runner.objects.create(name="r0", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=u)
    r1 = _online_runner("r1", u, capabilities={"sessions": True})
    _assign(a, r0, 0); _assign(a, r1, 1)
    s = _session(agent=a, workspace=ws)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s3")
    assert services.claim_next_turn(r1) is not None  # r0 offline → r1 (rank 1) takes it
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_runner_routing.py -v -k session`

- [ ] **Step 3: Implement**

In `claim_next_turn`, replace the session leg of `target_q` (`if session_capable: target_q | Q(chat_session__isnull=False)`) with a binding-aware leg:

```python
    if session_capable:
        # Stickiness: a bound session's turns go to the binding holder ONLY. A
        # bound session whose holder is gone claims NOWHERE until the user places
        # it (chat offers wait/continue). Unbound sessions are open here and
        # refined per-candidate below (agent sessions follow the assignment
        # cascade; project sessions stay any-sessions-capable).
        session_leg = Q(chat_session__isnull=False) & (
            Q(chat_session__runner_binding__isnull=True)
            | Q(chat_session__runner_binding__runner__isnull=True)
            | Q(chat_session__runner_binding__runner=runner)
        )
        target_q = target_q | session_leg
```

In the candidate loop, refine unbound **agent** sessions with the cascade (bound-to-me and project sessions pass through):

```python
        if not pinned_here and turn.chat_session_id:
            sess = turn.chat_session
            binding = getattr(sess, "runner_binding", None)
            bound_to_me = binding is not None and binding.runner_id == runner.id
            if not bound_to_me and sess.agent_id:
                if not _assignment_allows_for_agent(runner, sess.agent_id, turn, assignment_map, now):
                    continue
```

Refactor `_assignment_allows` so both paths share it:

```python
def _assignment_allows_for_agent(runner, agent_id, turn, assignment_map, now) -> bool:
    rows = assignment_map.get(agent_id) or []
    mine = next((rank for rank, r in rows if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for rank, r in rows if rank < mine)


def _assignment_allows(runner, turn, assignment_map, now) -> bool:
    return _assignment_allows_for_agent(runner, turn.agent_id, turn, assignment_map, now)
```

Extend the assignment_map agent-id collection to include session agents:
`agent_ids = {t.agent_id for t in candidates if t.agent_id} | {t.chat_session.agent_id for t in candidates if t.chat_session_id and t.chat_session.agent_id}` and add `.select_related("agent", "chat_session", "chat_session__runner_binding")` to the candidates query.

- [ ] **Step 4: Run tests** — `uv run pytest -q`; fix any chat tests that relied on any-runner claiming a bound session by binding them to the claiming runner in setup.

- [ ] **Step 5: Commit**

```bash
git add apps/harness/services.py tests/
git commit -m "feat(harness): session turns sticky to their binding holder; unbound agent chats follow the cascade"
```

---

### Task 6: Assignments API — GET/PUT /api/agents/{slug}/runners

**Files:**
- Modify: `apps/agents/api.py`, `apps/agents/schemas.py`
- Test: `tests/test_agents_api.py` (append; follow its existing client/auth fixtures)

**Interfaces:**
- Produces: `GET /api/agents/{slug}/runners` → `list[AgentRunnerOut]`; `PUT` body `AgentRunnersIn{runner_ids: list[UUID]}` replaces the list wholesale (index = rank), 422 on unknown/retired runner id. Schemas:

```python
# apps/agents/schemas.py — add
class AgentRunnerOut(Schema):
    runner_id: uuid.UUID
    runner_name: str
    kind: str
    rank: int
    online: bool
    ready: bool


class AgentRunnersIn(Schema):
    runner_ids: list[uuid.UUID]
```

- [ ] **Step 1: Failing tests** — GET returns seeded order; PUT reorders/removes/adds atomically; PUT with a retired runner id → 422; PUT `[]` empties the list.

```python
def test_put_agent_runners_replaces_ordered_list(client_logged_in, agent, runner_a, runner_b):
    r = client_logged_in.put(
        f"/api/agents/{agent.slug}/runners",
        data=json.dumps({"runner_ids": [str(runner_b.id), str(runner_a.id)]}),
        content_type="application/json",
    )
    assert r.status_code == 200
    got = client_logged_in.get(f"/api/agents/{agent.slug}/runners").json()
    assert [x["runner_name"] for x in got] == [runner_b.name, runner_a.name]
    assert [x["rank"] for x in got] == [0, 1]
```

(Adapt fixture names to the file's actual conventions.)

- [ ] **Step 2: Verify failure**, then **Step 3: implement** in `apps/agents/api.py`:

```python
@router.get("/{slug}/runners", response=list[AgentRunnerOut])
def list_agent_runners(request: HttpRequest, slug: str):
    agent = _agent_or_404(request, slug)
    return [
        AgentRunnerOut(
            runner_id=a.runner_id, runner_name=a.runner.name, kind=a.runner.kind,
            rank=a.rank, online=a.runner.live_status == a.runner.ONLINE, ready=a.runner.ready,
        )
        for a in agent.runner_assignments.select_related("runner")
    ]


@router.put("/{slug}/runners", response=list[AgentRunnerOut])
def replace_agent_runners(request: HttpRequest, slug: str, payload: AgentRunnersIn):
    """Replace the agent's ORDERED runner list (index = rank) — the single
    routing authority (spec 2026-07-24). Wholesale replace: the matrix UI saves a
    full row, so there is no partial-update ambiguity."""
    from apps.harness.models import Runner, RunnerAssignment

    agent = _agent_or_404(request, slug)
    runners = list(Runner.objects.filter(id__in=payload.runner_ids).exclude(status=Runner.RETIRED))
    by_id = {r.id: r for r in runners}
    missing = [str(rid) for rid in payload.runner_ids if rid not in by_id]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    with transaction.atomic():
        RunnerAssignment.objects.filter(agent=agent).delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(agent=agent, runner=by_id[rid], rank=i)
            for i, rid in enumerate(payload.runner_ids)
        ])
    return list_agent_runners(request, slug)
```

Match the file's actual `_agent_or_404`/auth helper names; add imports (`transaction`, schemas).

- [ ] **Step 4: Tests pass** — `uv run pytest tests/test_agents_api.py -q`
- [ ] **Step 5: Commit** — `git commit -m "feat(agents): GET/PUT /runners — the ordered runner-assignment API"`

---

### Task 7: Drill service + endpoints + report callback

**Files:**
- Modify: `apps/harness/services.py`, `apps/harness/api.py`, `apps/harness/schemas.py`
- Test: `tests/test_runner_drills.py` (new)

**Interfaces:**
- Produces:
  - `services.start_drill(runner, agents: list[Agent]) -> list[RunnerDrill]` — upserts rows to pending, enqueues pinned drill turns.
  - `services.DRILL_PROMPT` template with `{agent_slug}`, `{report_url}`, `{drill_id}` slots.
  - `services.report_drill(drill: RunnerDrill, *, outcome: str, summary: str) -> RunnerDrill`.
  - `finish_turn` hook: a drill turn finishing `failed` marks its drill `fail` (summary = result_note) if still pending.
  - API: `POST /runners/{runner_id}/drill` (body `DrillIn{agents: list[str] | None}`) → `list[RunnerDrillOut]`; `GET /runners/{runner_id}/drills` → `list[RunnerDrillOut]`; `POST /drills/{drill_id}/report` (body `DrillReportIn{outcome: Literal["pass","fail"], summary: str}`) → `RunnerDrillOut`.
  - `RunnerDrillOut`: `id: int, agent_slug: str, outcome: str, summary: str, started_at, finished_at, turn_id: uuid.UUID | None`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_runner_drills.py
import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, RunnerDrill, Turn

pytestmark = pytest.mark.django_db


def test_start_drill_fans_out_pinned_pending_turns(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="standby", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s) for s in ("echo", "ada"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)
    drills = services.start_drill(r, [a1, a2])
    assert {d.outcome for d in drills} == {RunnerDrill.OUTCOME_PENDING}
    turns = Turn.objects.filter(origin=Turn.ORIGIN_DRILL)
    assert turns.count() == 2
    assert all(t.pinned_runner_id == r.id for t in turns)
    assert "read-only" in turns.first().prompt.lower()


def test_report_drill_resolves_outcome(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u)
    a = Agent.objects.create(slug="echo", name="Echo")
    d = RunnerDrill.objects.create(runner=r, agent=a)
    services.report_drill(d, outcome="pass", summary="all checks green")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PASS and d.finished_at is not None


def test_failed_drill_turn_marks_drill_fail(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo")
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    turn = Turn.objects.get(origin=Turn.ORIGIN_DRILL)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=r)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.FAILED, result_note="claude auth expired")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_FAIL
    assert "claude auth expired" in d.summary


def test_redrill_resets_to_pending(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo")
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    services.report_drill(d, outcome="pass", summary="ok")
    [d2] = services.start_drill(r, [a])
    assert d2.pk == d.pk and d2.outcome == RunnerDrill.OUTCOME_PENDING
```

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_runner_drills.py -v`

- [ ] **Step 3: Implement services** (`apps/harness/services.py`):

```python
DRILL_PROMPT = """READINESS DRILL — READ-ONLY. You are the agent "{agent_slug}".
Verify you can operate end-to-end in THIS environment, then report.

1. Confirm your working environment. If your agent repo is not checked out here,
   clone it (read-only credentials are staged in this environment).
2. Run your doctor / preflight / setup-verification checks. READ-ONLY mode:
   take NO outward action — no emails, no posts, no board writes, no deploys,
   no state mutations anywhere.
3. Report the result back to canopy-web (this callback is part of the drill —
   it proves this environment can reach the control plane):

   curl -s -X POST "{report_url}" \\
     -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token 2>/dev/null || echo "$CANOPY_PAT")" \\
     -H "Content-Type: application/json" \\
     -d '{{"outcome": "pass", "summary": "<one-paragraph findings>"}}'

   Use "outcome": "fail" if ANY check failed, and say which. Keep the summary to
   one paragraph. Do nothing after reporting."""


def start_drill(runner: Runner, agents: list) -> list[RunnerDrill]:
    """Fan a readiness drill out over `agents`: reset each (runner, agent)
    RunnerDrill to pending and enqueue one hard-pinned, read-only doctor turn
    per agent. Drills queue behind real executing turns (the one-executing-turn
    constraint) — they never interrupt live work."""
    import uuid as uuidlib

    drills: list[RunnerDrill] = []
    for agent in agents:
        drill, _ = RunnerDrill.objects.update_or_create(
            runner=runner, agent=agent,
            defaults={"outcome": RunnerDrill.OUTCOME_PENDING, "summary": "",
                      "finished_at": None, "started_at": timezone.now()},
        )
        report_url = f"{settings.CANOPY_PUBLIC_BASE_URL}/api/harness/drills/{drill.id}/report"
        turn, _created = enqueue_turn(
            agent=agent,
            origin=Turn.ORIGIN_DRILL,
            idempotency_key=f"drill:{runner.id}:{agent.slug}:{uuidlib.uuid4().hex[:8]}",
            prompt=DRILL_PROMPT.format(agent_slug=agent.slug, report_url=report_url),
            pinned_runner=runner,
        )
        drill.turn = turn
        drill.save(update_fields=["turn"])
        drills.append(drill)
    return drills


def report_drill(drill: RunnerDrill, *, outcome: str, summary: str) -> RunnerDrill:
    if outcome not in (RunnerDrill.OUTCOME_PASS, RunnerDrill.OUTCOME_FAIL):
        raise ValueError(f"outcome must be pass|fail, got {outcome!r}")
    drill.outcome = outcome
    drill.summary = summary
    drill.finished_at = timezone.now()
    drill.save(update_fields=["outcome", "summary", "finished_at"])
    return drill
```

`started_at` has `auto_now_add`, so change the model field to `started_at = models.DateTimeField(default=timezone.now)` in a small follow-up migration so `update_or_create` can reset it (makemigrations will pick it up).

`CANOPY_PUBLIC_BASE_URL`: if no such setting exists (grep `config/settings`), add to `config/settings/base.py`: `CANOPY_PUBLIC_BASE_URL = env("CANOPY_PUBLIC_BASE_URL", default="http://localhost:8000")` following the file's env-var idiom, and set it to `https://labs.connect.dimagi.com/canopy` in `connectlabs.py`.

In `finish_turn`, after the `updated`/append_events block, add:

```python
    if turn.origin == Turn.ORIGIN_DRILL and status == Turn.FAILED:
        RunnerDrill.objects.filter(
            turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
        ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary=result_note or "drill turn failed",
                 finished_at=now)
```

- [ ] **Step 4: Implement API** (`apps/harness/api.py` + `schemas.py`):

```python
# schemas.py
class RunnerDrillOut(Schema):
    id: int
    agent_slug: str
    outcome: str
    summary: str
    started_at: datetime
    finished_at: datetime | None
    turn_id: uuid.UUID | None

    @staticmethod
    def resolve_agent_slug(obj):
        return obj.agent.slug


class DrillIn(Schema):
    agents: list[str] | None = None


class DrillReportIn(Schema):
    outcome: Literal["pass", "fail"]
    summary: str = ""
```

```python
# api.py
@router.post("/runners/{runner_id}/drill", response=list[RunnerDrillOut])
def start_runner_drill(request: HttpRequest, runner_id: uuid.UUID, payload: DrillIn):
    """Fan out a readiness drill (owner-gated). Default: every agent assigned to
    this runner; body.agents narrows by slug."""
    from apps.agents.models import Agent

    runner = _runner_or_404(request, runner_id)
    assigned = Agent.objects.filter(runner_assignments__runner=runner)
    agents = list(assigned.filter(slug__in=payload.agents) if payload.agents else assigned)
    if not agents:
        raise HttpError(422, "no assigned agents to drill — assign this runner to an agent first")
    return services.start_drill(runner, agents)


@router.get("/runners/{runner_id}/drills", response=list[RunnerDrillOut])
def list_runner_drills(request: HttpRequest, runner_id: uuid.UUID):
    runner = _runner_or_404(request, runner_id)
    return list(runner.drills.select_related("agent"))


@router.post("/drills/{drill_id}/report", response=RunnerDrillOut)
def report_drill(request: HttpRequest, drill_id: int, payload: DrillReportIn):
    """The drilled agent's callback. Gated like every runner route: the caller
    must be the drilled runner's owner (the agent runs under the owner's
    environment token, so this proves control-plane reachability too)."""
    drill = get_object_or_404(
        RunnerDrill.objects.select_related("runner", "agent"), pk=drill_id
    )
    _runner_or_404(request, drill.runner_id)  # reuse the owner gate; 404 on non-owner
    return services.report_drill(drill, outcome=payload.outcome, summary=payload.summary)
```

Match import style; add `RunnerDrill` import to api.py.

- [ ] **Step 5: All tests pass + commit**

Run: `uv run pytest tests/test_runner_drills.py tests/ -q`

```bash
git add apps/harness/ config/settings/ tests/test_runner_drills.py
git commit -m "feat(harness): readiness drills — pinned doctor turns, report callback, per-pair outcomes"
```

---

### Task 8: RunnerOut.drill_rollup + runner-preference deprecation

**Files:**
- Modify: `apps/harness/schemas.py` (`RunnerOut`), `apps/agents/api.py` (runner-preference endpoint docstring/response)
- Test: `tests/test_runner_drills.py` (append)

**Interfaces:**
- Produces: `RunnerOut.drill_rollup: DrillRollup | None` where `DrillRollup(passed: int, failed: int, pending: int, last_finished_at: datetime | None)`; computed via resolver from `runner.drills`. `PATCH /api/agents/{slug}/runner-preference` responds normally but its OpenAPI description gains "DEPRECATED: superseded by PUT /api/agents/{slug}/runners; removed next release."

- [ ] **Step 1: Failing test** — list runners API includes rollup counts after a drill + report.
- [ ] **Step 2: Implement**

```python
class DrillRollup(Schema):
    passed: int
    failed: int
    pending: int
    last_finished_at: datetime | None


class RunnerOut(Schema):
    ...  # existing fields stay
    drill_rollup: DrillRollup | None = None

    @staticmethod
    def resolve_drill_rollup(obj):
        rows = list(obj.drills.all())
        if not rows:
            return None
        return DrillRollup(
            passed=sum(1 for d in rows if d.outcome == "pass"),
            failed=sum(1 for d in rows if d.outcome == "fail"),
            pending=sum(1 for d in rows if d.outcome == "pending"),
            last_finished_at=max((d.finished_at for d in rows if d.finished_at), default=None),
        )
```

Add `.prefetch_related("drills")` to `list_runners`' queryset. Update the runner-preference endpoint's docstring first line to the deprecation sentence.

- [ ] **Step 3: Tests + commit** — `git commit -m "feat(harness): drill rollup on RunnerOut; deprecate kind runner-preference"`

---

### Task 9: Session placement — runner_id on create, placement on send, /place

**Files:**
- Modify: `apps/canopy_sessions/api.py`, `apps/canopy_sessions/schemas.py` (or inline schemas), `apps/canopy_sessions/services.py`
- Test: `tests/test_canopy_sessions.py` (append to existing chat test file; find by `grep -l send_message tests/`)

**Interfaces:**
- Consumes: `enqueue_turn(pinned_runner=...)` (Task 3).
- Produces:
  - `SessionCreateIn` gains `runner_id: uuid.UUID | None = None`; create stores it as `session.metadata["requested_runner_id"]`.
  - `services.send_message(...)` pins: explicit `placement` param wins; else a live binding is left to claim-time stickiness; else `requested_runner_id` (first turn of a directed new chat) pins there.
  - `SendIn` gains `placement: str | None = None` (`"wait"` or a runner UUID string).
  - `POST /api/canopy-sessions/{id}/place` body `PlaceIn{placement: str}` re-pins that session's oldest QUEUED turn (404 if none) — the banner's after-the-fact decision.

- [ ] **Step 1: Failing tests**

Locate the chat test module (`grep -rl "send_message" tests/`) and reuse its session/workspace fixtures; the tests below show the full logic with plain ORM setup — adapt only the fixture plumbing:

```python
def test_directed_new_chat_pins_first_turn(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    ws = wsvc.create_workspace(owner=u, name="W1")
    r2 = Runner.objects.create(name="r2", kind=Runner.EMDASH,
                               capabilities={"sessions": True}, paired_by=u)
    s = Session.objects.create(workspace=ws, project="canopy-web",
                               metadata={"requested_runner_id": str(r2.id)})
    chat_services.send_message(session=s, user=u, text="hi", client_id="c1")
    turn = Turn.objects.get(chat_session=s)
    assert turn.pinned_runner_id == r2.id


def test_send_placement_wait_pins_to_bound_runner(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    ws = wsvc.create_workspace(owner=u, name="W1")
    r1 = Runner.objects.create(name="r1", kind=Runner.EMDASH,
                               capabilities={"sessions": True}, paired_by=u)  # offline
    s = Session.objects.create(workspace=ws, project="canopy-web")
    RunnerBinding.objects.create(session=s, runner=r1, thread_key=str(s.id))
    chat_services.send_message(session=s, user=u, text="hi", client_id="c2", placement="wait")
    turn = Turn.objects.get(chat_session=s)
    assert turn.pinned_runner_id == r1.id


def test_place_repins_queued_turn(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    ws = wsvc.create_workspace(owner=u, name="W1")
    r1 = Runner.objects.create(name="r1", kind=Runner.EMDASH,
                               capabilities={"sessions": True}, paired_by=u)
    r2 = Runner.objects.create(name="r2", kind=Runner.EMDASH,
                               capabilities={"sessions": True}, paired_by=u)
    s = Session.objects.create(workspace=ws, project="canopy-web")
    RunnerBinding.objects.create(session=s, runner=r1, thread_key=str(s.id))
    chat_services.send_message(session=s, user=u, text="hi", client_id="c3")
    chat_services.place_queued_turn(session=s, placement=str(r2.id))  # service form of /place
    turn = Turn.objects.get(chat_session=s)
    assert turn.pinned_runner_id == r2.id
```

(Expose the `/place` logic as `services.place_queued_turn(session, placement)` called by the API route, so it is testable without HTTP; match `send_message`'s real signature when wiring `placement` through.)

- [ ] **Step 2: Implement** — in `send_message` (`apps/canopy_sessions/services.py`, the `enqueue_turn` call at ~line 249):

```python
    pinned = None
    if placement == "wait":
        binding = getattr(session, "runner_binding", None)
        pinned = binding.runner if binding and binding.runner_id else None
    elif placement:
        from apps.harness.models import Runner
        pinned = Runner.objects.filter(id=placement).exclude(status=Runner.RETIRED).first()
        if pinned is None:
            raise ValueError("unknown runner for placement")
    elif not getattr(session, "runner_binding", None):
        rid = (session.metadata or {}).get("requested_runner_id")
        if rid:
            from apps.harness.models import Runner
            pinned = Runner.objects.filter(id=rid).exclude(status=Runner.RETIRED).first()
    turn, _ = harness_services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_API, idempotency_key=..., prompt=text,
        origin_ref=..., pinned_runner=pinned,
    )
```

(`placement: str | None = None` threaded from the API schema into `send_message`.) The `/place` endpoint:

```python
@router.post("/{session_id}/place", response=TurnOutMinimal)
def place_queued_turn(request, session_id: uuid.UUID, payload: PlaceIn):
    session = _session_or_404(request, session_id)
    turn = Turn.objects.filter(chat_session=session, status=Turn.QUEUED).order_by("created_at").first()
    if turn is None:
        raise HttpError(404, "no queued turn to place")
    if payload.placement == "wait":
        binding = getattr(session, "runner_binding", None)
        if not (binding and binding.runner_id):
            raise HttpError(422, "session has no bound runner to wait for")
        turn.pinned_runner_id = binding.runner_id
    else:
        from apps.harness.models import Runner
        r = Runner.objects.filter(id=payload.placement).exclude(status=Runner.RETIRED).first()
        if r is None:
            raise HttpError(422, "unknown runner")
        turn.pinned_runner = r
    turn.save(update_fields=["pinned_runner"])
    return turn
```

**Important:** a `wait`-pinned turn on a bound session is already claimable only by the holder (stickiness); the pin's value is that when the binding is later re-pointed by "continue elsewhere", an explicit `wait` still holds the turn to the original runner. A `continue` placement must ALSO clear the stickiness block — the session leg in `claim_next_turn` already admits `pinned_runner=runner` turns via the pin filter before the binding check; verify with the test.

- [ ] **Step 3: Tests + regen guard + commit**

```bash
uv run pytest -q
git add apps/canopy_sessions/ tests/
git commit -m "feat(chat): directed session placement — runner_id on create, wait/continue on send"
```

---

### Task 10: Regenerate OpenAPI types

**Files:**
- Modify: `frontend/src/api/generated.ts`

- [ ] **Step 1:** Start backend (`uv run python manage.py runserver`) in background; `cd frontend && npm run gen:api`.
- [ ] **Step 2:** `npm run build` — expect type errors ONLY where new fields are unused (none yet) — must pass.
- [ ] **Step 3:** Commit: `git commit -m "chore: regenerate API types for routing/drill/placement endpoints"`

---

### Task 11: Frontend API helpers

**Files:**
- Modify: `frontend/src/api/agents.ts`, `frontend/src/api/chat.ts`, create `frontend/src/api/drills.ts`

**Interfaces:**
- Produces (typed via `generated.ts`):
  - `agents.ts`: `getAgentRunners(slug): Promise<AgentRunnerOut[]>`, `putAgentRunners(slug, runnerIds: string[]): Promise<AgentRunnerOut[]>`
  - `drills.ts`: `startDrill(runnerId, agents?: string[])`, `listDrills(runnerId)`
  - `chat.ts`: `createSession` gains optional `runnerId`; `placeTurn(sessionId, placement: string)`; `sendMessage` gains optional `placement`.

- [ ] **Step 1:** Implement following the `updateAgentRunnerPreference` idiom (apiV2 + unwrap). Example:

```ts
export async function putAgentRunners(
  slug: string,
  runnerIds: readonly string[],
): Promise<AgentRunnerOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/runners', {
    params: { path: { slug } },
    body: { runner_ids: [...runnerIds] },
  })
  return unwrap(res, 'putAgentRunners')
}
```

- [ ] **Step 2:** `npm run build` passes. Commit: `git commit -m "feat(frontend): API helpers for assignments, drills, placement"`

---

### Task 12: Routing matrix UI

**Files:**
- Create: `frontend/src/components/agents/RunnerAssignments.tsx`
- Modify: `frontend/src/pages/agents/AgentOverviewSection.tsx` (~line 147 — replace `<RunnerOrder …/>`), `frontend/src/components/supervisor/RunnerDetail.tsx` (~line 66 — replace `<RunnerOrder …/>`), `frontend/src/pages/SupervisorPage.tsx` (add matrix section)
- Delete (after both mounts swapped): `frontend/src/components/agents/RunnerOrder.tsx`, `frontend/src/components/supervisor/runnerPriority.ts` + its test

**Interfaces:**
- Consumes: `getAgentRunners`/`putAgentRunners`, `listRunners` (existing helper for the fleet).
- Produces: `<RunnerAssignments agentSlug />` — a single row editor: ordered chips (status dot + name + kind), ↑/↓ reorder buttons on each chip (buttons, not drag — reliable and keyboard-accessible; drag can come later), `×` remove, a `+ add` menu listing unassigned non-retired runners; empty state renders a `warning`-toned "unroutable — no runners assigned" chip. `<RoutingMatrix />` — one `RunnerAssignments` row per agent with the agent name in a left column; mounted in a "Routing" section on `/supervisor`.

- [ ] **Step 1:** Build `RunnerAssignments.tsx` (complete component: local state from `getAgentRunners`, optimistic `putAgentRunners` on every mutation, `bg-card border-border` chips, `text-muted-foreground` meta, `success`/`destructive` dots for online/offline, `warning` for the empty state).
- [ ] **Step 2:** Mount in both places; build `RoutingMatrix` in `SupervisorPage` (rows from the existing agents list the page already loads; dense rows, no cards-in-cards).
- [ ] **Step 3:** Remove `RunnerOrder.tsx` + `runnerPriority.ts` and their imports/tests.
- [ ] **Step 4:** `npm run build` + eyeball via dev server. Commit: `git commit -m "feat(frontend): routing matrix — per-agent ranked runner assignments UI"`

---

### Task 13: Drill UI

**Files:**
- Create: `frontend/src/components/supervisor/RunnerDrills.tsx`
- Modify: `frontend/src/components/supervisor/RunnerDetail.tsx` (badge + Drill button + grid), `RunnerStatus.tsx` (rollup badge on the card row)

**Interfaces:**
- Consumes: `startDrill`/`listDrills`, `drill_rollup` on `RunnerOut`.
- Produces: badge text `drilled {relative(last_finished_at)} — {passed}/{passed+failed+pending}`; grid rows: agent, outcome chip (`success`/`destructive`/`warning` tint for pass/fail/pending), age, link to `/w/{ws}/agents/{slug}/turns` filtered by the drill turn id where the turn detail already renders; pending rows older than 30 min render as `timed out` (client-side only, per spec). "Drill runner" button calls `startDrill` then polls `listDrills` every 10s while any row is pending.

- [ ] **Step 1:** Implement; **Step 2:** `npm run build`; **Step 3:** commit `git commit -m "feat(frontend): runner drill badge + per-agent readiness grid"`

---

### Task 14: Chat placement UI

**Files:**
- Modify: `frontend/src/components/chat/ChatSessionsPanel.tsx` (new-chat block, lines ~117-146), `frontend/src/pages/ChatPage.tsx` (header/banner area, lines ~168-214)

**Interfaces:**
- Consumes: `createSession({..., runnerId})`, `placeTurn`, session `runner_name`/`running` fields already projected.
- Produces:
  - New chat: a "Run on" `<select>` (default `auto`) listing sessions-capable runners of the picked agent (fetch via `getAgentRunners`, filter client-side on `online`; project chats list all online runners from `listRunners`). Value passed to `createSession` only when not `auto`.
  - ChatPage: when the session has a binding whose runner is offline AND a queued turn exists (both derivable from the session payload's `runner_name` + existing turn state the page already tracks), render a banner: `"{runner_name} is offline"` with buttons **Wait for it** → `placeTurn(id, "wait")` and **Continue on…** → runner select → `placeTurn(id, runnerId)`. Banner uses `bg-warning/10 text-warning border-warning/30`.

- [ ] **Step 1:** Implement both; **Step 2:** `npm run build`; manual smoke via dev server against local backend; **Step 3:** commit `git commit -m "feat(chat): run-on picker + offline wait/continue placement banner"`

---

### Task 15: Docs + final gates

**Files:**
- Modify: `CLAUDE.md` (API Endpoints — harness + agents + chat sections; Design Decisions runner_preference mention)

- [ ] **Step 1:** Add the new routes to CLAUDE.md's endpoint lists; note assignments as the routing authority and mark runner-preference deprecated.
- [ ] **Step 2:** Full gates: `uv run pytest -q` AND `cd frontend && npm run build` AND regen check (`npm run gen:api` produces no diff).
- [ ] **Step 3:** Commit: `git commit -m "docs: directed runner routing — endpoints + design decisions"`
