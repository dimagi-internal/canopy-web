# Schedule CRUD over the canopy-web MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `AgentSchedule` CRUD (list/create/update/delete/run-now/preview) as canopy-web MCP tools, by extracting a request-free service layer the REST routes and the MCP tools both call so the two surfaces can't drift.

**Architecture:** A new `apps/harness/schedule_services.py` holds the logic currently inline in the Ninja route handlers (auth resolution, duplicate-name detection, the savepoint, supersede-before-delete). It takes a `user` (not a `request`) and raises two domain exceptions instead of `HttpError`. The REST routes become thin (call the service, map exceptions → HTTP, keep their Pydantic schemas); six `@mcp.tool` wrappers call the same service and map exceptions → re-raise after auditing.

**Tech Stack:** Django 5, Django Ninja 1.6, Pydantic v2, FastMCP 3.x, `canopy_cron`, pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-schedule-mcp-tools-design.md` — read it before Task 1.

## Global Constraints

- **Framework tier.** `apps/harness` must never import product apps (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`). `apps.workspaces`/`apps.agents` are framework — importing them is fine. `tests/test_architecture_boundary.py` enforces it.
- **The service must not import Ninja or raise `HttpError`.** It raises `ScheduleNotFound` / `DuplicateScheduleName`. The MCP would otherwise inherit an HTTP dependency it has no use for.
- **The extraction is behavior-preserving.** `tests/test_schedule_api.py` must pass **unchanged** — it is the regression gate on the refactor. Do not edit it.
- **Reuse existing services, don't reimplement.** `apps/harness/services.py` already has `supersede_open_turns(schedule, *, reason)`, `run_schedule_now(schedule) -> Turn`, `latest_occurrence_turn(schedule)`. The new module calls these.
- **`AgentSchedule` has NO FK to `Turn`.** Deleting a schedule must call `supersede_open_turns` **before** `.delete()`, or an executing occurrence becomes permanently unreleasable and wedges the agent forever.
- **Duplicate name → 409, in a savepoint.** `uniq_agent_schedule_name` raises `IntegrityError`; catch it inside `transaction.atomic()` (SESSION_SAVE_EVERY_REQUEST poisons an un-savepointed transaction → 400 instead of 409).
- **Errors are RFC 7807** (`apps/api/errors.py`, `TYPE_CONFLICT` for the 409). 404 never 403 for tenancy — one `ScheduleNotFound` for missing/wrong-tenant/non-member so existence never leaks.
- **MCP tools follow `apps/mcp/tools/insights.py` exactly:** `@mcp.tool` async → `current_user_id()` → (writes) `check_write_limit(user_id)` → `sync_to_async(fn, thread_sensitive=True)` → `write_audit(...)` on both success and exception paths.
- **Cron/timezone validation** stays in the Pydantic schema for REST; the MCP tools apply it explicitly via `canopy_cron.validate_cron` / `validate_timezone`. The service assumes validated input.
- Run backend tests with `uv run pytest`. Lint with `uv run ruff check apps/`. Repo ruff selects `UP` at py311 (`dt.UTC`, not `dt.timezone.utc`).

---

### Task 1: Domain exceptions + request-free auth resolvers

**Files:**
- Create: `apps/harness/schedule_services.py`
- Test: `tests/test_schedule_services_crud.py`

**Interfaces:**
- Consumes: `apps.agents.models.Agent`, `apps.harness.models.AgentSchedule`, `apps.workspaces.services` (`auto_join_workspaces`, `is_member`).
- Produces:
  - `class ScheduleNotFound(Exception)` — agent missing / wrong tenant / non-member / schedule not under this agent.
  - `class DuplicateScheduleName(Exception)` — carries `.name`.
  - `_resolve_agent(user, agent_slug: str, *, workspace_slug: str | None = None) -> Agent`
  - `_resolve_schedule(user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None) -> AgentSchedule`

