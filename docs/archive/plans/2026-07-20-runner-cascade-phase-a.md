# Runner cascade — Phase A (the `ready` signal + mobile runner detail) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runner reports whether it can actually fire a turn (`ready` + a reason), separate from merely being online, and the mobile UI lets you tap a runner to see its on/ready state and why.

**Architecture:** The runner computes `ready = cdp_healthy() AND not(recent-failure)` — the existing #277/#278 CDP preflight is the proactive half; a marker file in the runner's state dir (written on any `fail_turn`, cleared on `finish(done)`) is the reactive half that catches "online but the turn failed" (e.g. not-logged-in). It sends `ready` + `ready_note` in the heartbeat; canopy-web persists them on `Runner` and exposes them on `RunnerOut`; the mobile Agents tab gains a runner-detail view. No routing changes — that's Phase B.

**Tech Stack:** Django 5 + Django Ninja + Postgres; stdlib `canopy_runner`; React 19 + Vite + canopy-ui + react-router; pytest; Playwright.

**Spec:** `docs/superpowers/specs/2026-07-20-runner-cascade-design.md` (Phase A).

## Global Constraints

- **Verify like CI:** `uv run pytest` with `.env` moved aside (`mv .env /tmp/.env.aside; …; mv /tmp/.env.aside .env`).
- **`canopy_runner` is stdlib-only** and NOT on the main suite's path — its tests run from `packages/canopy_runner` (`uv run --with pytest pytest`). A canopy-web test that imports it uses a per-file `sys.path` insert (see `tests/test_mobile_loop_e2e.py`).
- **No routing change in Phase A** — do not touch `claim_next_turn`. `ready` is additive; the cascade consumes it in Phase B.
- **Design tokens only** in the UI — no raw palette literals. Use `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, status tokens (`text-success`, `text-warning`, `text-destructive`).
- **Never hand-edit `frontend/src/api/generated.ts`** — regenerate offline (Task 1 shows the command).
- **Tests:** `pytestmark`/`@pytest.mark.django_db`, fixtures inline per file, no `tests/conftest.py`.
- **CI does not run Playwright** — run `npx playwright test` locally before merge.

## File Structure

- `apps/harness/models.py` — `Runner.ready` + `Runner.ready_note` (modify).
- `apps/harness/migrations/00NN_runner_ready.py` — migration.
- `apps/harness/schemas.py` — `HeartbeatIn` gains `ready`/`ready_note`; `RunnerOut` exposes them (modify).
- `apps/harness/services.py` — `heartbeat()` persists them (modify).
- `apps/harness/api.py` — the heartbeat route passes them through (modify).
- `tests/test_harness_runner_ready.py` — backend tests (create).
- `packages/canopy_runner/canopy_runner/readiness.py` — the reactive marker read/write (create).
- `packages/canopy_runner/canopy_runner/client.py` — `heartbeat()` sends `ready`/`ready_note` (modify).
- `packages/canopy_runner/canopy_runner/main.py` — compute `ready` at the heartbeat sites (modify).
- `packages/canopy_runner/canopy_runner/execute.py` — mark not-ready on fail, ready on done (modify).
- `packages/canopy_runner/tests/test_readiness.py` — runner tests (create).
- `frontend/src/api/harness.ts` — `getRunner` (or reuse list) + types (modify).
- `frontend/src/components/supervisor/RunnerDetail.tsx` — the detail view (create).
- `frontend/src/components/supervisor/RunnerStatus.tsx` — not-ready indicator + tap-through (modify).
- `frontend/src/pages/SupervisorPage.tsx` — wire the detail (modify).
- `frontend/e2e/seed.py` + `frontend/e2e/supervisor.spec.ts` — seed a not-ready runner + test (modify).

---

## Task 1: canopy-web — `Runner.ready` / `ready_note`, heartbeat persists, `RunnerOut` exposes

**Files:**
- Modify: `apps/harness/models.py`, `apps/harness/schemas.py`, `apps/harness/services.py`, `apps/harness/api.py`
- Create: `apps/harness/migrations/00NN_runner_ready.py`, `tests/test_harness_runner_ready.py`
- Modify: `frontend/src/api/generated.ts` (regenerate)

**Interfaces:**
- Produces: `Runner.ready: bool` (default True), `Runner.ready_note: str`; `HeartbeatIn.ready: bool = True`, `HeartbeatIn.ready_note: str = ""`; `RunnerOut.ready`, `RunnerOut.ready_note`; `services.heartbeat(..., ready=True, ready_note="")` persists them.

- [ ] **Step 1: Write the failing test** — create `tests/test_harness_runner_ready.py`:

```python
"""Runner readiness: the 'can I fire a turn' signal, distinct from being online."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _runner(user, ws):
    return Runner.objects.create(
        name="mbp", kind=Runner.EMDASH, host="h", paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


@pytest.fixture
def user(db):
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture
def ws(db, user):
    w = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=w, role=WorkspaceMembership.OWNER)
    return w


def test_runner_defaults_to_ready(user, ws):
    r = _runner(user, ws)
    assert r.ready is True
    assert r.ready_note == ""


def test_heartbeat_persists_not_ready_with_a_reason(user, ws):
    r = _runner(user, ws)
    c = Client()
    c.force_login(user)
    resp = c.post(
        f"/api/harness/runners/{r.id}/heartbeat",
        {"active_turn_ids": [], "ready": False, "ready_note": "Not logged in"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["ready"] is False
    assert body["ready_note"] == "Not logged in"
    r.refresh_from_db()
    assert r.ready is False and r.ready_note == "Not logged in"


def test_heartbeat_omitting_ready_defaults_to_ready_true(user, ws):
    """An older runner that predates the field still heartbeats — it must read as
    ready (fail OPEN: an un-upgraded runner is presumed able to fire, as today)."""
    r = _runner(user, ws)
    Runner.objects.filter(pk=r.pk).update(ready=False, ready_note="stale")
    c = Client()
    c.force_login(user)
    c.post(f"/api/harness/runners/{r.id}/heartbeat", {"active_turn_ids": []},
           content_type="application/json")
    r.refresh_from_db()
    assert r.ready is True and r.ready_note == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_runner_ready.py -q; mv /tmp/.env.aside .env`
Expected: FAIL — `Runner` has no attribute `ready`.

- [ ] **Step 3: Add the model fields** — in `apps/harness/models.py`, on `Runner` (near `status`/`status_note`):

```python
    # Can this runner actually FIRE a turn right now — distinct from being online.
    # Set from the heartbeat: the runner self-reports cdp_healthy() AND not-recently-
    # failed. `available = live_status == ONLINE and ready` (the Phase B cascade gate).
    # Defaults True so an un-upgraded runner reads as able to fire, matching prior behavior.
    ready = models.BooleanField(default=True)
    ready_note = models.CharField(max_length=200, blank=True, default="")
```

- [ ] **Step 4: Migration**

Run: `uv run python manage.py makemigrations harness`
Expected: creates `00NN_runner_ready.py` adding the two fields.

- [ ] **Step 5: Wire the schemas** — in `apps/harness/schemas.py`:

`HeartbeatIn` gains (after `host`):
```python
    ready: bool = True          # can the runner fire a turn (cdp healthy ∧ not recently failed)
    ready_note: str = ""
```
`RunnerOut` gains (after `status_note` / wherever status lives):
```python
    ready: bool
    ready_note: str
```

- [ ] **Step 6: Persist in the service** — in `apps/harness/services.py`, `heartbeat(...)`:

```python
def heartbeat(
    runner: Runner, *, active_turn_ids: list[str], degraded: bool = False, note: str = "",
    ready: bool = True, ready_note: str = "",
) -> Runner:
    now = timezone.now()
    runner.last_heartbeat_at = now
    runner.status = Runner.DEGRADED if degraded else Runner.ONLINE
    runner.status_note = note
    runner.ready = ready
    runner.ready_note = ready_note
    runner.save(update_fields=["last_heartbeat_at", "status", "status_note", "ready", "ready_note"])
    if active_turn_ids:
        Turn.objects.filter(
            pk__in=active_turn_ids, claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
    return runner
```

- [ ] **Step 7: Pass them through the route** — in `apps/harness/api.py`, the heartbeat handler calls `services.heartbeat(...)`; add `ready=payload.ready, ready_note=payload.ready_note` to that call.

- [ ] **Step 8: Run to verify it passes**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_runner_ready.py -q; mv /tmp/.env.aside .env`
Expected: PASS (3).

- [ ] **Step 9: Regenerate types**

```bash
uv run python -c "
import django, json, os
os.environ['DJANGO_SETTINGS_MODULE']='config.settings.test'
django.setup()
from apps.api.api import api
json.dump(api.get_openapi_schema(), open('frontend/openapi.json','w'), indent=2)
"
cd frontend && npx openapi-typescript openapi.json --output src/api/generated.ts --immutable && cd ..
```
Verify: `grep -c 'ready_note' frontend/src/api/generated.ts` ≥ 1.

- [ ] **Step 10: Commit**

```bash
git add apps/harness/models.py apps/harness/schemas.py apps/harness/services.py apps/harness/api.py apps/harness/migrations/00*_runner_ready.py tests/test_harness_runner_ready.py frontend/src/api/generated.ts
git commit -m "feat(harness): a runner reports whether it can FIRE (ready), not just that it's online"
```

---

## Task 2: runner — compute `ready` (proactive CDP ∧ reactive failure) + report it

**Files:**
- Create: `packages/canopy_runner/canopy_runner/readiness.py`
- Modify: `packages/canopy_runner/canopy_runner/client.py`, `packages/canopy_runner/canopy_runner/main.py`, `packages/canopy_runner/canopy_runner/execute.py`
- Create: `packages/canopy_runner/tests/test_readiness.py`

**Interfaces:**
- Consumes: `POST /runners/{id}/heartbeat` accepting `ready`/`ready_note` (Task 1); `cdp_control.cdp_healthy`.
- Produces: `readiness.mark_failed(cfg, note)`, `readiness.mark_ok(cfg)`, `readiness.compute(cfg) -> tuple[bool, str]`; `Client.heartbeat(..., ready=True, ready_note="")`.

- [ ] **Step 1: Write the failing test** — create `packages/canopy_runner/tests/test_readiness.py`:

```python
from types import SimpleNamespace

from canopy_runner import readiness


def _cfg(tmp_path):
    return SimpleNamespace(state_path=str(tmp_path / "runner-state.json"), cdp_port=9222)


def test_compute_not_ready_when_cdp_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: False)
    ready, note = readiness.compute(_cfg(tmp_path))
    assert ready is False
    assert "emdash" in note.lower() or "cdp" in note.lower()


def test_compute_ready_when_cdp_healthy_and_no_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    ready, note = readiness.compute(_cfg(tmp_path))
    assert ready is True and note == ""


def test_reactive_failure_flips_not_ready_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    readiness.mark_failed(cfg, "Not logged in")
    ready, note = readiness.compute(cfg)
    assert ready is False and note == "Not logged in"     # CDP fine, but a turn just failed
    readiness.mark_ok(cfg)
    ready, note = readiness.compute(cfg)
    assert ready is True and note == ""                    # a clean run clears it


def test_marker_survives_process_restart(tmp_path, monkeypatch):
    """--drain-one is one-shot; the marker must persist on disk, not in memory."""
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    readiness.mark_failed(cfg, "boom")
    # a fresh cfg pointing at the same state dir (simulates a new process)
    cfg2 = _cfg(tmp_path)
    assert readiness.compute(cfg2) == (False, "boom")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd packages/canopy_runner && uv run --with pytest pytest tests/test_readiness.py -q; cd ../..`
Expected: FAIL — no module `canopy_runner.readiness`.

- [ ] **Step 3: Implement `readiness.py`**

```python
"""Runner readiness — the 'can I fire a turn' self-assessment reported in the heartbeat.

