# Fleet Turn Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only fleet **Activity** page at `/activity` (and `/w/:workspace/activity`) that lists the last 20 harness turns — time, agent, trigger, runner, status — with client-side filters and an inline event-ledger drill-down.

**Architecture:** One additive backend param (`limit` on the existing `GET /api/harness/turns/`), then a pure-TypeScript helpers module (unit-tested with vitest), then a React page that consumes the endpoint via the existing `apiV2` client, then two routes + a nav entry. The page mirrors the existing `SchedulesPage`/`ScheduleCalendar` dual-mount pattern exactly (flat route = all my workspaces; `/w/:ws/` = one tenant — handled transparently by the api client's tenant-rewrite middleware).

**Tech Stack:** Django Ninja + Pydantic v2 (backend), pytest (backend tests), React 19 + Tailwind 4 + `openapi-fetch` (frontend), vitest (frontend pure-function tests).

## Global Constraints

- **Design tokens only** — no raw Tailwind palette literals (`stone-*`, `orange-*`, `red-*`, `amber-*`, `emerald-*`, `sky-*`, etc.) and no `dark:` variants. Use semantic tokens: surfaces `bg-background`/`bg-card`/`bg-muted`, borders `border-border`, text ladder `text-foreground`/`text-foreground-secondary`/`text-muted-foreground`, status `success`/`destructive`/`warning`/`info` (each tinted as `bg-<token>/10 text-<token> border-<token>/30`).
- **UI is dense, readable, tables not cards.**
- **Frontend tests are pure functions only** — vitest, no `@testing-library`, no component render tests.
- **Backend contract:** Django Ninja routes only; errors are RFC 7807 `application/problem+json`. Do not hand-edit `frontend/src/api/generated.ts` — it is regenerated from the OpenAPI schema (`npm run gen:api`).
- **Times display in the viewer's local zone** (this laptop is Mountain Time) via `toLocaleString`; never hardcode an offset.
- Frontend type check / build: `cd frontend && npm run build`. Frontend unit tests: `cd frontend && npx vitest run <path>`. Backend tests: `uv run pytest <path>`.

---

### Task 1: Backend — `limit` param on `list_turns`

**Files:**
- Modify: `apps/harness/api.py:385-398` (`list_turns`)
- Test: `tests/test_harness_api.py` (add one test near the existing `test_list_filter_by_agent_and_status` at line 146)

**Interfaces:**
- Produces: `GET /api/harness/turns/?limit=<int>` — optional query param, clamped to `1..200`, default `100`. Response shape (`list[TurnOut]`), ordering (`-created_at`), tenant filter, and the `agent`/`status` params are all unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_harness_api.py`:

```python
def test_list_respects_limit_and_clamps(client, agent):
    # enqueue 3 turns for the same agent (idempotency_key must differ per row)
    for i in range(3):
        r = client.post(
            "/api/harness/turns/",
            {"agent_slug": "echo", "origin": "manual", "idempotency_key": f"k{i}"},
            content_type="application/json",
        )
        assert r.status_code == 201, r.content

    # limit caps the row count
    assert len(client.get("/api/harness/turns/?limit=2").json()) == 2
    # limit below 1 clamps up to 1 (never returns 0 / everything)
    assert len(client.get("/api/harness/turns/?limit=0").json()) == 1
    # limit above 200 clamps down to 200 (here just <= the 3 we have)
    assert len(client.get("/api/harness/turns/?limit=9999").json()) == 3
    # omitted -> default 100 (existing callers unchanged): returns all 3
    assert len(client.get("/api/harness/turns/").json()) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_api.py::test_list_respects_limit_and_clamps -v`
Expected: FAIL — the endpoint ignores `limit` today, so `?limit=2` returns 3 (the `assert len(...) == 2` fails).

- [ ] **Step 3: Implement the `limit` param**

In `apps/harness/api.py`, change the `list_turns` signature and slice. Current (lines 385-398):

```python
@router.get("/turns/", response=list[TurnOut])
def list_turns(request: HttpRequest, agent: str | None = None, status: str | None = None):
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    qs = Turn.objects.select_related("agent", "claimed_by").order_by("-created_at")
    if agent:
        qs = qs.filter(agent__slug=agent)
    if status:
        qs = qs.filter(status__in=status.split(","))
    # Tenant filter: a turn's tenant is its agent's. Null-workspace agents stay
    # visible (ungated, per the migration-safety rule).
    qs = qs.filter(Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True))
    return list(qs[:100])  # filter BEFORE slicing — a sliced queryset cannot be filtered
