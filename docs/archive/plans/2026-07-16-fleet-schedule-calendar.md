# Fleet Schedule Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly calendar of every recurring schedule across the fleet, at two scopes — a personal `/schedules` roll-up spanning every workspace you belong to, and a per-workspace `/w/:workspace/schedules` — with client-side filters by workspace and agent, and inline edit via the existing `ScheduleEditor`.

**Architecture:** One `<ScheduleCalendar>` component mounted at two routes, backed by ONE endpoint (`GET /api/agents/schedules/week`) whose scope is decided by whether the URL is tenant-pinned. The frontend always calls the flat path; the client's request middleware auto-rewrites it to `/api/w/{ws}/…` when the browser is under a `/w/:ws/` route (`/api/agents` is already in `WS_SCOPED_API_PREFIXES`). So the personal view genuinely IS the workspace view with the pin removed. A new `canopy_cron.slots_between` computes the week's fires; the client renders them in the viewer's local timezone.

**Tech Stack:** Django 5, Django Ninja 1.6, Pydantic v2, `canopy_cron` (croniter), React 19 + Vite, `openapi-fetch`, vitest.

**Spec:** `docs/superpowers/specs/2026-07-16-fleet-schedule-calendar-design.md` — read it before Task 1.

## Global Constraints

- **Framework tier.** `apps/harness` must never import product apps (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`). `apps.workspaces`/`apps.agents` are framework — importing `apps.workspaces.services` is fine, but a harness **api** module must NOT import the agents **api** module (the rule that forced `_agent_or_404` to be duplicated). Compute the visible-workspace set from `apps.workspaces.services` primitives, not by importing `_visible_agent_workspace_ids`.
- **The service stays request-free.** `apps/harness/schedule_services.py` takes a resolved `workspace_ids` set (the route computes it), imports no Ninja, raises only its domain exceptions.
- **Fire times cross the wire as tz-aware UTC.** The client renders them in the viewer's local tz. `slots_between` returns UTC instants.
- **`slots_between` is `[start, end)` — half-open.** Inclusive start, exclusive end, so adjacent weeks tile without double-counting a boundary fire.
- **Reuse `ScheduleEditor`, don't fork it.** `frontend/src/components/agents/ScheduleEditor.tsx` is already `{agentSlug, schedule, onClose, onSaved}`. The calendar mounts the same component; no extraction, no change to the per-agent page.
- **Filters are client-side** over the fetched week (small bounded set). No backend filter params.
- **Design tokens only** in frontend — no raw palette literals (`stone-*`, `orange-*`, `zinc-*`, `slate-*`, `red-*`, `amber-*`, `emerald-*`, `sky-*`, `violet-*`, bare `black`/`white`); no `dark:` variants. Components are typecheck-only; only pure functions get vitest (`frontend/src/api/base.test.ts` is the convention).
- **CI runs `ruff check . --select F`** (pyflakes) — no unused imports / undefined names. Backend tests: `uv run pytest`. `canopy_cron`'s own suite runs from its dir: `cd packages/canopy_cron && uv run --with pytest pytest`.

---

### Task 1: `canopy_cron.slots_between` — window fire calculation

**Files:**
- Modify: `packages/canopy_cron/canopy_cron/slots.py`
- Test: `packages/canopy_cron/tests/test_slots.py`

**Interfaces:**
- Consumes: existing `validate_cron`, `validate_timezone` in the same module.
- Produces: `slots_between(cron: str, tz: str, *, start: datetime, end: datetime) -> list[datetime]` — every fire in `[start, end)`, ordered, tz-aware UTC.

`next_slots` (same file) is the shape to mirror: `croniter(validate_cron(cron), <seed>.astimezone(zone))` then `get_next(dt.datetime).astimezone(dt.UTC)`.

**Boundary subtlety — verify against the test, don't assume:** `croniter(expr, seed).get_next()` returns the first fire STRICTLY AFTER `seed`. To make `start` *inclusive*, seed one microsecond before it (cron granularity is minutes, so this can never catch a different fire). If the boundary test fails, the seed idiom is what to fix — not the test.

- [ ] **Step 1: Write the failing tests**

Append to `packages/canopy_cron/tests/test_slots.py` (check the existing imports at the top — `datetime as dt`, `slots_between` needs adding to the `from canopy_cron import …` line):

```python
def test_slots_between_daily_yields_seven_in_a_week():
    # A daily 9am UTC cron over a 7-day UTC window → exactly 7 fires.
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)   # Mon 00:00
    end = start + dt.timedelta(days=7)
    fires = slots_between("0 9 * * *", "UTC", start=start, end=end)
    assert len(fires) == 7
    assert fires[0] == dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)
    assert fires[-1] == dt.datetime(2026, 7, 19, 9, 0, tzinfo=dt.UTC)