Two halves:
- proactive: cdp_control.cdp_healthy() — is emdash up with its debug port (the #277/#278
  preflight).
- reactive: a marker file next to the runner's state. A failed turn writes it (with the
  reason); a clean turn clears it. This is how "online but not logged in" — invisible to a
  CDP probe — becomes a not-ready signal. It lives ON DISK so it survives --drain-one's
  one-shot process.
"""
from __future__ import annotations

from pathlib import Path

from . import cdp_control

_MARKER = "not-ready"


def _marker(cfg) -> Path:
    base = Path(cfg.state_path).parent if getattr(cfg, "state_path", "") else Path.home() / ".canopy"
    return base / _MARKER


def mark_failed(cfg, note: str) -> None:
    """A turn failed — this runner may be unable to fire (auth/health). Record why."""
    try:
        p = _marker(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((note or "recent turn failed")[:200])
    except OSError:
        pass  # best-effort; a missing marker just means "presumed ready"


def mark_ok(cfg) -> None:
    """A turn succeeded — clear any prior failure marker."""
    try:
        _marker(cfg).unlink(missing_ok=True)
    except OSError:
        pass


def compute(cfg) -> tuple[bool, str]:
    """(ready, ready_note). Not ready if emdash's CDP is unreachable, or a recent turn
    failed and hasn't been cleared by a clean run."""
    if not cdp_control.cdp_healthy(port=getattr(cfg, "cdp_port", 9222)):
        return False, "emdash CDP unreachable"
    try:
        note = _marker(cfg).read_text().strip()
        if note:
            return False, note
    except OSError:
        pass
    return True, ""
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd packages/canopy_runner && uv run --with pytest pytest tests/test_readiness.py -q; cd ../..`
Expected: PASS (4).

- [ ] **Step 5: Client sends `ready`/`ready_note`** — in `client.py`, `heartbeat(...)`:

```python
    def heartbeat(self, runner_id: str, active_turn_ids: list[str], degraded: bool = False,
                  note: str = "", host: str = "", ready: bool = True, ready_note: str = "") -> dict:
        _, payload = self._call(
            "POST", f"/runners/{runner_id}/heartbeat",
            {"active_turn_ids": active_turn_ids, "degraded": degraded, "note": note,
             "host": host, "ready": ready, "ready_note": ready_note},
        )
        return payload or {}