```

Change to:

```python
@router.get("/turns/", response=list[TurnOut])
def list_turns(
    request: HttpRequest,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
):
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    qs = Turn.objects.select_related("agent", "claimed_by").order_by("-created_at")
    if agent:
        qs = qs.filter(agent__slug=agent)
    if status:
        qs = qs.filter(status__in=status.split(","))
    # Tenant filter: a turn's tenant is its agent's. Null-workspace agents stay
    # visible (ungated, per the migration-safety rule).
    qs = qs.filter(Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True))
    limit = max(1, min(limit, 200))  # clamp; default 100 keeps existing callers unchanged
    return list(qs[:limit])  # filter BEFORE slicing — a sliced queryset cannot be filtered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness_api.py::test_list_respects_limit_and_clamps tests/test_harness_api.py::test_list_filter_by_agent_and_status -v`
Expected: PASS (both — the new test and the pre-existing filter test, confirming the param is additive).

- [ ] **Step 5: Regenerate the OpenAPI TypeScript types**

The new query param must appear in the generated client so the frontend can pass `limit`.

Run: `cd frontend && npm run gen:api`
Expected: `frontend/src/api/generated.ts` updates so `operations["apps_harness_api_list_turns"]` gains an optional `limit` query param. (Do not hand-edit the file.)

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api.py tests/test_harness_api.py frontend/src/api/generated.ts
git commit -m "feat(harness): list_turns accepts a clamped limit (default 100)"
```

---

### Task 2: Frontend — pure helpers (`turnLog.ts`) + vitest

**Files:**
- Create: `frontend/src/components/activity/turnLog.ts`
- Test: `frontend/src/components/activity/turnLog.test.ts`

**Interfaces:**
- Consumes: `components["schemas"]["TurnOut"]` from `@/api/generated` (fields used: `agent_slug`, `project`, `origin`, `status`, `origin_ref`, `enqueued_by_email`, `claimed_by_name`, `created_at`).
- Produces:
  - `type Turn = components["schemas"]["TurnOut"]`
  - `type TurnFilters = { agent: string | null; origin: string | null; status: string | null }`
  - `originLabel(turn: Turn): string` — human label for the Trigger column.
  - `agentLabel(turn: Turn): string` — `agent_slug` or `project:<project>`.
  - `matchesTurnFilters(turn: Turn, filters: TurnFilters): boolean` — AND across set filters; a `null` filter matches everything.
  - `relativeTime(iso: string, now: Date): string` — e.g. `"2h ago"`, `"just now"`.
  - `STATUS_TOKEN: Record<string, string>` — maps a status string to a tinted-badge className built from semantic tokens.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/activity/turnLog.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import type { Turn } from "./turnLog";
import { originLabel, agentLabel, matchesTurnFilters, relativeTime } from "./turnLog";

function turn(over: Partial<Turn>): Turn {
  return {
    id: "00000000-0000-0000-0000-000000000000",
    agent_slug: "eva",
    project: "",
    target: "eva",
    workspace_slug: "alpha",
    origin: "manual",
    status: "done",
    routing: "prefer_local",
    prompt: "",
    origin_ref: {},
    claimed_by_name: "jj-mbp",
    enqueued_by_email: "jj@dimagi.com",
    session_id: "",
    result_note: "",
    created_at: "2026-07-20T18:00:00Z",
    claimed_at: null,
    started_at: null,
    finished_at: null,
    lease_expires_at: null,
    ...over,
  } as Turn;
}

describe("agentLabel", () => {
  it("uses the agent slug for agent turns", () => {
    expect(agentLabel(turn({ agent_slug: "eva" }))).toBe("eva");
  });
  it("falls back to project:<name> for project turns", () => {
    expect(agentLabel(turn({ agent_slug: null, project: "canopy-web" }))).toBe("project:canopy-web");
  });
});

describe("originLabel", () => {
  it("labels a cron turn with its fired slot from origin_ref", () => {
    const t = turn({ origin: "cron", origin_ref: { slot: "2026-07-20T13:00:00Z" } });
    expect(originLabel(t)).toContain("cron");
    expect(originLabel(t)).toContain("2026"); // the slot is surfaced
  });
  it("labels a manual turn with the enqueuer email", () => {
    expect(originLabel(turn({ origin: "manual", enqueued_by_email: "jj@dimagi.com" }))).toContain("jj@dimagi.com");
  });
  it("passes email / api through as the bare origin", () => {
    expect(originLabel(turn({ origin: "email", enqueued_by_email: null }))).toBe("email");
    expect(originLabel(turn({ origin: "api", enqueued_by_email: null }))).toBe("api");
  });
});

describe("matchesTurnFilters", () => {
  const t = turn({ agent_slug: "eva", origin: "cron", status: "done" });
  it("matches when all filters are null (identity)", () => {
    expect(matchesTurnFilters(t, { agent: null, origin: null, status: null })).toBe(true);
  });
  it("matches when every set filter agrees", () => {
    expect(matchesTurnFilters(t, { agent: "eva", origin: "cron", status: "done" })).toBe(true);
  });
  it("rejects when any set filter disagrees (AND)", () => {
    expect(matchesTurnFilters(t, { agent: "eva", origin: "manual", status: null })).toBe(false);
  });
  it("filters project turns by their project:<name> agent label", () => {
    const p = turn({ agent_slug: null, project: "canopy-web" });
    expect(matchesTurnFilters(p, { agent: "project:canopy-web", origin: null, status: null })).toBe(true);
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-07-20T18:00:00Z");
  it("reads 'just now' within a minute", () => {
    expect(relativeTime("2026-07-20T17:59:30Z", now)).toBe("just now");
  });
  it("reads minutes", () => {
    expect(relativeTime("2026-07-20T17:45:00Z", now)).toBe("15m ago");
  });
  it("reads hours", () => {
    expect(relativeTime("2026-07-20T16:00:00Z", now)).toBe("2h ago");
  });
  it("reads days", () => {
    expect(relativeTime("2026-07-18T18:00:00Z", now)).toBe("2d ago");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/activity/turnLog.test.ts`