def test_slots_between_empty_window():
    # A weekly Friday cron over a Mon–Thu window → no fires.
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)   # Mon
    end = dt.datetime(2026, 7, 17, 0, 0, tzinfo=dt.UTC)     # Fri 00:00 (before 9am Fri)
    assert slots_between("0 9 * * 5", "UTC", start=start, end=end) == []


def test_slots_between_is_half_open_inclusive_start_exclusive_end():
    # A fire EXACTLY at start is included; a fire EXACTLY at end is not.
    start = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)   # a fire lands here
    end = dt.datetime(2026, 7, 14, 9, 0, tzinfo=dt.UTC)     # and here
    fires = slots_between("0 9 * * *", "UTC", start=start, end=end)
    assert fires == [dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)]  # start in, end out


def test_slots_between_dst_holds_local_9am_across_the_shift():
    # US Eastern falls back 2026-11-01. A daily 9am-ET cron stays 9am LOCAL:
    # 13:00Z before the shift (EDT, UTC-4), 14:00Z after (EST, UTC-5).
    start = dt.datetime(2026, 10, 30, 0, 0, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=7)
    fires = slots_between("0 9 * * *", "America/New_York", start=start, end=end)
    # Convert each to the ET wall clock and assert it reads 09:00 every day.
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    assert all(f.astimezone(et).hour == 9 and f.astimezone(et).minute == 0 for f in fires)
    # And the offset genuinely changed mid-window (proves it isn't fixed-offset).
    assert dt.datetime(2026, 10, 30, 13, 0, tzinfo=dt.UTC) in fires   # EDT
    assert dt.datetime(2026, 11, 6, 14, 0, tzinfo=dt.UTC) in fires    # EST
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/canopy_cron && uv run --with pytest pytest tests/test_slots.py -k slots_between -v`
Expected: FAIL — `ImportError: cannot import name 'slots_between'`

- [ ] **Step 3: Implement**

In `packages/canopy_cron/canopy_cron/slots.py`, add after `next_slots`:

```python
def slots_between(
    cron: str, tz: str, *, start: dt.datetime, end: dt.datetime
) -> list[dt.datetime]:
    """Every fire time in the half-open window [start, end) — inclusive start,
    exclusive end, so adjacent weeks tile without double-counting a boundary
    fire. Returns ordered tz-aware UTC instants; the cron is evaluated in `tz`.

    Seeded one microsecond before `start` because croniter.get_next() is
    strictly-after its seed — this makes a fire landing exactly on `start`
    inclusive. Cron granularity is minutes, so the microsecond can never catch a
    different fire.
    """
    zone = ZoneInfo(validate_timezone(tz))
    seed = (start - dt.timedelta(microseconds=1)).astimezone(zone)
    itr = croniter(validate_cron(cron), seed)
    out: list[dt.datetime] = []
    while True:
        nxt = itr.get_next(dt.datetime).astimezone(dt.UTC)
        if nxt >= end:
            break
        out.append(nxt)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/canopy_cron && uv run --with pytest pytest tests/test_slots.py -v`
Expected: PASS (all, including the pre-existing next_slots/due_slot tests)

- [ ] **Step 5: Commit**

```bash
git add packages/canopy_cron/canopy_cron/slots.py packages/canopy_cron/tests/test_slots.py
git commit -m "feat(canopy_cron): slots_between — every fire in a half-open window"
```

---

### Task 2: `week_schedules` service aggregator + schemas

**Files:**
- Modify: `apps/harness/schedule_services.py`
- Modify: `apps/harness/schemas.py`
- Test: `tests/test_schedule_services_crud.py`

**Interfaces:**
- Consumes: `canopy_cron.slots_between` (Task 1); existing `serialize_schedule` in the same module; `AgentSchedule`.
- Produces:
  - `week_schedules(workspace_ids: set[str | None], start: datetime) -> list[dict]` — one dict per enabled schedule in those workspaces: `{"schedule": <serialize_schedule dict>, "workspace_slug": <str|None>, "fires": [<datetime>...]}`.
  - Schemas `ScheduledFireOut`, `ScheduleWeekOut` (Task 3 uses them).

**`__in` + None subtlety:** `agent__workspace_id__in={None, "x"}` drops the None in SQL (NULL isn't matched by IN). Unhomed agents (`workspace_id IS NULL`) must be caught with an explicit `Q(...isnull=True)` when `None` is in the set — see the code.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schedule_services_crud.py` (it already imports `schedule_services as ss`, `AgentSchedule`, `datetime as dt`, and has `owner`/`ws`/`agent` fixtures — reuse them):