```

- [ ] **Step 6: Report `ready` at the primary heartbeat** — in `main.py`, `_run_once_cdp`, the healthy-path heartbeat call `client.heartbeat(cfg.runner_id, [], host=host_id())` becomes:

```python
    from . import readiness
    _ready, _rnote = readiness.compute(cfg)
    client.heartbeat(cfg.runner_id, [], host=host_id(), ready=_ready, ready_note=_rnote)
```
(Leave the `degraded=True` unhealthy-path heartbeats as they are — those already fire when CDP is down; `compute()` will also return not-ready there, and either signal surfaces it. Do the same one-line `readiness.compute` addition at the `drain_one` heartbeat `client.heartbeat(cfg.runner_id, [], host=host_id())`.)

- [ ] **Step 7: Flip the reactive marker in execute** — in `execute.py`: at every `client.fail_turn(turn_id, <note>)`, immediately precede it with `readiness.mark_failed(cfg, <note>)`; at every successful `client.finish(turn_id, ...)`, precede it with `readiness.mark_ok(cfg)`. Add `from . import readiness` at the top. (There are two `fail_turn` sites and two `finish` sites — the reuse and create branches.)

- [ ] **Step 8: Run the whole runner suite**

Run: `cd packages/canopy_runner && uv run --with pytest pytest -q; cd ../..`
Expected: PASS (existing + new). If `test_mobile_loop_e2e.py` (canopy-web side) or execute tests reference the changed `finish`/`fail_turn` flow, confirm they still pass — the marker calls are additive.

- [ ] **Step 9: Commit**

```bash
git add packages/canopy_runner/canopy_runner/readiness.py packages/canopy_runner/canopy_runner/client.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/canopy_runner/execute.py packages/canopy_runner/tests/test_readiness.py
git commit -m "feat(runner): report readiness (cdp health ∧ no recent failure) in the heartbeat"
```

---

## Task 3: mobile — runner detail view + not-ready indicator

**Files:**
- Modify: `frontend/src/api/harness.ts`
- Create: `frontend/src/components/supervisor/RunnerDetail.tsx`
- Modify: `frontend/src/components/supervisor/RunnerStatus.tsx`, `frontend/src/pages/SupervisorPage.tsx`
- Modify: `frontend/e2e/seed.py`, `frontend/e2e/supervisor.spec.ts`

**Interfaces:**
- Consumes: `RunnerOut` (now with `ready`, `ready_note`) from `listRunners()`.
- Produces: a tappable runner row that opens a detail panel showing on/ready/why + kind/host/workspace/capabilities/heartbeat.

- [ ] **Step 1: The detail component** — create `frontend/src/components/supervisor/RunnerDetail.tsx`:

```tsx
import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'