Expected: FAIL — `turnLog.ts` does not exist yet ("Cannot find module './turnLog'").

- [ ] **Step 3: Implement the helpers**

Create `frontend/src/components/activity/turnLog.ts`:

```typescript
import type { components } from "@/api/generated";

export type Turn = components["schemas"]["TurnOut"];

export type TurnFilters = {
  agent: string | null;
  origin: string | null;
  status: string | null;
};

/** Agent turns show their slug; project turns have no agent, so show the repo. */
export function agentLabel(turn: Turn): string {
  return turn.agent_slug ?? `project:${turn.project}`;
}

/** The Trigger column: what caused this turn.
 * - cron   → "cron · <fired slot>" (the slot lives in origin_ref.slot)
 * - manual → "manual · <who enqueued it>"
 * - email/api (or anything else) → the bare origin string */
export function originLabel(turn: Turn): string {
  if (turn.origin === "cron") {
    const slot = turn.origin_ref?.slot;
    return typeof slot === "string" ? `cron · ${slot}` : "cron";
  }
  if (turn.origin === "manual" && turn.enqueued_by_email) {
    return `manual · ${turn.enqueued_by_email}`;
  }
  return turn.origin;
}

/** AND across the set filters; a null filter matches everything. The agent
 * filter compares against agentLabel so project turns filter by project:<name>
 * — the same string the filter dropdown offers. */
export function matchesTurnFilters(turn: Turn, filters: TurnFilters): boolean {
  if (filters.agent && agentLabel(turn) !== filters.agent) return false;
  if (filters.origin && turn.origin !== filters.origin) return false;
  if (filters.status && turn.status !== filters.status) return false;
  return true;
}

/** Compact relative age. `now` is injected so the function is pure/testable. */
export function relativeTime(iso: string, now: Date): string {
  const secs = Math.floor((now.getTime() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Status → tinted-badge className, built only from semantic tokens. Unknown
 * statuses fall back to the muted style. */
export const STATUS_TOKEN: Record<string, string> = {
  done: "bg-success/10 text-success border-success/30",
  failed: "bg-destructive/10 text-destructive border-destructive/30",
  missed: "bg-warning/10 text-warning border-warning/30",
  running: "bg-info/10 text-info border-info/30",
  claimed: "bg-muted text-muted-foreground border-border",
  queued: "bg-muted text-muted-foreground border-border",
};

export function statusToken(status: string): string {
  return STATUS_TOKEN[status] ?? "bg-muted text-muted-foreground border-border";
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/activity/turnLog.test.ts`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/activity/turnLog.ts frontend/src/components/activity/turnLog.test.ts
git commit -m "feat(activity): pure turn-log helpers (origin/agent labels, filter predicate, relative time)"
```

---

### Task 3: Frontend — the api wrapper (`api/turns.ts`)

**Files:**
- Create: `frontend/src/api/turns.ts`

**Interfaces:**
- Consumes: `apiV2` from `@/api/client.v2`, `problemMessage` from `@/api/problem`, `Turn` from `@/components/activity/turnLog`, `components` from `@/api/generated`.
- Produces:
  - `listTurns(limit?: number): Promise<Turn[]>` — GET `/api/harness/turns/` (the client rewrites to `/api/w/{ws}/...` under a tenant route automatically). Defaults `limit` to 20.
  - `type TurnEvent = components["schemas"]["TurnEventOut"]`
  - `listTurnEvents(turnId: string): Promise<TurnEvent[]>` — GET `/api/harness/turns/{turn_id}/events`.

- [ ] **Step 1: Write the wrapper**

Create `frontend/src/api/turns.ts`:

```typescript
import { apiV2 } from "./client.v2";
import { problemMessage } from "./problem";
import type { components } from "./generated";
import type { Turn } from "@/components/activity/turnLog";