```python
def test_week_schedules_gathers_enabled_with_fires(owner, agent, ws):
    ss.create_schedule(owner, "eva", _fields(name="Daily", cron="0 9 * * *", timezone="UTC"))
    ss.create_schedule(owner, "eva", _fields(name="Paused", cron="0 9 * * *", timezone="UTC", enabled=False))
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)

    rows = ss.week_schedules({ws.slug}, start)

    assert len(rows) == 1  # the disabled one is excluded
    row = rows[0]
    assert row["schedule"]["name"] == "Daily"
    assert row["workspace_slug"] == ws.slug
    assert len(row["fires"]) == 7  # daily over the week


def test_week_schedules_scoped_to_given_workspaces(owner, agent, ws):
    # A schedule in a workspace NOT in the set must not appear.
    ss.create_schedule(owner, "eva", _fields(cron="0 9 * * *"))
    rows = ss.week_schedules({"some-other-ws"}, dt.datetime(2026, 7, 13, tzinfo=dt.UTC))
    assert rows == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_services_crud.py -k week_schedules -v`
Expected: FAIL — `AttributeError: module 'apps.harness.schedule_services' has no attribute 'week_schedules'`

- [ ] **Step 3: Implement the service function**

Append to `apps/harness/schedule_services.py` (add `from django.db.models import Q` and `from canopy_cron import slots_between` to the imports if not present — grep first):

```python
def week_schedules(workspace_ids: set, start: dt.datetime) -> list[dict]:
    """Every ENABLED schedule in `workspace_ids`, each with its fires in the
    week [start, start+7d). `workspace_ids` is the caller's already-resolved
    visible-workspace set (the route computes it from apps.workspaces.services),
    so this stays request-free. A None in the set means 'legacy unhomed agents'
    — matched explicitly, since SQL IN never matches NULL."""
    end = start + dt.timedelta(days=7)
    non_null = {w for w in workspace_ids if w is not None}
    q = Q(agent__workspace_id__in=non_null)
    if None in workspace_ids:
        q |= Q(agent__workspace_id__isnull=True)
    schedules = (
        AgentSchedule.objects.filter(enabled=True).filter(q).select_related("agent")
    )
    rows = []
    for s in schedules:
        rows.append({
            "schedule": serialize_schedule(s),
            "workspace_slug": s.agent.workspace_id,
            "fires": slots_between(s.cron, s.timezone, start=start, end=end),
        })
    return rows
```

- [ ] **Step 4: Add the schemas**

Append to `apps/harness/schemas.py` (it already imports `datetime as dt`; `ScheduleOut` is defined there):

```python
class ScheduledFireOut(Schema):
    schedule: ScheduleOut
    workspace_slug: str | None = None
    fires: list[dt.datetime]


class ScheduleWeekOut(Schema):
    start: dt.datetime
    items: list[ScheduledFireOut]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_services_crud.py -v`
Expected: PASS (all — the two new + the existing CRUD tests)

- [ ] **Step 6: Commit**

```bash
git add apps/harness/schedule_services.py apps/harness/schemas.py tests/test_schedule_services_crud.py
git commit -m "feat(harness): week_schedules aggregator + week schemas"
```

---

### Task 3: The `/schedules/week` endpoint — personal + tenant scope

**Files:**
- Modify: `apps/harness/api_schedules.py`
- Test: `tests/test_schedule_week.py` (new)

**Interfaces:**
- Consumes: `week_schedules` + `ScheduleWeekOut`/`ScheduledFireOut` (Task 2); `apps.workspaces.services`.
- Produces: `GET /api/agents/schedules/week?start=<iso>` → `ScheduleWeekOut`; helper `_visible_workspace_ids(request) -> set`.

**Route ordering:** declare `/schedules/week` BEFORE the `/{slug}/schedules/…` routes, same defensive ordering `preview` uses — so `schedules` can't be captured as a `{slug}`.