// A runner's full state — the click-through from the Agents tab's runner list.
// Surfaces the two signals that matter: is it ON (heartbeating) and is it READY
// (can actually fire a turn), with the reason it isn't.
export function RunnerDetail({ runner, onBack }: { runner: RunnerOut; onBack: () => void }): JSX.Element {
  const online = runner.status === 'online'
  const caps = (runner.capabilities ?? {}) as { agents?: string[]; projects?: string[] }
  const row = (label: string, value: string) => (
    <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-[13px] text-foreground">{value}</span>
    </div>
  )
  return (
    <div className="flex flex-col gap-2" data-testid={`runner-detail-${runner.name}`}>
      <button type="button" onClick={onBack} className="self-start text-[12px] text-primary" data-testid="runner-detail-back">
        ← Runners
      </button>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${online ? 'bg-success' : 'bg-muted-foreground'}`} />
        <span className="text-[15px] font-semibold text-foreground">{runner.name}</span>
        <span
          data-testid="runner-detail-ready"
          className={`ml-auto rounded px-1.5 py-0.5 text-[11px] ${runner.ready ? 'bg-success/15 text-success' : 'bg-destructive/15 text-destructive'}`}
        >
          {runner.ready ? 'ready' : 'not ready'}
        </span>
      </div>
      {!runner.ready && runner.ready_note && (
        <p className="text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      <div className="rounded-lg border border-border bg-card p-3">
        {row('status', online ? 'online' : (runner.status ?? 'unknown'))}
        {row('kind', runner.kind ?? '')}
        {row('host', runner.host ?? '')}
        {row('workspace', runner.workspace ?? '')}
        {row('agents', (caps.agents ?? []).join(', ') || '—')}
        {row('projects', (caps.projects ?? []).join(', ') || '—')}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Make the list rows tappable + show a not-ready dot** — in `RunnerStatus.tsx`, accept an `onSelect` prop and render each row as a button; add a small not-ready marker. Change the signature to `export function RunnerStatus({ runners, onSelect }: { runners: RunnerOut[]; onSelect?: (r: RunnerOut) => void })`, wrap each row so it calls `onSelect?.(r)` on click (keep the existing testid `runner-${r.name}`), and after the name add:

```tsx
        {!r.ready && (
          <span data-testid={`runner-notready-${r.name}`} className="shrink-0 rounded bg-destructive/15 px-1 text-[10px] text-destructive">
            not ready
          </span>
        )}
```

- [ ] **Step 3: Wire the detail into the page** — in `SupervisorPage.tsx`, add local state `const [selectedRunner, setSelectedRunner] = useState<RunnerOut | null>(null)` and in the **Agents** tab render the detail when one is selected, else the list:

```tsx
          {selectedRunner ? (
            <RunnerDetail runner={selectedRunner} onBack={() => setSelectedRunner(null)} />
          ) : errs.runners ? (
            <BandError message={errs.runners} />
          ) : renderRunners === null ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <RunnerStatus runners={renderRunners} onSelect={setSelectedRunner} />
          )}
```
Import `RunnerDetail` and `type RunnerOut`. (When a fresh fetch lands, `selectedRunner` holds a snapshot — acceptable for v1; the detail is re-opened from the live list.)

- [ ] **Step 4: Build**

Run: `cd frontend && npm run build 2>&1 | grep -E "error|✓ built"; cd ..`
Expected: `✓ built`.

- [ ] **Step 5: Seed a not-ready runner** — in `frontend/e2e/seed.py`, on the existing seeded runner (or a second one), set `ready=False, ready_note="emdash CDP unreachable"` so the indicator + detail have something to assert. If the seed's runner is created inline, add `ready=False, ready_note="emdash CDP unreachable"` to its `Runner.objects.create(...)`.

- [ ] **Step 6: Playwright** — in `frontend/e2e/supervisor.spec.ts`, add inside the describe:

```typescript
  test('a runner shows not-ready and opens a detail view with the reason', async ({ page }) => {
    await page.goto('/supervisor?tab=agents')
    // the seeded runner is not-ready → the list shows the marker
    const notReady = page.locator('[data-testid^="runner-notready-"]').first()
    await expect(notReady).toBeVisible()
    // tap the runner row → detail view with the reason
    await page.locator('[data-testid^="runner-"]').filter({ hasText: /not ready/ }).first().click()
    await expect(page.getByTestId('runner-detail-back')).toBeVisible()
    await expect(page.getByTestId('runner-detail-ready')).toHaveText('not ready')
    await expect(page.getByTestId('runner-detail-why')).toContainText('emdash CDP unreachable')
  })
```
(If the row-click selector is fiddly against the button wrapping, target the specific `getByTestId(\`runner-<name>\`)` for the seeded runner's name instead.)

- [ ] **Step 7: Run the frontend gates**

Run: `cd frontend && npm run test 2>&1 | grep "Tests " && npx playwright test -g "not-ready|supervisor" 2>&1 | tail -4; cd ..`
Expected: vitest green; the new + existing supervisor tests pass on desktop + mobile.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/harness.ts frontend/src/components/supervisor/RunnerDetail.tsx frontend/src/components/supervisor/RunnerStatus.tsx frontend/src/pages/SupervisorPage.tsx frontend/e2e/seed.py frontend/e2e/supervisor.spec.ts
git commit -m "feat(supervisor): tap a runner to see on/ready/why; not-ready indicator in the list"
```

---

## Final verification (before PR)

- [ ] `mv .env /tmp/.env.aside; uv run pytest -q; mv /tmp/.env.aside .env` — full backend suite green (needs daphne locally: `uv sync --extra dev`).
- [ ] `cd packages/canopy_runner && uv run --with pytest pytest -q; cd ..` — runner suite green.
- [ ] `cd frontend && npm run build && npm run test && npx playwright test; cd ..` — all green.
- [ ] PR, CI green, merge, deploy (`run_migrations` auto — Task 1 adds a migration), verify the live image tag == the merge SHA.
- [ ] After deploy: the runner half is inert until the laptop daemon updates (git pull + `launchctl kickstart -k gui/$(id -u)/com.canopy.runner`), same as before — then `ready` starts flowing.

## Self-review notes (coverage against the spec, Phase A)

- Runner reports `ready` (proactive `cdp_healthy` ∧ reactive marker) + `ready_note` → Task 2. canopy-web persists + exposes → Task 1. Mobile runner-detail + not-ready indicator → Task 3.
- No routing change (spec: "Phase A changes no routing") — `claim_next_turn` untouched.
- `ready` defaults True (fail-open for un-upgraded runners) — Task 1 model + the omit-defaults test.
- The reactive marker persists on disk so `--drain-one` (one-shot) sees it — Task 2 `test_marker_survives_process_restart`.
- Phase B (the cascade + routing + config UI) is a separate plan, not built here.