The resolvers replicate `apps/harness/api.py::_agent_or_404` **exactly**, minus the `request` — the tenant pin becomes a parameter. Read that function first.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_services_crud.py`:

```python
"""Request-free schedule service layer — auth resolution + CRUD, shared by the
REST routes and the MCP tools."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness import schedule_services as ss
from apps.harness.models import AgentSchedule
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", created_by=owner, auto_join_domains=[]
    )
    wsvc.ensure_member(w, owner, WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def agent(ws):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws)


def test_resolve_agent_for_member(owner, agent):
    assert ss._resolve_agent(owner, "eva").slug == "eva"


def test_resolve_agent_missing_raises_not_found(owner, ws):
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(owner, "nope")


def test_resolve_agent_non_member_raises_not_found(agent):
    """A non-member gets ScheduleNotFound — the same as a missing agent, so
    tenancy never leaks existence."""
    outsider = User.objects.create_user("mallory", "mallory@evil.com", "pw")
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(outsider, "eva")


def test_resolve_agent_wrong_tenant_pin_raises_not_found(owner, agent):
    """The workspace_slug pin (the REST tenant-URL) must match the agent's."""
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_agent(owner, "eva", workspace_slug="some-other-ws")


def test_resolve_schedule_wrong_agent_raises_not_found(owner, agent, ws):
    other = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    sched = AgentSchedule.objects.create(
        agent=other, name="s", prompt="p", cron="0 9 * * 5", timezone="UTC"
    )
    with pytest.raises(ss.ScheduleNotFound):
        ss._resolve_schedule(owner, "eva", sched.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_services_crud.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.harness.schedule_services'`

- [ ] **Step 3: Write the module skeleton**

Create `apps/harness/schedule_services.py`:

```python
"""Request-free schedule service layer.

The MCP invariant (apps/mcp/tools/insights.py) is that tools call the SAME
service functions as the REST views, so the two surfaces can't drift. Schedules
had no such layer — create/update/delete were inline in the Ninja handlers — so
this module extracts them. It takes a `user` (not a `request`) and raises domain
exceptions (not HttpError), so both the REST routes and the MCP tools can call
it. REST maps the exceptions to HTTP; the MCP re-raises them after auditing.

Reuses apps.harness.services for the turn-lifecycle operations
(supersede_open_turns / run_schedule_now / latest_occurrence_turn).
"""
from __future__ import annotations

import datetime as dt

from canopy_cron import next_slots
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import services
from .models import AgentSchedule


class ScheduleNotFound(Exception):
    """Agent missing / wrong tenant / non-member / schedule not under this agent.

    One type for all four, so a non-member cannot distinguish 'no such agent'
    from 'not yours' — existence never leaks."""


class DuplicateScheduleName(Exception):
    """The uniq_agent_schedule_name constraint was violated on create/update."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _resolve_agent(user, agent_slug: str, *, workspace_slug: str | None = None) -> Agent:
    """Resolve an agent, gated by workspace membership. Request-free twin of
    apps/harness/api.py::_agent_or_404 — the tenant-URL pin is a parameter.

    REST passes request.workspace_slug (preserving today's behavior); the MCP
    passes None (membership gating only, no tenant-URL concept). Every failure
    raises ScheduleNotFound — 404-not-403 on the REST side, no existence leak."""
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent is None:
        raise ScheduleNotFound(agent_slug)
    wsvc.auto_join_workspaces(user)
    if workspace_slug and agent.workspace_id != workspace_slug:
        raise ScheduleNotFound(agent_slug)  # wrong tenant
    if agent.workspace_id and not wsvc.is_member(user, agent.workspace_id):
        raise ScheduleNotFound(agent_slug)
    return agent


def _resolve_schedule(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    schedule = AgentSchedule.objects.filter(pk=schedule_id, agent=agent).first()
    if schedule is None:
        raise ScheduleNotFound(f"{agent_slug}/{schedule_id}")
    return schedule
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_services_crud.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/harness/schedule_services.py tests/test_schedule_services_crud.py
git commit -m "feat(harness): request-free schedule auth resolvers + domain exceptions"
```

---

### Task 2: The CRUD service functions + dict serializer

**Files:**
- Modify: `apps/harness/schedule_services.py`
- Test: `tests/test_schedule_services_crud.py`

**Interfaces:**
- Consumes: Task 1's resolvers + exceptions; `apps.harness.services.{supersede_open_turns, run_schedule_now, latest_occurrence_turn}`; `canopy_cron.next_slots`.
- Produces:
  - `serialize_schedule(schedule: AgentSchedule) -> dict` — the single shape (incl. `fire_after`, `next_runs`, `last_status`), consumed by both REST's `ScheduleOut(**d)` and the tools' dict return.
  - `list_schedules(user, agent_slug, *, workspace_slug=None) -> list[AgentSchedule]`
  - `create_schedule(user, agent_slug, fields: dict, *, workspace_slug=None) -> AgentSchedule`
  - `update_schedule(user, agent_slug, schedule_id, fields: dict, *, workspace_slug=None) -> AgentSchedule`
  - `delete_schedule(user, agent_slug, schedule_id, *, workspace_slug=None) -> None`
  - `run_schedule_now(user, agent_slug, schedule_id, *, workspace_slug=None) -> AgentSchedule`
  - `preview_cron(user, agent_slug, cron: str, timezone_name: str, *, workspace_slug=None) -> list[dt.datetime]`

`create`/`update` take a **plain `fields` dict** (already validated by the caller). `update`'s dict must already have None values dropped by the caller (REST's `exclude_none`); the service applies exactly the keys present.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schedule_services_crud.py`:

```python
from apps.harness.models import Turn