**Scope by URL, not param:** `_visible_workspace_ids` builds the same set `_visible_agent_workspace_ids` does, from `wsvc` primitives (NOT by importing the agents-api helper — harness api can't depend on agents api). Pinned (`request.workspace_slug` truthy) → `{ws}`; unpinned → every membership + `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_week.py`:

```python
"""GET /api/agents/schedules/week — personal roll-up (flat) + tenant scope (pinned)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness import schedule_services as ss
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

START = "2026-07-13T00:00:00Z"


def _ws(slug, owner):
    w = Workspace.objects.create(slug=slug, display_name=slug, created_by=owner, auto_join_domains=[])
    wsvc.ensure_member(w, owner, WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def setup():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    a = _ws("alpha", owner)
    b = _ws("beta", owner)
    # A third workspace the owner is NOT a member of.
    stranger = User.objects.create_user("s", "s@x.com", "pw")
    c = Workspace.objects.create(slug="gamma", display_name="g", created_by=stranger, auto_join_domains=[])
    ea = Agent.objects.create(slug="eva", name="Eva", workspace=a)
    hb = Agent.objects.create(slug="hal", name="Hal", workspace=b)
    gc = Agent.objects.create(slug="ghost", name="Ghost", workspace=c)
    ss.create_schedule(owner, "eva", dict(name="A", prompt="p", cron="0 9 * * *", timezone="UTC",
                                          enabled=True, routing="prefer_local", grace_minutes=120, notify=["inbox"]))
    ss.create_schedule(owner, "hal", dict(name="B", prompt="p", cron="0 9 * * *", timezone="UTC",
                                          enabled=True, routing="prefer_local", grace_minutes=120, notify=["inbox"]))
    # ghost's schedule is created directly (owner can't via service — not a member)
    from apps.harness.models import AgentSchedule
    AgentSchedule.objects.create(agent=gc, name="C", prompt="p", cron="0 9 * * *", timezone="UTC")
    c_ = Client(); c_.force_login(owner)
    return c_


def test_personal_flat_spans_all_my_workspaces(setup):
    resp = setup.get(f"/api/agents/schedules/week?start={START}")
    assert resp.status_code == 200, resp.content
    names = {i["schedule"]["name"] for i in resp.json()["items"]}
    assert names == {"A", "B"}  # alpha + beta; NOT ghost's C (gamma, not a member)


def test_tenant_pinned_returns_one_workspace(setup):
    resp = setup.get(f"/api/w/alpha/agents/schedules/week?start={START}")
    assert resp.status_code == 200, resp.content
    names = {i["schedule"]["name"] for i in resp.json()["items"]}
    assert names == {"A"}  # alpha only


def test_fires_present_in_the_week(setup):
    resp = setup.get(f"/api/agents/schedules/week?start={START}")
    item = next(i for i in resp.json()["items"] if i["schedule"]["name"] == "A")
    assert len(item["fires"]) == 7  # daily
    assert item["workspace_slug"] == "alpha"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_week.py -v`
Expected: FAIL — 404 (route not declared).

- [ ] **Step 3: Add the helper + route**

In `apps/harness/api_schedules.py`: import `datetime` and the new schemas + service member, then add the helper and route **above** the `@router.get("/{slug}/schedules/", …)` route. Add to imports:

```python
import datetime as dt

from apps.workspaces import services as wsvc

from .schemas import ScheduledFireOut, ScheduleWeekOut  # add to the existing .schemas import
```

Then, above the first `/{slug}/` route:

```python
def _visible_workspace_ids(request: HttpRequest) -> set:
    """The workspaces whose agents this caller may see — pinned to one (tenant
    URL) or spanning every membership (flat/personal). Built from wsvc
    primitives, NOT by importing agents.api._visible_agent_workspace_ids: a
    harness api module must not depend on the agents api module (the same rule
    that duplicated _agent_or_404). None = legacy unhomed agents."""
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws:
        return {ws}
    return set(wsvc.user_workspace_slugs(request.user)) | {None}


@router.get("/schedules/week", response=ScheduleWeekOut,
            summary="A week of scheduled fires across the visible fleet",
            openapi_extra={"x-mcp-expose": True})
def schedule_week(request: HttpRequest, start: dt.datetime) -> ScheduleWeekOut:
    """Every enabled schedule the caller can see, each with its fires in
    [start, start+7d). Scope is the URL: flat → all my workspaces; /w/{ws}/ →
    that one (WorkspaceResolveMiddleware sets request.workspace_slug). Declared
    before /{slug}/schedules so 'schedules' isn't captured as a slug."""
    rows = ss.week_schedules(_visible_workspace_ids(request), start)
    return ScheduleWeekOut(start=start, items=[ScheduledFireOut(**r) for r in rows])
```

(`ss` is the module alias already imported in this file — confirm it is; the CRUD routes use `ss.list_schedules` etc.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_week.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Full suite + boundary**

Run: `uv run pytest -q && uv run pytest tests/test_architecture_boundary.py -q && uv run ruff check apps/harness --select F`
Expected: all PASS; no pyflakes errors.

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api_schedules.py tests/test_schedule_week.py
git commit -m "feat(harness): GET /schedules/week — personal roll-up + tenant scope"
```

---

### Task 4: Regenerate types + client fn + pure grid helpers

**Files:**
- Modify: `frontend/src/api/generated.ts` (generated — never hand-edit)
- Modify: `frontend/src/api/schedules.ts`
- Create: `frontend/src/components/schedules/calendarGrid.ts`
- Create: `frontend/src/components/schedules/calendarGrid.test.ts`

**Interfaces:**
- Consumes: `generated.ts` types (post-regen); `apiV2`.
- Produces:
  - `getScheduleWeek(start: string): Promise<ScheduleWeekItem[]>` and types `ScheduleWeekItem` = `components["schemas"]["ScheduledFireOut"]`.
  - `bucketByDay(items, weekStart): DayColumn[]` and `matchesFilters(item, {agent, workspace})` — pure, vitest-tested.

**No workspace param on the client fn** — the request middleware auto-rewrites the flat path to `/api/w/{ws}/…` when the browser is under a `/w/:ws/` route (`/api/agents` is in `WS_SCOPED_API_PREFIXES`). So the same call serves both routes.

- [ ] **Step 1: Regenerate the types**

Run: `cd frontend && npm run gen:api:local 2>/dev/null || npm run gen:api`
(the `:local` variant dumps the schema in-process — no server/DB needed; check `frontend/package.json`.)
Then verify: `grep -c "schedules/week" frontend/src/api/generated.ts` → non-zero, and `grep -c "ScheduledFireOut" frontend/src/api/generated.ts` → non-zero.

- [ ] **Step 2: Add the client function**

Append to `frontend/src/api/schedules.ts` (match the file's existing `apiV2.GET` + `problemMessage` idiom — read `listSchedules` at the top):

```typescript
export type ScheduleWeekItem = components["schemas"]["ScheduledFireOut"];

/** A week of scheduled fires across every schedule the caller can see. Scope is
 * the CURRENT ROUTE: on /schedules (root) this hits the flat path and spans all
 * your workspaces; under /w/:ws/… the client middleware rewrites it to the
 * tenant path and the server pins that workspace. No ws param needed. */
export async function getScheduleWeek(start: string): Promise<ScheduleWeekItem[]> {
  const { data, error } = await apiV2.GET("/api/agents/schedules/week", {
    params: { query: { start } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load the schedule"));
  return data.items as ScheduleWeekItem[];
}
```

- [ ] **Step 3: Write the failing test for the pure helpers**

Create `frontend/src/components/schedules/calendarGrid.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { bucketByDay, matchesFilters } from "./calendarGrid";
import type { ScheduleWeekItem } from "@/api/schedules";

function item(name: string, agent: string, ws: string, fires: string[]): ScheduleWeekItem {
  return {
    // only the fields the helpers read need to be real
    schedule: { name, agent_slug: agent } as ScheduleWeekItem["schedule"],
    workspace_slug: ws,
    fires,
  } as ScheduleWeekItem;
}

describe("bucketByDay", () => {
  it("drops each fire into its local-day column (Mon-indexed)", () => {
    // Week of Mon 2026-07-13. Two fires: Mon 09:00Z and Wed 09:00Z.
    const weekStart = new Date("2026-07-13T00:00:00Z");
    const items = [item("Daily", "eva", "alpha", ["2026-07-13T09:00:00Z", "2026-07-15T09:00:00Z"])];
    const cols = bucketByDay(items, weekStart);
    expect(cols).toHaveLength(7);
    expect(cols[0].fires).toHaveLength(1); // Monday
    expect(cols[2].fires).toHaveLength(1); // Wednesday
    expect(cols[1].fires).toHaveLength(0); // Tuesday
    // each bucketed fire carries its source item so a chip can render agent + open the editor
    expect(cols[0].fires[0].item.schedule.name).toBe("Daily");
  });
});

describe("matchesFilters", () => {
  const it0 = item("A", "eva", "alpha", []);
  it("passes when no filter is set", () => {
    expect(matchesFilters(it0, { agent: null, workspace: null })).toBe(true);
  });
  it("filters by agent", () => {
    expect(matchesFilters(it0, { agent: "eva", workspace: null })).toBe(true);
    expect(matchesFilters(it0, { agent: "hal", workspace: null })).toBe(false);
  });
  it("filters by workspace", () => {
    expect(matchesFilters(it0, { agent: null, workspace: "alpha" })).toBe(true);
    expect(matchesFilters(it0, { agent: null, workspace: "beta" })).toBe(false);
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/schedules/calendarGrid.test.ts`
Expected: FAIL — cannot resolve `./calendarGrid`.

- [ ] **Step 5: Implement the helpers**

Create `frontend/src/components/schedules/calendarGrid.ts`:

```typescript
import type { ScheduleWeekItem } from "@/api/schedules";

export interface BucketedFire {
  /** The fire instant, as a Date (rendered in the viewer's local tz). */
  when: Date;
  item: ScheduleWeekItem;
}

export interface DayColumn {
  /** 0 = Monday … 6 = Sunday. */
  index: number;
  /** Local midnight of this day. */
  date: Date;
  /** Fires that land on this day, ascending by time. */
  fires: BucketedFire[];
}

/** Drop every fire of every item into its local-day column. `weekStart` is the
 * Monday the grid is showing; columns are Mon..Sun in the VIEWER's local tz
 * (the fires cross the wire as UTC and are compared in local time here). */
export function bucketByDay(items: ScheduleWeekItem[], weekStart: Date): DayColumn[] {
  const cols: DayColumn[] = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + i);
    d.setHours(0, 0, 0, 0);
    return { index: i, date: d, fires: [] };
  });
  const weekStartMidnight = new Date(cols[0].date);
  for (const item of items) {
    for (const iso of item.fires) {
      const when = new Date(iso);
      // days since the week's Monday, in local time
      const dayIdx = Math.floor((startOfLocalDay(when).getTime() - weekStartMidnight.getTime()) / 86_400_000);
      if (dayIdx >= 0 && dayIdx < 7) cols[dayIdx].fires.push({ when, item });
    }
  }
  for (const c of cols) c.fires.sort((a, b) => a.when.getTime() - b.when.getTime());
  return cols;
}

function startOfLocalDay(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

export interface Filters {
  agent: string | null;
  workspace: string | null;
}

/** Client-side filter over the fetched week — instant, no refetch. */
export function matchesFilters(item: ScheduleWeekItem, f: Filters): boolean {
  if (f.agent && item.schedule.agent_slug !== f.agent) return false;
  if (f.workspace && item.workspace_slug !== f.workspace) return false;
  return true;
}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/schedules/calendarGrid.test.ts`
Expected: PASS (5 passed)

- [ ] **Step 7: Typecheck + commit**

Run: `cd frontend && npm run build`
Expected: build succeeds.

```bash
git add frontend/src/api/generated.ts frontend/src/api/schedules.ts frontend/src/components/schedules/calendarGrid.ts frontend/src/components/schedules/calendarGrid.test.ts
git commit -m "feat(frontend): schedule-week client + pure calendar-grid helpers"
```

---

### Task 5: The `<ScheduleCalendar>` grid + page + routes

**Files:**
- Create: `frontend/src/components/schedules/ScheduleCalendar.tsx`
- Create: `frontend/src/pages/SchedulesPage.tsx`
- Modify: `frontend/src/router.tsx`
- Modify: the header nav (find it — see Step 1)

**Interfaces:**
- Consumes: `getScheduleWeek`, `bucketByDay`, `matchesFilters` (Task 4); the existing `ScheduleEditor` from `@/components/agents/ScheduleEditor`; the existing `Schedule` type.
- Produces: `<ScheduleCalendar />` (self-fetching, reads no props — scope comes from the route it's on); `SchedulesPage` mounting it.

Typecheck-only (no component tests — repo convention). **Design tokens only, no `dark:`**.

- [ ] **Step 1: Find the nav + a root-page precedent**

Run: `grep -rn "to=\"/insights\"\|to=\"/supervisor\"\|'/insights'" frontend/src/components frontend/src/ | head` and read how `/insights` gets a header link and how `InsightsPage`/`SupervisorPage` are structured (self-fetching page, no props). Mirror that. Confirm the exact nav component path before editing.

- [ ] **Step 2: Write the calendar component**

Create `frontend/src/components/schedules/ScheduleCalendar.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Schedule, ScheduleWeekItem } from "@/api/schedules";
import { getScheduleWeek } from "@/api/schedules";
import { ScheduleEditor } from "@/components/agents/ScheduleEditor";
import { bucketByDay, matchesFilters, type Filters } from "./calendarGrid";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Monday 00:00 local of the week containing `d`. */
function mondayOf(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  const dow = (x.getDay() + 6) % 7; // Mon=0
  x.setDate(x.getDate() - dow);
  return x;
}

function timeLabel(d: Date): string {
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** Reads no props: which workspaces it spans is decided by the ROUTE it's on
 * (flat /schedules → all; /w/:ws/schedules → that one), handled transparently
 * by the api client's tenant-rewrite middleware. */
export function ScheduleCalendar({ showWorkspaceFilter }: { showWorkspaceFilter: boolean }) {
  const [weekStart, setWeekStart] = useState(() => mondayOf(new Date()));
  const [items, setItems] = useState<ScheduleWeekItem[] | null>(null);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<Filters>({ agent: null, workspace: null });
  const [editing, setEditing] = useState<{ agentSlug: string; schedule: Schedule } | null>(null);

  const load = useCallback(async () => {
    setItems(null);
    try {
      setItems(await getScheduleWeek(weekStart.toISOString()));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setItems([]);
    }
  }, [weekStart]);

  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(() => (items ?? []).filter((i) => matchesFilters(i, filters)), [items, filters]);
  const columns = useMemo(() => bucketByDay(shown, weekStart), [shown, weekStart]);

  const agents = useMemo(
    () => [...new Set((items ?? []).map((i) => i.schedule.agent_slug))].sort(),
    [items],
  );
  const workspaces = useMemo(
    () => [...new Set((items ?? []).map((i) => i.workspace_slug).filter(Boolean))].sort() as string[],
    [items],
  );

  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;

  function shiftWeek(days: number) {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + days);
    setWeekStart(mondayOf(d));
  }

  return (
    <div className="p-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Schedule</h1>
          <p className="text-xs text-muted-foreground">
            Week of {weekStart.toLocaleDateString(undefined, { month: "short", day: "numeric" })} · times in {tz}
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button type="button" onClick={() => shiftWeek(-7)}
            className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">←</button>
          <button type="button" onClick={() => setWeekStart(mondayOf(new Date()))}
            className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">This week</button>
          <button type="button" onClick={() => shiftWeek(7)}
            className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">→</button>
        </div>
      </header>

      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        <FilterRow label="Agent" value={filters.agent} options={agents}
          onChange={(v) => setFilters((f) => ({ ...f, agent: v }))} />
        {showWorkspaceFilter && (
          <FilterRow label="Workspace" value={filters.workspace} options={workspaces}
            onChange={(v) => setFilters((f) => ({ ...f, workspace: v }))} />
        )}
      </div>

      {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
      {items === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-7">
          {columns.map((col) => (
            <div key={col.index} className="rounded border border-border bg-card">
              <div className="border-b border-border px-2 py-1 text-xs font-medium text-muted-foreground">
                {DAY_LABELS[col.index]} {col.date.getDate()}
              </div>
              <ul className="min-h-16 space-y-1 p-1">
                {col.fires.map((f, i) => (
                  <li key={i}>
                    <button type="button"
                      onClick={() => setEditing({ agentSlug: f.item.schedule.agent_slug, schedule: f.item.schedule })}
                      className="w-full rounded bg-muted px-1.5 py-1 text-left text-xs hover:bg-primary/10">
                      <span className="font-medium text-foreground">{timeLabel(f.when)}</span>{" "}
                      <span className="text-muted-foreground">{f.item.schedule.agent_slug}</span>
                      <div className="truncate text-foreground-secondary">{f.item.schedule.name}</div>
                    </button>
                  </li>
                ))}
                {col.fires.length === 0 && <li className="px-1.5 py-1 text-xs text-foreground-subtle">—</li>}
              </ul>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <ScheduleEditor
          agentSlug={editing.agentSlug}
          schedule={editing.schedule}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); void load(); }}
        />
      )}
    </div>
  );
}

function FilterRow({ label, value, options, onChange }: {
  label: string; value: string | null; options: string[]; onChange: (v: string | null) => void;
}) {
  if (options.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="text-muted-foreground">{label}:</span>
      <button type="button" onClick={() => onChange(null)}
        className={`rounded border px-1.5 py-0.5 ${value === null ? "border-primary text-primary" : "border-border text-foreground-secondary hover:bg-muted"}`}>
        All
      </button>
      {options.map((o) => (
        <button key={o} type="button" onClick={() => onChange(o)}
          className={`rounded border px-1.5 py-0.5 ${value === o ? "border-primary text-primary" : "border-border text-foreground-secondary hover:bg-muted"}`}>
          {o}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Write the page**

Create `frontend/src/pages/SchedulesPage.tsx`:

```tsx
import { useParams } from "react-router-dom";
import { ScheduleCalendar } from "@/components/schedules/ScheduleCalendar";

/** Mounted at BOTH /schedules (personal, spans all workspaces) and
 * /w/:workspace/schedules (that workspace). Same component; the workspace
 * filter is only meaningful — and only shown — on the personal route, since the
 * tenant route is already one workspace. The api client picks the scope from
 * the URL automatically. */
export default function SchedulesPage() {
  const { workspace } = useParams();
  return <ScheduleCalendar showWorkspaceFilter={!workspace} />;
}
```

- [ ] **Step 4: Add the routes**

In `frontend/src/router.tsx`: add a lazy import near the other page imports, and two routes — the root one beside `/insights`/`/supervisor`, the tenant one beside the other `/w/:workspace/…` routes:

```tsx
const SchedulesPage = lazy(() => import('./pages/SchedulesPage'))
```
```tsx
// beside /insights, /supervisor (root, personal):
{ path: '/schedules', element: <SchedulesPage /> },
// in the /w/:workspace block:
{ path: '/w/:workspace/schedules', element: <SchedulesPage /> },
```

- [ ] **Step 5: Add the header nav link**

Following exactly what you found in Step 1, add a "Schedule" link to the same nav that has Insights/Supervisor, pointing to `/schedules`. Tokens only.

- [ ] **Step 6: Typecheck + palette check**

Run: `cd frontend && npm run build`
Expected: build succeeds.

Run: `grep -nE "stone-|orange-|zinc-|slate-|red-|amber-|emerald-|sky-|violet-|dark:" frontend/src/components/schedules/ScheduleCalendar.tsx frontend/src/pages/SchedulesPage.tsx`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/schedules/ScheduleCalendar.tsx frontend/src/pages/SchedulesPage.tsx frontend/src/router.tsx frontend/src/components
git commit -m "feat(frontend): fleet schedule calendar — /schedules + /w/:workspace/schedules"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document the routes + endpoint**

In `CLAUDE.md`:
- Under **Key URLs → Root / personal**, add: `/schedules` — personal weekly calendar of every recurring schedule across all workspaces you belong to (client-filterable by workspace/agent); the per-workspace view is `/w/:workspace/schedules`.
- Add `/w/:workspace/schedules` to the tenant-scoped list.
- Under the **Agents** API section (near the other `schedules` routes), add: `GET /api/agents/schedules/week?start=<iso>` — a week of scheduled fires across the visible fleet; scope follows the URL (flat = all your workspaces, `/w/{ws}/` = that one), computed via `canopy_cron.slots_between`.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: fleet schedule calendar routes + week endpoint"
```

---

## Final Verification

- [ ] **Full backend suite** — `uv run pytest` → PASS (incl. `test_schedule_week.py`, `test_architecture_boundary.py`).
- [ ] **canopy_cron suite** — `cd packages/canopy_cron && uv run --with pytest pytest` → PASS.
- [ ] **Frontend** — `cd frontend && npm run build && npx vitest run` → build ok, vitest passes (incl. `calendarGrid.test.ts`).
- [ ] **Pyflakes + migrations** — `uv run ruff check . --select F --ignore F403,F405` clean; `uv run python manage.py makemigrations --check --dry-run` → No changes detected (this feature adds no models).
- [ ] **Drive it** — `uv run honcho start -f Procfile.dev`, then visit `/schedules` (all workspaces) and `/w/<ws>/schedules` (one), confirm chips render in local time, a filter narrows instantly, and clicking a chip opens the editor and a save refetches.

## Deferred

**Spec non-goals:** month view, drag-to-reschedule, hour-ruled/heatmap layouts, server-side filtering, any new mutation surface (the shared `ScheduleEditor` is the whole write path).

**Cut from this plan, needs a nod:** the spec's "show paused" toggle (render disabled schedules as ghost chips at their would-be times). This plan's `week_schedules` returns only `enabled=True` schedules and the grid has no paused toggle — a "what's firing this week" calendar showing things that *won't* fire is arguably noise, and it adds a rendering mode + more payload. Trivial to add later (drop the `enabled=True` filter; the client hides `!enabled` behind a toggle; `serialize_schedule` already carries `enabled`). Flagged because it's an approved-spec line this plan intentionally drops.