export type TurnEvent = components["schemas"]["TurnEventOut"];

/** Last N turns across the current scope (all my workspaces on /activity, one
 * tenant on /w/:ws/activity — the api client picks scope from the URL). */
export async function listTurns(limit = 20): Promise<Turn[]> {
  const { data, error } = await apiV2.GET("/api/harness/turns/", {
    params: { query: { limit } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load activity"));
  return data as Turn[];
}

/** The append-only event ledger for one turn (drill-down). */
export async function listTurnEvents(turnId: string): Promise<TurnEvent[]> {
  const { data, error } = await apiV2.GET("/api/harness/turns/{turn_id}/events", {
    params: { path: { turn_id: turnId } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load turn events"));
  return data.events as TurnEvent[];
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS — no type errors. (If `params.query.limit` errors, Task 1 Step 5 `gen:api` did not run; re-run it.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/turns.ts
git commit -m "feat(activity): api wrappers for listTurns + listTurnEvents"
```

---

### Task 4: Frontend — the `ActivityPage` component

**Files:**
- Create: `frontend/src/pages/ActivityPage.tsx`

**Interfaces:**
- Consumes: `listTurns`, `listTurnEvents`, `TurnEvent` from `@/api/turns`; `Turn`, `TurnFilters`, `agentLabel`, `originLabel`, `matchesTurnFilters`, `relativeTime`, `statusToken` from `@/components/activity/turnLog`; `useParams` from `react-router-dom`.
- Produces: default-exported `ActivityPage` React component. Mounts at both `/activity` and `/w/:workspace/activity` (Task 5 wires the routes). Reads no props; scope comes from the route.

- [ ] **Step 1: Write the component**

Create `frontend/src/pages/ActivityPage.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { listTurns, listTurnEvents, type TurnEvent } from "@/api/turns";
import {
  type Turn,
  type TurnFilters,
  agentLabel,
  originLabel,
  matchesTurnFilters,
  relativeTime,
  statusToken,
} from "@/components/activity/turnLog";

const LIMIT = 20;

/** Mounted at BOTH /activity (all my workspaces) and /w/:workspace/activity
 * (that workspace). Same component; the api client picks the scope from the
 * URL. Read-only log of the last 20 fired turns. Sibling of the Schedule page:
 * schedules are what WILL fire, this is what DID. */
export default function ActivityPage() {
  const [turns, setTurns] = useState<Turn[] | null>(null);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<TurnFilters>({ agent: null, origin: null, status: null });
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setTurns(null);
    try {
      setTurns(await listTurns(LIMIT));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setTurns([]);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(
    () => (turns ?? []).filter((t) => matchesTurnFilters(t, filters)),
    [turns, filters],
  );
  const agents = useMemo(
    () => [...new Set((turns ?? []).map(agentLabel))].sort(),
    [turns],
  );
  const origins = useMemo(
    () => [...new Set((turns ?? []).map((t) => t.origin))].sort(),
    [turns],
  );
  const statuses = useMemo(
    () => [...new Set((turns ?? []).map((t) => t.status))].sort(),
    [turns],
  );

  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = new Date();

  return (
    <div className="p-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Activity</h1>
          <p className="text-xs text-muted-foreground">
            Last {LIMIT} triggered turns · times in {tz}
          </p>
        </div>
        <button type="button" onClick={() => void load()}
          className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">
          Refresh
        </button>
      </header>

      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        <FilterRow label="Agent" value={filters.agent} options={agents}
          onChange={(v) => setFilters((f) => ({ ...f, agent: v }))} />
        <FilterRow label="Trigger" value={filters.origin} options={origins}
          onChange={(v) => setFilters((f) => ({ ...f, origin: v }))} />
        <FilterRow label="Status" value={filters.status} options={statuses}
          onChange={(v) => setFilters((f) => ({ ...f, status: v }))} />
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {turns === null ? (
        <SkeletonRows />
      ) : shown.length === 0 ? (
        <EmptyState hasTurns={(turns ?? []).length > 0} />
      ) : (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Trigger</th>
                <th className="px-3 py-2 font-medium">Runner</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((t) => (
                <TurnRow key={t.id} turn={t} now={now}
                  open={expanded === t.id}
                  onToggle={() => setExpanded((cur) => (cur === t.id ? null : t.id))} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TurnRow({ turn, now, open, onToggle }: {
  turn: Turn; now: Date; open: boolean; onToggle: () => void;
}) {
  return (
    <>
      <tr onClick={onToggle}
        className="cursor-pointer border-b border-border last:border-0 hover:bg-muted">
        <td className="px-3 py-2 text-foreground-secondary" title={new Date(turn.created_at).toLocaleString()}>
          {relativeTime(turn.created_at, now)}
        </td>
        <td className="px-3 py-2 text-foreground">{agentLabel(turn)}</td>
        <td className="px-3 py-2 text-muted-foreground">{originLabel(turn)}</td>
        <td className="px-3 py-2 text-foreground-secondary">{turn.claimed_by_name ?? "—"}</td>
        <td className="px-3 py-2">
          <span className={`inline-block rounded border px-1.5 py-0.5 text-xs ${statusToken(turn.status)}`}>
            {turn.status}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="border-b border-border bg-card">
          <td colSpan={5} className="px-3 py-2">
            <EventLedger turnId={turn.id} />
          </td>
        </tr>
      )}
    </>
  );
}

/** Lazily loads the turn's event ledger on first expand; component unmounts on
 * collapse, so re-expanding refetches — fine for a rarely-opened drill-down. */
function EventLedger({ turnId }: { turnId: string }) {
  const [events, setEvents] = useState<TurnEvent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    listTurnEvents(turnId)
      .then((e) => { if (alive) setEvents(e); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to load events"); });
    return () => { alive = false; };
  }, [turnId]);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (events === null) return <p className="text-xs text-muted-foreground">Loading events…</p>;
  if (events.length === 0) return <p className="text-xs text-muted-foreground">No events recorded.</p>;

  return (
    <ol className="space-y-1">
      {events.map((e) => (
        <li key={e.seq} className="flex gap-2 text-xs">
          <span className="text-foreground-subtle tabular-nums">#{e.seq}</span>
          <span className="text-foreground-secondary" title={new Date(e.ts).toLocaleString()}>
            {new Date(e.ts).toLocaleTimeString()}
          </span>
          <span className="font-medium text-foreground">{e.kind}</span>
        </li>
      ))}
    </ol>
  );
}

function FilterRow({ label, value, options, onChange }: {
  label: string; value: string | null; options: string[]; onChange: (v: string | null) => void;
}) {
  return (
    <label className="flex items-center gap-1">
      <span className="text-muted-foreground">{label}</span>
      <select
        className="rounded border border-input bg-input px-1.5 py-1 text-foreground"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">All</option>
        {options.map((o) => (<option key={o} value={o}>{o}</option>))}
      </select>
    </label>
  );
}

function SkeletonRows() {
  return (
    <div className="space-y-2" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-8 animate-pulse rounded bg-muted" />
      ))}
    </div>
  );
}

function EmptyState({ hasTurns }: { hasTurns: boolean }) {
  return (
    <p className="text-sm text-muted-foreground">
      {hasTurns
        ? "No turns match these filters."
        : "No turns yet. Triggered turns appear here — from a schedule, an email, or a manual run."}
    </p>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS — no type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/ActivityPage.tsx
git commit -m "feat(activity): ActivityPage — dense turn-log table, filters, event-ledger drill-down"
```

---

### Task 5: Frontend — wire routes + nav entry

**Files:**
- Modify: `frontend/src/router.tsx` (imports near line 22; personal route near line 138; tenant route near line 151)
- Modify: `frontend/src/components/AppLayout/AppLayout.tsx` (the `NAV_ITEMS` array, line 20-22)

**Interfaces:**
- Consumes: default-exported `ActivityPage` from `@/pages/ActivityPage` (Task 4).
- Produces: reachable routes `/activity` and `/w/:workspace/activity`, and an "Activity" nav link (personal/global, `tenant: false`).

- [ ] **Step 1: Import the page in the router**

In `frontend/src/router.tsx`, add next to the other page imports (e.g. after the `SupervisorPage` import at line 22):

```tsx
import ActivityPage from '@/pages/ActivityPage'
```

- [ ] **Step 2: Add the personal route**

In `frontend/src/router.tsx`, in the "Personal / global" block, right after the `/schedules` line (line 138):

```tsx
      { path: '/activity', element: <ActivityPage /> },
```

- [ ] **Step 3: Add the tenant route**

In `frontend/src/router.tsx`, in the "Tenant-scoped surfaces under /w/:workspace" block, right after the `/w/:workspace/schedules` line (line 151):

```tsx
      { path: '/w/:workspace/activity', element: <ActivityPage /> },
```

- [ ] **Step 4: Add the nav entry**

In `frontend/src/components/AppLayout/AppLayout.tsx`, in `NAV_ITEMS`, right after the Schedule entry (line 21):

```tsx
  { path: '/activity', label: 'Activity', tenant: false },
```

- [ ] **Step 5: Build to verify wiring + types**

Run: `cd frontend && npm run build`
Expected: PASS — type check + Vite build succeed. The route and nav now resolve `ActivityPage`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/router.tsx frontend/src/components/AppLayout/AppLayout.tsx
git commit -m "feat(activity): mount /activity + /w/:workspace/activity and add the nav entry"
```

---

### Task 6: Full-suite sanity + final verification

**Files:** none (verification only).

- [ ] **Step 1: Backend tests green**

Run: `uv run pytest tests/test_harness_api.py -q`
Expected: PASS — including the new `test_list_respects_limit_and_clamps` and all pre-existing harness API tests.

- [ ] **Step 2: Frontend unit tests green**

Run: `cd frontend && npx vitest run src/components/activity/turnLog.test.ts`
Expected: PASS.

- [ ] **Step 3: Frontend build green**

Run: `cd frontend && npm run build`
Expected: PASS — type check + build.

- [ ] **Step 4: No palette-literal / `dark:` regressions in new files**

Run: `grep -REn "stone-|orange-|zinc-|slate-|red-[0-9]|amber-[0-9]|emerald-|sky-[0-9]|violet-|dark:" frontend/src/pages/ActivityPage.tsx frontend/src/components/activity/`
Expected: no matches (exit 1 / empty output). Any hit is a token violation to fix before finishing.

- [ ] **Step 5: Manual smoke (optional, if a dev server is handy)**

Run backend + frontend (`uv run honcho start -f Procfile.dev`), visit `/activity`, confirm: the last 20 turns render newest-first; agent/trigger/status filters narrow the list; clicking a row expands its event ledger; `/w/<ws>/activity` shows only that workspace. This is a confirmation, not a gate.

---

## Self-Review Notes

- **Spec coverage:** route + placement (Task 5), `limit` backend param (Task 1), the five columns + status tokens (Task 2 helpers + Task 4 render), client-side filters (Task 2 predicate + Task 4 controls), row event-ledger drill-down (Task 3 wrapper + Task 4 `EventLedger`), loading/empty/error states (Task 4), backend + vitest tests (Tasks 1, 2), design-tokens-only guard (Task 6 Step 4). All spec sections map to a task.
- **Type consistency:** `Turn` is defined once in `turnLog.ts` (Task 2) and imported everywhere else (Tasks 3, 4). `TurnFilters` fields (`agent`/`origin`/`status`) are identical across the predicate (Task 2), the api is not involved, and the page state (Task 4). `TurnEvent` is `components["schemas"]["TurnEventOut"]` in both Task 3 and Task 4; its fields used (`seq`, `ts`, `kind`) match the generated schema. `listTurns(limit=20)` / `listTurnEvents(turnId)` signatures match their call sites in Task 4.
- **Note on `origin_ref.slot`:** the cron firing path stores the slot under `origin_ref` (idempotency key `sched:<id>:<slot>`); `originLabel` reads `origin_ref?.slot` defensively and falls back to bare `"cron"` if absent, so a cron turn without a serialized slot still renders.