def _fields(**over):
    f = dict(
        name="Goal review", prompt="/eva:goal-review", cron="0 9 1 * *",
        timezone="America/New_York", enabled=True, routing="prefer_local",
        grace_minutes=120, notify=["inbox"],
    )
    f.update(over)
    return f


def test_create_and_list(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    assert s.name == "Goal review"
    assert [x.id for x in ss.list_schedules(owner, "eva")] == [s.id]


def test_create_duplicate_name_raises(owner, agent):
    ss.create_schedule(owner, "eva", _fields())
    with pytest.raises(ss.DuplicateScheduleName) as exc:
        ss.create_schedule(owner, "eva", _fields(prompt="different"))
    assert exc.value.name == "Goal review"


def test_update_applies_only_supplied_fields(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    out = ss.update_schedule(owner, "eva", s.id, {"enabled": False})
    assert out.enabled is False
    assert out.cron == "0 9 1 * *"  # untouched


def test_serialize_shape(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    d = ss.serialize_schedule(s)
    assert d["agent_slug"] == "eva"
    assert d["fire_after"] == s.created_at  # last_slot is None -> created_at
    assert len(d["next_runs"]) == 3
    assert d["last_status"] == ""


def test_delete_supersedes_open_turns_then_removes(owner, agent):
    """The wedge regression: an executing occurrence must be retired BEFORE the
    row is deleted, or it holds one_executing_turn_per_agent forever."""
    s = ss.create_schedule(owner, "eva", _fields())
    turn, _ = services.fire_schedule(s, dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC))
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)

    ss.delete_schedule(owner, "eva", s.id)

    turn.refresh_from_db()
    assert turn.status == Turn.MISSED  # retired, not stranded
    assert not AgentSchedule.objects.filter(pk=s.id).exists()
    # Proof it is unwedged: a new executing turn for the agent is insertable.
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="b1", status=Turn.RUNNING
    )


def test_run_now_enqueues_manual_turn(owner, agent):
    s = ss.create_schedule(owner, "eva", _fields())
    ss.run_schedule_now(owner, "eva", s.id)
    assert Turn.objects.filter(origin=Turn.ORIGIN_MANUAL).count() == 1


def test_preview_cron_returns_three(owner, agent):
    out = ss.preview_cron(owner, "eva", "0 9 * * 5", "America/New_York")
    assert len(out) == 3


def test_mcp_shape_no_workspace_pin_still_gated(agent):
    """workspace_slug=None (the MCP path) still requires membership."""
    outsider = User.objects.create_user("m", "m@evil.com", "pw")
    with pytest.raises(ss.ScheduleNotFound):
        ss.list_schedules(outsider, "eva")
```

Add `from apps.harness import services` and `import datetime as dt` if not already imported at the top of the test file (Task 1 imported `schedule_services as ss`; add these).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_services_crud.py -v`
Expected: FAIL — `AttributeError: module 'apps.harness.schedule_services' has no attribute 'serialize_schedule'`

- [ ] **Step 3: Write the functions**

Append to `apps/harness/schedule_services.py`:

```python
def serialize_schedule(schedule: AgentSchedule) -> dict:
    """The single serialized shape. REST builds ScheduleOut(**this); the MCP
    tools return it directly. fire_after = last_slot or created_at is the anchor
    the runner passes to due_slot — last_slot is NULL until the first fire, and
    an unbounded backward lookup would fire a slot predating the schedule."""
    latest = services.latest_occurrence_turn(schedule)
    return {
        "id": schedule.id,
        "agent_slug": schedule.agent_slug,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "routing": schedule.routing,
        "grace_minutes": schedule.grace_minutes,
        "notify": schedule.notify,
        "last_slot": schedule.last_slot,
        "fire_after": schedule.last_slot or schedule.created_at,
        "next_runs": next_slots(schedule.cron, schedule.timezone, now=timezone.now(), count=3),
        "last_status": latest.status if latest else "",
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def list_schedules(user, agent_slug: str, *, workspace_slug: str | None = None) -> list[AgentSchedule]:
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    return list(agent.schedules.all())


def create_schedule(
    user, agent_slug: str, fields: dict, *, workspace_slug: str | None = None
) -> AgentSchedule:
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    try:
        # Own savepoint: an IntegrityError from uniq_agent_schedule_name must not
        # poison the request transaction (SESSION_SAVE_EVERY_REQUEST would then
        # 400 instead of surfacing the 409). Mirrors apps/projects/api.py.
        with transaction.atomic():
            return AgentSchedule.objects.create(agent=agent, **fields)
    except IntegrityError:
        raise DuplicateScheduleName(fields["name"]) from None


def update_schedule(
    user, agent_slug: str, schedule_id: int, fields: dict, *, workspace_slug: str | None = None
) -> AgentSchedule:
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
    for key, value in fields.items():
        setattr(schedule, key, value)
    if fields:
        try:
            with transaction.atomic():  # savepoint — see create_schedule
                schedule.save()
        except IntegrityError:
            raise DuplicateScheduleName(schedule.name) from None
    return schedule


def delete_schedule(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> None:
    """Retire open occurrences FIRST — see the module docstring and the spec.
    There is no Turn->AgentSchedule FK, so nothing cascades; an executing
    occurrence would otherwise hold one_executing_turn_per_agent forever."""
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
    services.supersede_open_turns(schedule, reason="schedule deleted")
    schedule.delete()


def run_schedule_now(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
    services.run_schedule_now(schedule)
    return schedule


def preview_cron(
    user, agent_slug: str, cron: str, timezone_name: str, *, workspace_slug: str | None = None
) -> list[dt.datetime]:
    """agent_slug is for authorization only (you must see the agent to preview
    against it) — matches the REST preview route, which resolves + ignores it."""
    _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    return next_slots(cron, timezone_name, now=timezone.now(), count=3)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_services_crud.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/harness/schedule_services.py tests/test_schedule_services_crud.py
git commit -m "feat(harness): schedule CRUD service — create/update/delete/run-now/preview + serializer"
```

---

### Task 3: Re-point the REST routes at the service (behavior-preserving)

**Files:**
- Modify: `apps/harness/api_schedules.py`
- Test: `tests/test_schedule_api.py` (must pass UNCHANGED — do not edit it)

**Interfaces:**
- Consumes: everything from Tasks 1-2.
- Produces: no new interface — the routes keep their paths, schemas, and status codes.

The routes become thin: resolve via the service, map `ScheduleNotFound` → `HttpError(404)` and `DuplicateScheduleName` → the existing `_duplicate_name` 409, keep the Pydantic schemas + route-ordering + `openapi_extra`. The savepoint moves into the service, so the handlers no longer catch `IntegrityError`.

- [ ] **Step 1: Confirm the regression gate is currently green**

Run: `uv run pytest tests/test_schedule_api.py -v`
Expected: PASS (all) — this is the baseline the refactor must preserve.

- [ ] **Step 2: Rewrite the route handlers**

Replace the body of `apps/harness/api_schedules.py`'s handlers (keep the module docstring, imports of schemas, the `router`, and the route-ordering comment). The new handlers:

```python
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.pagination import Page, clamp_limit, paginate

from . import schedule_services as ss
from .schemas import (
    ScheduleIn,
    ScheduleOut,
    SchedulePatch,
    SchedulePreviewIn,
    SchedulePreviewOut,
)

router = Router(auth=session_auth, tags=["schedules"])


def _pin(request: HttpRequest) -> str | None:
    return getattr(request, "workspace_slug", None)


def _not_found(exc: ss.ScheduleNotFound) -> HttpError:
    return HttpError(404, "not found")


def _duplicate_name(name: str) -> ProblemError:
    """uniq_agent_schedule_name → 409, the repo's convention for a uniqueness
    violation (apps/projects/api.py, apps/workspaces/api.py)."""
    return ProblemError(
        409, "Schedule name already exists", type_=TYPE_CONFLICT,
        detail=f"A schedule named '{name}' already exists for this agent.",
    )


@router.get("/{slug}/schedules/", response=Page[ScheduleOut],
            summary="List an agent's recurring schedules",
            openapi_extra={"x-mcp-expose": True})
def list_schedules(request: HttpRequest, slug: str, limit: int = 100) -> Page[ScheduleOut]:
    try:
        schedules = ss.list_schedules(request.user, slug, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    items = [ScheduleOut(**ss.serialize_schedule(s)) for s in schedules]
    return paginate(items, offset=0, limit=clamp_limit(limit))


@router.post("/{slug}/schedules/", response={201: ScheduleOut},
             summary="Create a recurring schedule",
             openapi_extra={"x-mcp-expose": True})
def create_schedule(request: HttpRequest, slug: str, payload: ScheduleIn) -> Status:
    try:
        schedule = ss.create_schedule(request.user, slug, payload.dict(), workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    return Status(201, ScheduleOut(**ss.serialize_schedule(schedule)))


# Route-ordering invariant: this literal "preview" route must stay declared
# BEFORE the "/{slug}/schedules/{schedule_id}" routes. Ninja compiles path
# params with no int: converter, so "schedules/preview" resolves as
# {"schedule_id": "preview"} once a {schedule_id} pattern exists, and Django's
# resolution is method-agnostic. Moving this below PATCH/DELETE silently shadows it.
@router.post("/{slug}/schedules/preview", response=SchedulePreviewOut,
             summary="Preview the next fire times for a cron expression",
             openapi_extra={"x-mcp-expose": True})
def preview_schedule(request: HttpRequest, slug: str, payload: SchedulePreviewIn) -> SchedulePreviewOut:
    try:
        runs = ss.preview_cron(request.user, slug, payload.cron, payload.timezone, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return SchedulePreviewOut(next_runs=runs)


@router.patch("/{slug}/schedules/{schedule_id}", response=ScheduleOut,
              summary="Update a recurring schedule",
              openapi_extra={"x-mcp-expose": True})
def update_schedule(request: HttpRequest, slug: str, schedule_id: int, payload: SchedulePatch) -> ScheduleOut:
    # exclude_none as well as exclude_unset: SchedulePatch validators short-circuit
    # on None, so an explicit {"cron": null} slips past validation AND counts as
    # set; setattr(None) onto a non-nullable column would 500 where a 422 belongs.
    fields = payload.dict(exclude_unset=True, exclude_none=True)
    try:
        schedule = ss.update_schedule(request.user, slug, schedule_id, fields, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    return ScheduleOut(**ss.serialize_schedule(schedule))


@router.delete("/{slug}/schedules/{schedule_id}", response={204: None},
               summary="Delete a recurring schedule",
               openapi_extra={"x-mcp-expose": True})
def delete_schedule(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    try:
        ss.delete_schedule(request.user, slug, schedule_id, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return Status(204, None)


@router.post("/{slug}/schedules/{schedule_id}/run-now", response={202: ScheduleOut},
             summary="Trigger a schedule off-cycle, now",
             openapi_extra={"x-mcp-expose": True})
def run_now(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    try:
        schedule = ss.run_schedule_now(request.user, slug, schedule_id, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return Status(202, ScheduleOut(**ss.serialize_schedule(schedule)))
```

Delete the now-orphaned `_serialize` / `_schedule_or_404` helpers and the `_agent_or_404` import if nothing else in the module uses them (grep first).

- [ ] **Step 3: Run the regression gate — it must pass unchanged**

Run: `uv run pytest tests/test_schedule_api.py -v`
Expected: PASS (all, same count as Step 1). If any test fails, the extraction changed behavior — fix the service/route, NOT the test. Two tests are the sharp ones: `test_non_member_gets_404_not_403_on_every_route_and_nothing_leaks_or_writes` (proves the `ScheduleNotFound`→404 mapping holds on all six routes, POST bodies included, and nothing writes) and the duplicate-name tests asserting `"…" in resp.json()["detail"]` (prove `DuplicateScheduleName`→409 keeps its message via `_duplicate_name`). The 404 tests assert only `status_code`, so `_not_found`'s generic message is safe.

- [ ] **Step 4: Full suite + boundary + lint**

Run: `uv run pytest -q && uv run ruff check apps/harness && uv run pytest tests/test_architecture_boundary.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/harness/api_schedules.py
git commit -m "refactor(harness): REST schedule routes call the shared service"
```

---

### Task 4: The six MCP tools

**Files:**
- Create: `apps/mcp/tools/schedules.py`
- Modify: `apps/mcp/tools/__init__.py`
- Test: `apps/mcp/tests/test_schedule_tools.py`

**Interfaces:**
- Consumes: the service (Task 2); `apps.mcp.server.mcp`; `apps.mcp.audit.{current_user_id, write_audit}`; `apps.mcp.rate_limit.check_write_limit`; `canopy_cron.{validate_cron, validate_timezone}`.
- Produces: 6 registered tools: `list_schedules`, `preview_cron`, `create_schedule`, `update_schedule`, `delete_schedule`, `run_schedule_now`.

Each mirrors `apps/mcp/tools/insights.py`: async, resolve `current_user_id()`, (writes) `check_write_limit(user_id)`, run via `sync_to_async(fn, thread_sensitive=True)`, `write_audit(...)` on **both** success and exception, then `raise` on exception. Tools resolve the user themselves (no `workspace_slug` — that's the tenant-URL concept the MCP lacks).

- [ ] **Step 1: Write the failing test**

Create `apps/mcp/tests/test_schedule_tools.py`, mirroring `apps/mcp/tests/test_tools.py`'s `as_user` pattern:

```python
"""Schedule MCP tools — run as the authenticated user, audit, rate-limit."""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(
        token="t", client_id=str(user.pk), scopes=["canopy:user"],
        claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


@pytest.fixture()
def member():
    u = User.objects.create_user(username="jj", email="jj@dimagi.com")
    w = Workspace.objects.create(slug="dimagi", display_name="D", created_by=u, auto_join_domains=[])
    wsvc.ensure_member(w, u, WorkspaceMembership.OWNER)
    Agent.objects.create(slug="eva", name="Eva", workspace=w)
    return u


def _call(name, args):
    return async_to_sync(mcp.call_tool)(name, args)


def test_tools_are_registered():
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"list_schedules", "create_schedule", "update_schedule",
            "delete_schedule", "run_schedule_now", "preview_cron"} <= names


def test_create_then_list(member):
    with as_user(member):
        _call("create_schedule", {
            "agent_slug": "eva", "name": "Goal review", "prompt": "/eva:goal-review",
            "cron": "0 9 1 * *", "timezone": "America/New_York",
        })
        result = _call("list_schedules", {"agent_slug": "eva"})
    rows = result.structured_content["result"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Goal review"


def test_create_audits_success(member):
    with as_user(member):
        _call("create_schedule", {
            "agent_slug": "eva", "name": "R", "prompt": "p", "cron": "0 9 * * 5",
        })
    row = MCPAuditLog.objects.filter(tool="create_schedule").latest("id")
    assert row.ok is True


def test_run_now_audit_carries_schedule_name(member):
    with as_user(member):
        _call("create_schedule", {"agent_slug": "eva", "name": "Weekly", "prompt": "p", "cron": "0 9 * * 5"})
        sid = AgentSchedule.objects.get().id
        _call("run_schedule_now", {"agent_slug": "eva", "schedule_id": sid})
    row = MCPAuditLog.objects.filter(tool="run_schedule_now").latest("id")
    assert "Weekly" in row.args_summary
    assert Turn.objects.filter(origin=Turn.ORIGIN_MANUAL).count() == 1


def test_non_member_gets_error_not_leak(member):
    outsider = User.objects.create_user(username="m", email="m@evil.com")
    with as_user(outsider):
        with pytest.raises(Exception):  # ScheduleNotFound surfaces as a tool error
            _call("list_schedules", {"agent_slug": "eva"})


def test_delete_supersedes_then_removes(member):
    with as_user(member):
        _call("create_schedule", {"agent_slug": "eva", "name": "D", "prompt": "p", "cron": "0 9 * * 5"})
        sid = AgentSchedule.objects.get().id
        _call("delete_schedule", {"agent_slug": "eva", "schedule_id": sid})
    assert not AgentSchedule.objects.filter(pk=sid).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest apps/mcp/tests/test_schedule_tools.py -v`
Expected: FAIL — `test_tools_are_registered` fails (tools not registered).

- [ ] **Step 3: Write the tools**

Create `apps/mcp/tools/schedules.py`:

```python
"""Schedule MCP tools — run as the authenticated user.

Six tools (list/create/update/delete/run-now/preview) over AgentSchedule. Each
calls apps.harness.schedule_services — the SAME layer the REST routes call, so
the two surfaces can't drift. Each tool: resolves the user, (writes) enforces
the per-user rate limit, runs the ORM work via sync_to_async, and audits both
the success and error paths.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from canopy_cron import validate_cron, validate_timezone
from django.contrib.auth import get_user_model

from apps.harness import schedule_services as ss
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import check_write_limit
from apps.mcp.server import mcp

User = get_user_model()


def _user(user_id: int):
    return User.objects.get(pk=user_id)


@mcp.tool
async def list_schedules(agent_slug: str, limit: int = 100) -> list[dict]:
    """List an agent's recurring schedules (cron config + next fire times)."""
    user_id = current_user_id()
    try:
        rows = await sync_to_async(_list_sync, thread_sensitive=True)(user_id, agent_slug, limit)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="list_schedules",
                          args_summary=f"agent={agent_slug}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="list_schedules",
                      args_summary=f"agent={agent_slug} -> {len(rows)} rows", ok=True)
    return rows


def _list_sync(user_id, agent_slug, limit):
    user = _user(user_id)
    scheds = ss.list_schedules(user, agent_slug)[:limit]
    return [ss.serialize_schedule(s) for s in scheds]


@mcp.tool
async def preview_cron(agent_slug: str, cron: str, timezone: str = "UTC") -> list[str]:
    """Preview the next 3 fire times for a cron+timezone, as ISO-8601 strings.
    Computed with the same slot math the runner fires on — never re-implement cron."""
    user_id = current_user_id()
    try:
        runs = await sync_to_async(_preview_sync, thread_sensitive=True)(user_id, agent_slug, cron, timezone)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="preview_cron",
                          args_summary=f"agent={agent_slug} cron={cron!r}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="preview_cron",
                      args_summary=f"agent={agent_slug} cron={cron!r}", ok=True)
    return runs


def _preview_sync(user_id, agent_slug, cron, timezone):
    validate_cron(cron)
    validate_timezone(timezone)
    user = _user(user_id)
    return [d.isoformat() for d in ss.preview_cron(user, agent_slug, cron, timezone)]


@mcp.tool
async def create_schedule(
    agent_slug: str, name: str, prompt: str, cron: str, timezone: str = "UTC",
    enabled: bool = True, routing: str = "prefer_local", grace_minutes: int = 120,
    notify: list[str] | None = None,
) -> dict:
    """Create a recurring turn for an agent. `cron` is a 5-field expression;
    `timezone` an IANA name. `prompt` is what the turn is seeded with."""
    user_id = current_user_id()
    check_write_limit(user_id)
    try:
        row = await sync_to_async(_create_sync, thread_sensitive=True)(
            user_id, agent_slug, name, prompt, cron, timezone, enabled, routing,
            grace_minutes, notify or ["inbox"],
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="create_schedule",
                          args_summary=f"agent={agent_slug} name={name!r}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="create_schedule",
                      args_summary=f"agent={agent_slug} name={name!r}", ok=True)
    return row


def _create_sync(user_id, agent_slug, name, prompt, cron, timezone, enabled, routing, grace_minutes, notify):
    validate_cron(cron)
    validate_timezone(timezone)
    user = _user(user_id)
    fields = dict(name=name, prompt=prompt, cron=cron, timezone=timezone, enabled=enabled,
                  routing=routing, grace_minutes=grace_minutes, notify=notify)
    return ss.serialize_schedule(ss.create_schedule(user, agent_slug, fields))


@mcp.tool
async def update_schedule(
    agent_slug: str, schedule_id: int, name: str | None = None, prompt: str | None = None,
    cron: str | None = None, timezone: str | None = None, enabled: bool | None = None,
    routing: str | None = None, grace_minutes: int | None = None, notify: list[str] | None = None,
) -> dict:
    """Update a schedule. Only the fields you pass are changed."""
    user_id = current_user_id()
    check_write_limit(user_id)
    raw = dict(name=name, prompt=prompt, cron=cron, timezone=timezone, enabled=enabled,
               routing=routing, grace_minutes=grace_minutes, notify=notify)
    fields = {k: v for k, v in raw.items() if v is not None}
    try:
        row = await sync_to_async(_update_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id, fields)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="update_schedule",
                          args_summary=f"agent={agent_slug} id={schedule_id}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="update_schedule",
                      args_summary=f"agent={agent_slug} id={schedule_id} fields={sorted(fields)}", ok=True)
    return row


def _update_sync(user_id, agent_slug, schedule_id, fields):
    if "cron" in fields:
        validate_cron(fields["cron"])
    if "timezone" in fields:
        validate_timezone(fields["timezone"])
    user = _user(user_id)
    return ss.serialize_schedule(ss.update_schedule(user, agent_slug, schedule_id, fields))


@mcp.tool
async def delete_schedule(agent_slug: str, schedule_id: int) -> dict:
    """Delete a schedule. Any open occurrences it fired are retired first."""
    user_id = current_user_id()
    check_write_limit(user_id)
    try:
        await sync_to_async(_delete_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="delete_schedule",
                          args_summary=f"agent={agent_slug} id={schedule_id}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="delete_schedule",
                      args_summary=f"agent={agent_slug} id={schedule_id}", ok=True)
    return {"deleted": schedule_id}


def _delete_sync(user_id, agent_slug, schedule_id):
    ss.delete_schedule(_user(user_id), agent_slug, schedule_id)


@mcp.tool
async def run_schedule_now(agent_slug: str, schedule_id: int) -> dict:
    """Trigger a schedule off-cycle NOW. Spawns a real agent turn (tokens)."""
    user_id = current_user_id()
    check_write_limit(user_id)
    try:
        row, name = await sync_to_async(_run_now_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="run_schedule_now",
                          args_summary=f"agent={agent_slug} id={schedule_id}", ok=False, error=str(exc))
        raise
    # The name is in the summary on purpose: run_now is the one tool that burns
    # tokens, so a runaway must be visible in MCPAuditLog rather than inferred.
    await write_audit(user_id=user_id, tool="run_schedule_now",
                      args_summary=f"agent={agent_slug} id={schedule_id} name={name!r}", ok=True)
    return row


def _run_now_sync(user_id, agent_slug, schedule_id):
    sched = ss.run_schedule_now(_user(user_id), agent_slug, schedule_id)
    return ss.serialize_schedule(sched), sched.name
```

- [ ] **Step 4: Register the module**

In `apps/mcp/tools/__init__.py`, add beside the insights import:

```python
from . import schedules  # noqa: F401
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest apps/mcp/tests/test_schedule_tools.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check apps/mcp apps/harness`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/mcp/tools/schedules.py apps/mcp/tools/__init__.py apps/mcp/tests/test_schedule_tools.py
git commit -m "feat(mcp): schedule CRUD tools — list/create/update/delete/run-now/preview"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/architecture/mcp-surface.md`
- Modify: `CLAUDE.md`

**Interfaces:** none.

- [ ] **Step 1: Update the MCP surface doc**

In `docs/architecture/mcp-surface.md`, find the tool inventory (lists `list_insights` + `clear_insights`) and add the six schedule tools with a one-line description each, plus a sentence noting they call `apps/harness/schedule_services.py` — the same layer the REST routes call.

- [ ] **Step 2: Update CLAUDE.md**

In `CLAUDE.md`, find the MCP section (`### MCP (apps/mcp...)`, "Tools today: `list_insights` (read) + `clear_insights` (write)") and update it to include the schedule tools, e.g.:

```markdown
Tools today: `list_insights` + `clear_insights` (insights), and `list_schedules` / `preview_cron` (read) + `create_schedule` / `update_schedule` / `delete_schedule` / `run_schedule_now` (write) for recurring turns. The schedule tools call `apps/harness/schedule_services.py`, the same request-free service layer the REST routes call, so the MCP and REST surfaces can't drift.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/mcp-surface.md CLAUDE.md
git commit -m "docs: MCP schedule tools in the surface doc + CLAUDE.md"
```

---

## Final Verification

- [ ] **Full backend suite**

Run: `uv run pytest`
Expected: PASS — including `tests/test_schedule_api.py` (unchanged), `tests/test_architecture_boundary.py`, the new service + tool suites.

- [ ] **Regression gate held**

Run: `uv run pytest tests/test_schedule_api.py -q`
Expected: PASS, same count as before the refactor. This proves the extraction was behavior-preserving.

- [ ] **Lint + migrations**

Run: `uv run ruff check apps/ && uv run python manage.py makemigrations --check --dry-run`
Expected: no new lint errors; `No changes detected` (this change has no model changes).

- [ ] **Tools are live end to end**

Run:
```bash
uv run python -c "
import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.test'); django.setup()
from asgiref.sync import async_to_sync
from apps.mcp.server import mcp
names = {t.name for t in async_to_sync(mcp.list_tools)()}
print('schedule tools present:', {'list_schedules','create_schedule','update_schedule','delete_schedule','run_schedule_now','preview_cron'} <= names)
"
```
Expected: `schedule tools present: True`

## Deferred (spec's "Open for iteration")

- A per-tool rate limit on `run_schedule_now` (audit visibility first; add only if the log shows looping).
- Trimming the tool return shape if `next_runs`/`last_status` prove noisy.
