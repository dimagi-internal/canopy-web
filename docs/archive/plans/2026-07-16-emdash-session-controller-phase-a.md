# emdash session controller — Phase A (list + continue) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the phone, see your open emdash sessions and drop a prompt into a specific one, which lands in that exact emdash task on the laptop.

**Architecture:** The runner (the only thing that sees both the laptop's emdash and the cloud) reads un-archived tasks from `emdash4.db` and POSTs them to canopy-web each poll tick. canopy-web stores them in a new `EmdashSession` model (wholesale-replaced per runner) and, for each, upserts a `SessionLink` so "continue" rides the existing reuse path. The phone lists the sessions and dispatches a repo turn carrying the session's `emdash:{name}` thread_key; `resolve_session` returns reuse and the runner `open_and_send`s the prompt into that task — no `execute.py` change.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2 + Postgres; stdlib-only `canopy_runner` (Python) + sqlite3; React 19 + Vite + Tailwind + canopy-ui; pytest; vitest; Playwright.

**Spec:** `docs/superpowers/specs/2026-07-16-emdash-session-controller-design.md`.

## Global Constraints

- **The emdash read is READ-ONLY and NOT behind the write-vet pin.** `emdash.task_state` already establishes this: a read cannot corrupt emdash, so it stays correct across emdash upgrades. A missing/renamed column or unreadable DB degrades to `[]`, never raises — the runner loop must survive anything.
- **`claim_next_turn` derives tenancy from `runner.paired_by`, not `Runner.workspace`** (the #227 outage). Any new tenant-scoped query follows the same rule.
- **`capabilities` is a routing hint, never a security boundary** — the workspace gates.
- **`Workspace`'s primary key is its slug** — `runner.workspace_id` / `session.workspace_id` are strings.
- **`SESSION_SAVE_EVERY_REQUEST = True`** — any view with `except IntegrityError` needs its own `transaction.atomic()` savepoint.
- **Design tokens only** — `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, status tokens. No raw palette literals.
- **Never hand-edit `frontend/src/api/generated.ts`** — regenerate it (Task 3 shows the exact offline command).
- **Tests:** pytest, `pytestmark = pytest.mark.django_db`, fixtures inline per file. No `tests/conftest.py`.
- **Verify like CI:** `uv run pytest` with `.env` moved aside; `canopy_runner` suite via `uv run --with pytest pytest` in its package dir; `npm run build` + `npm run test`; `npx playwright test` locally (CI does not run Playwright).
- **Reported sessions default to the `dimagi` workspace** — a first-class `EmdashSession.workspace` FK whose value defaults to dimagi, consistent with repo dispatch.

## File Structure

- `apps/harness/models.py` — add `EmdashSession` (modify).
- `apps/harness/migrations/00NN_emdashsession.py` — new migration (created by makemigrations).
- `apps/harness/services.py` — add `replace_reported_sessions()` + `list_visible_sessions()` (modify).
- `apps/harness/schemas.py` — add `ReportedSessionIn`, `ReportSessionsIn`, `EmdashSessionOut` (modify).
- `apps/harness/api.py` — add `POST /runners/{id}/sessions` + `GET /sessions` (modify).
- `tests/test_harness_emdash_sessions.py` — backend tests (create).
- `packages/canopy_runner/canopy_runner/emdash.py` — add `list_open_sessions()` (modify).
- `packages/canopy_runner/canopy_runner/client.py` — add `report_sessions()` (modify).
- `packages/canopy_runner/canopy_runner/main.py` — call it each CDP tick (modify).
- `packages/canopy_runner/tests/test_emdash_sessions.py` — runner tests (create).
- `frontend/src/api/harness.ts` — add `listOpenSessions()` + `EmdashSessionOut` (modify).
- `frontend/src/components/supervisor/OpenSessions.tsx` — the list + per-row continue (create).
- `frontend/src/pages/SupervisorPage.tsx` — mount the section (modify).
- `frontend/e2e/seed.py` — seed an EmdashSession (modify).
- `frontend/e2e/supervisor.spec.ts` — Playwright (modify).

---

## Task 1: `EmdashSession` model

**Files:**
- Modify: `apps/harness/models.py` (add the class after `SessionLink`)
- Create: `apps/harness/migrations/00NN_emdashsession.py` (via makemigrations)
- Test: `tests/test_harness_emdash_sessions.py`

**Interfaces:**
- Produces: `EmdashSession` with fields `runner (FK)`, `workspace (FK, PROTECT)`, `emdash_task (str)`, `project (str)`, `status (str)`, `last_interacted_at (datetime|null)`, `recent_messages (JSON, default list)`, `reported_at (auto_now)`; `unique_together = (runner, emdash_task)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_harness_emdash_sessions.py`:

```python
"""The emdash session controller — reported sessions + the list the phone reads."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.utils import timezone

from apps.harness.models import EmdashSession, Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, ws):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def test_a_runner_cannot_report_the_same_task_twice():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    EmdashSession.objects.create(runner=runner, workspace=ws, emdash_task="cloud-runner", project="canopy-web")
    with pytest.raises(IntegrityError):
        EmdashSession.objects.create(runner=runner, workspace=ws, emdash_task="cloud-runner", project="canopy-web")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py -q; mv /tmp/.env.aside .env`
Expected: FAIL — `ImportError: cannot import name 'EmdashSession'`.

- [ ] **Step 3: Add the model**

In `apps/harness/models.py`, after the `SessionLink` class, add:

```python
class EmdashSession(models.Model):
    """A snapshot of one OPEN emdash session, reported by the runner that can see it.

    Ephemeral by design: the runner replaces its whole set every report tick, so this
    is "what emdash shows right now on that laptop", not a durable record. The durable
    half of continuing a session lives in SessionLink (the report upserts one too); this
    model is purely the phone's read model (list + recent messages).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="emdash_sessions")
    # Tenant, first-class (defaults to dimagi at the reporting edge). PROTECT mirrors
    # the project-turn workspace: a tenant with live sessions should not vanish under them.
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="emdash_sessions"
    )
    emdash_task = models.CharField(max_length=200, help_text="The emdash task NAME — what open_and_send targets.")
    project = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=40, blank=True, default="")
    last_interacted_at = models.DateTimeField(null=True, blank=True)
    recent_messages = models.JSONField(default=list, blank=True)  # Phase B fills this; [] in Phase A
    reported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_interacted_at"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "emdash_task"], name="emdashsession_unique_per_runner_task"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"emdash-session:{self.runner_id}:{self.emdash_task}"
```

Confirm `uuid` and `models` are already imported at the top of the file (they are — `Turn`/`SessionLink` use both).

- [ ] **Step 4: Make the migration**

Run: `uv run python manage.py makemigrations harness`
Expected: creates `apps/harness/migrations/00NN_emdashsession.py` adding the model + constraint.

- [ ] **Step 5: Run the test to verify it passes**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py -q; mv /tmp/.env.aside .env`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/00*_emdashsession.py tests/test_harness_emdash_sessions.py
git commit -m "feat(harness): EmdashSession — the phone's read model for open emdash sessions"
```

---

## Task 2: report service + `POST /runners/{id}/sessions`

**Files:**
- Modify: `apps/harness/services.py` (add `replace_reported_sessions`)
- Modify: `apps/harness/schemas.py` (add `ReportedSessionIn`, `ReportSessionsIn`)
- Modify: `apps/harness/api.py` (add the route)
- Test: `tests/test_harness_emdash_sessions.py` (append)

**Interfaces:**
- Consumes: `EmdashSession` (Task 1); `services.record_session(agent, thread_key, *, runner, project, workspace, emdash_task_id)` (existing).
- Produces: `replace_reported_sessions(runner, workspace, sessions: list[ReportedSessionIn]) -> int` — wholesale-replaces this runner's `EmdashSession` rows AND upserts a `SessionLink` per session with `thread_key=f"emdash:{task}"`. Route `POST /api/harness/runners/{runner_id}/sessions` returns `{count: int}`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_harness_emdash_sessions.py`)

```python
def _report(client, runner_id, sessions):
    return client.post(
        f"/api/harness/runners/{runner_id}/sessions",
        {"sessions": sessions},
        content_type="application/json",
    )


def test_report_is_wholesale_and_upserts_a_sessionlink_for_continue():
    from django.test import Client
    from apps.harness.models import SessionLink

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    r1 = _report(c, runner.id, [
        {"emdash_task": "cloud-runner", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T15:52:00Z"},
        {"emdash_task": "ddd", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T12:41:00Z"},
    ])
    assert r1.status_code == 200, r1.content
    assert EmdashSession.objects.filter(runner=runner).count() == 2
    # The continue substrate: a SessionLink per session, keyed emdash:{task}.
    link = SessionLink.objects.get(project="canopy-web", thread_key="emdash:cloud-runner")
    assert link.live_emdash_task_id == "cloud-runner"
    assert link.live_runner_id == runner.id
    assert link.workspace_id == "dimagi"

    # A re-report with one session gone removes it (wholesale, not merge).
    r2 = _report(c, runner.id, [
        {"emdash_task": "cloud-runner", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T15:59:00Z"},
    ])
    assert r2.status_code == 200
    tasks = set(EmdashSession.objects.filter(runner=runner).values_list("emdash_task", flat=True))
    assert tasks == {"cloud-runner"}


def test_a_non_owner_cannot_report_for_another_users_runner():
    from django.test import Client

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    c = Client()
    c.force_login(mallory)

    resp = _report(c, runner.id, [{"emdash_task": "x", "project": "canopy-web"}])
    assert resp.status_code == 404
    assert EmdashSession.objects.filter(runner=runner).count() == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py -q; mv /tmp/.env.aside .env`
Expected: FAIL — 404 route not found (the endpoint does not exist yet).

- [ ] **Step 3: Add the schemas**

In `apps/harness/schemas.py`, after `RecordSessionIn`, add:

```python
class ReportedSessionIn(Schema):
    emdash_task: str  # the emdash task NAME
    project: str = ""
    status: str = ""
    last_interacted_at: dt.datetime | None = None
    recent_messages: list = []  # Phase B populates this; ignored/empty in Phase A


class ReportSessionsIn(Schema):
    sessions: list[ReportedSessionIn] = []
```

(`dt` is already imported at the top of `schemas.py`.)

- [ ] **Step 4: Add the service** (in `apps/harness/services.py`, after `record_session`)

```python
@transaction.atomic
def replace_reported_sessions(runner: Runner, workspace, sessions: list) -> int:
    """Wholesale-replace this runner's reported EmdashSessions, and upsert a
    SessionLink per session so `continue` rides the existing reuse path.

    Wholesale: a session that vanished from emdash simply stops being reported and
    disappears here. The SessionLinks are NOT deleted on drop — a durable link that
    revives when the session reappears is harmless, and deleting them would fight the
    reuse machinery; a stale link only ever resolves to reuse if its live hint still
    matches, which the next real report refreshes.
    """
    from .models import EmdashSession

    EmdashSession.objects.filter(runner=runner).delete()
    EmdashSession.objects.bulk_create([
        EmdashSession(
            runner=runner, workspace=workspace, emdash_task=s.emdash_task,
            project=s.project, status=s.status,
            last_interacted_at=_aware(s.last_interacted_at),
            recent_messages=list(s.recent_messages or []),
        )
        for s in sessions
    ])
    for s in sessions:
        if s.project:
            record_session(
                None, f"emdash:{s.emdash_task}", runner=runner, project=s.project,
                workspace=workspace, emdash_task_id=s.emdash_task,
            )
    return len(sessions)
```

- [ ] **Step 5: Add the route** (in `apps/harness/api.py`, after `record_session`)

```python
@router.post("/runners/{runner_id}/sessions", response=CountOut)
def report_sessions(request: HttpRequest, runner_id: uuid.UUID, payload: ReportSessionsIn):
    """The runner reports the open emdash sessions it can see. Wholesale per runner.
    Owner-gated via _runner_or_404 (404, not 403). Sessions are tenant-owned; they
    default to the runner's workspace (dimagi in practice), which the pairer is a
    member of by construction."""
    runner = _runner_or_404(request, runner_id)
    ws = runner.workspace
    if ws is None:
        raise HttpError(404, "runner has no workspace")
    count = services.replace_reported_sessions(runner, ws, payload.sessions)
    return CountOut(count=count)
```

Add the imports: `ReportSessionsIn` to the `from .schemas import (...)` block, and confirm `CountOut` is already imported there (it is — `replace_skills`/other routes use it; if not, add it).

- [ ] **Step 6: Run to verify it passes**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py -q; mv /tmp/.env.aside .env`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 7: Commit**

```bash
git add apps/harness/services.py apps/harness/schemas.py apps/harness/api.py tests/test_harness_emdash_sessions.py
git commit -m "feat(harness): runners report open emdash sessions (+ a SessionLink per session for continue)"
```

---

## Task 3: `GET /sessions` list + generated types

**Files:**
- Modify: `apps/harness/services.py` (add `list_visible_sessions`)
- Modify: `apps/harness/schemas.py` (add `EmdashSessionOut`)
- Modify: `apps/harness/api.py` (add the route)
- Modify: `frontend/src/api/generated.ts` (regenerate — do not hand-edit)
- Test: `tests/test_harness_emdash_sessions.py` (append)

**Interfaces:**
- Produces: `list_visible_sessions(user) -> list[EmdashSession]` — the caller's-workspaces sessions whose runner is live, newest-first. Route `GET /api/harness/sessions` → `list[EmdashSessionOut]` with fields `id, emdash_task, project, status, last_interacted_at, recent_messages, workspace, runner_name`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_list_is_tenant_scoped_and_hides_offline_runners():
    from datetime import timedelta
    from django.test import Client

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    live = _runner(jj, ws)
    EmdashSession.objects.create(runner=live, workspace=ws, emdash_task="cloud-runner",
                                 project="canopy-web", status="in_progress")

    # An offline runner's session is hidden (not deleted).
    stale = Runner.objects.create(
        name="old-mbp", kind=Runner.EMDASH, host="old", paired_by=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now() - timedelta(hours=2),
    )
    EmdashSession.objects.create(runner=stale, workspace=ws, emdash_task="ghost", project="canopy-web")

    c = Client()
    c.force_login(jj)
    rows = c.get("/api/harness/sessions").json()
    tasks = {r["emdash_task"] for r in rows}
    assert tasks == {"cloud-runner"}  # ghost hidden: its runner is not live

    # A non-member sees nothing.
    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    mc = Client()
    mc.force_login(mallory)
    assert mc.get("/api/harness/sessions").json() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py::test_list_is_tenant_scoped_and_hides_offline_runners -q; mv /tmp/.env.aside .env`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Add the schema** (`apps/harness/schemas.py`)

```python
class EmdashSessionOut(Schema):
    id: uuid.UUID
    emdash_task: str
    project: str
    status: str
    last_interacted_at: dt.datetime | None
    recent_messages: list
    workspace: str
    runner_name: str

    @staticmethod
    def resolve_workspace(obj) -> str:
        return obj.workspace_id

    @staticmethod
    def resolve_runner_name(obj) -> str:
        return obj.runner.name
```

(`uuid` is already imported in `schemas.py`.)

- [ ] **Step 4: Add the service** (`apps/harness/services.py`)

```python
def list_visible_sessions(user) -> list:
    """Open sessions in the caller's workspaces whose runner is LIVE. Runner liveness
    (not deletion) is what suppresses a briefly-offline runner's stale rows — see
    Runner.live_status. Newest-first."""
    from .models import EmdashSession

    ws_slugs = wsvc.user_workspace_slugs(user)
    rows = (
        EmdashSession.objects.filter(workspace_id__in=ws_slugs)
        .select_related("runner", "workspace")
        .order_by("-last_interacted_at")
    )
    return [s for s in rows if s.runner.live_status == Runner.ONLINE]
```

Confirm `wsvc` is imported in `services.py` (`from apps.workspaces import services as wsvc`); add it if missing.

- [ ] **Step 5: Add the route** (`apps/harness/api.py`, near `GET /turns/`)

```python
@router.get("/sessions", response=list[EmdashSessionOut])
def list_sessions(request: HttpRequest):
    """Open emdash sessions the caller can see — across their workspaces, live runners
    only, newest-first. Drives the phone's Open Sessions list."""
    return services.list_visible_sessions(request.user)
```

Add `EmdashSessionOut` to the schema import block.

- [ ] **Step 6: Run to verify it passes**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_harness_emdash_sessions.py -q; mv /tmp/.env.aside .env`
Expected: PASS (all).

- [ ] **Step 7: Regenerate the OpenAPI types** (offline, like CI — never hand-edit)

Run:
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
Verify: `grep -c EmdashSessionOut frontend/src/api/generated.ts` returns ≥1.

- [ ] **Step 8: Commit**

```bash
git add apps/harness/services.py apps/harness/schemas.py apps/harness/api.py frontend/src/api/generated.ts tests/test_harness_emdash_sessions.py
git commit -m "feat(harness): GET /sessions — the phone's tenant-scoped open-sessions list"
```

---

## Task 4: runner reads emdash + reports each tick

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/emdash.py` (add `list_open_sessions`)
- Modify: `packages/canopy_runner/canopy_runner/client.py` (add `report_sessions`)
- Modify: `packages/canopy_runner/canopy_runner/main.py` (call it in `_run_once_cdp`)
- Test: `packages/canopy_runner/tests/test_emdash_sessions.py`

**Interfaces:**
- Consumes: `POST /api/harness/runners/{id}/sessions` (Task 2).
- Produces: `emdash.list_open_sessions(db_path: str, limit: int = 30) -> list[dict]` returning `{"emdash_task", "project", "status", "last_interacted_at"}` newest-first, `[]` on any read failure; `Client.report_sessions(runner_id, sessions) -> None`.

- [ ] **Step 1: Write the failing test**

Create `packages/canopy_runner/tests/test_emdash_sessions.py`:

```python
import sqlite3
from canopy_runner import emdash


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT, name TEXT, path TEXT);
        CREATE TABLE tasks (id TEXT, project_id TEXT, name TEXT, status TEXT,
                            archived_at TEXT, last_interacted_at TEXT);
        INSERT INTO projects VALUES ('p1','canopy-web','/x/canopy-web');
        INSERT INTO tasks VALUES ('t1','p1','cloud-runner','in_progress',NULL,'2026-07-16T15:52:00');
        INSERT INTO tasks VALUES ('t2','p1','ddd','in_progress',NULL,'2026-07-16T12:41:00');
        INSERT INTO tasks VALUES ('t3','p1','old','done','2026-07-15T00:00:00','2026-07-15T00:00:00');
        """
    )
    conn.commit()
    conn.close()


def test_lists_unarchived_tasks_newest_first(tmp_path):
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    out = emdash.list_open_sessions(str(db))
    assert [s["emdash_task"] for s in out] == ["cloud-runner", "ddd"]  # 'old' archived → excluded
    assert out[0]["project"] == "canopy-web"
    assert out[0]["status"] == "in_progress"


def test_missing_db_returns_empty_not_raises(tmp_path):
    assert emdash.list_open_sessions(str(tmp_path / "nope.db")) == []


def test_a_broken_schema_returns_empty_not_raises(tmp_path):
    db = tmp_path / "bad.db"
    sqlite3.connect(str(db)).execute("CREATE TABLE tasks (id TEXT)")  # missing columns
    assert emdash.list_open_sessions(str(db)) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd packages/canopy_runner && uv run --with pytest pytest tests/test_emdash_sessions.py -q; cd ../..`
Expected: FAIL — `AttributeError: module 'canopy_runner.emdash' has no attribute 'list_open_sessions'`.

- [ ] **Step 3: Implement `list_open_sessions`** (in `emdash.py`, near `task_state`)

```python
def list_open_sessions(db_path: str, limit: int = 30) -> list[dict]:
    """READ-ONLY: the un-archived emdash tasks, newest-first, capped. Returns
    [{emdash_task, project, status, last_interacted_at}]. Like task_state this is a
    pure read — NOT behind the write-vet pin — and it must NEVER raise: a missing DB,
    a renamed column, or an emdash schema change degrades to [] so the runner loop
    survives. The task NAME is the identity open_and_send targets; project is joined
    from `projects` for display + the continue turn's target."""
    if not Path(db_path).exists():
        return []
    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT t.name AS emdash_task,
                       COALESCE(p.name, '') AS project,
                       COALESCE(t.status, '') AS status,
                       t.last_interacted_at AS last_interacted_at
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.archived_at IS NULL
                ORDER BY t.last_interacted_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
```

(`Path`, `sqlite3`, and `_db` are already in `emdash.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd packages/canopy_runner && uv run --with pytest pytest tests/test_emdash_sessions.py -q; cd ../..`
Expected: PASS (3 passed).

- [ ] **Step 5: Add the client method** (`packages/canopy_runner/canopy_runner/client.py`, after `record_session`)

```python
def report_sessions(self, runner_id: str, sessions: list[dict]) -> None:
    """Report the open emdash sessions this runner can see (wholesale)."""
    self._call("POST", f"/runners/{runner_id}/sessions", {"sessions": sessions})
```

- [ ] **Step 6: Wire it into the CDP tick** (`main.py`, inside `_run_once_cdp`, right after the `client.heartbeat(...)` line)

```python
    # Report the open emdash sessions the phone can continue. Best-effort: a read or
    # POST failure must never stop the tick from claiming work.
    try:
        client.report_sessions(cfg.runner_id, emdash.list_open_sessions(cfg.emdash_db))
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)
```

Confirm `emdash` is imported in `main.py` (it is — `run_once` uses `emdash.check_schema`).

- [ ] **Step 7: Run the whole runner suite**

Run: `cd packages/canopy_runner && uv run --with pytest pytest -q; cd ../..`
Expected: PASS (existing + 3 new).

- [ ] **Step 8: Commit**

```bash
git add packages/canopy_runner/canopy_runner/emdash.py packages/canopy_runner/canopy_runner/client.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/tests/test_emdash_sessions.py
git commit -m "feat(runner): read open emdash sessions and report them each tick"
```

---

## Task 5: the phone's Open Sessions list + continue

**Files:**
- Modify: `frontend/src/api/harness.ts` (add `listOpenSessions` + `EmdashSessionOut`)
- Create: `frontend/src/components/supervisor/OpenSessions.tsx`
- Modify: `frontend/src/pages/SupervisorPage.tsx` (mount it)
- Modify: `frontend/e2e/seed.py` (seed one session)
- Modify: `frontend/e2e/supervisor.spec.ts` (Playwright)

**Interfaces:**
- Consumes: `GET /api/harness/sessions` (Task 3); `enqueueTurn({project, workspace, prompt, threadKey})` (existing); `WORKSPACE_HEADER` routing (existing).
- Produces: `listOpenSessions(): Promise<EmdashSessionOut[]>`; `<OpenSessions />` (self-fetching section).

- [ ] **Step 1: Add the API client** (`frontend/src/api/harness.ts`)

```typescript
export type EmdashSessionOut = components['schemas']['EmdashSessionOut']

export async function listOpenSessions(): Promise<EmdashSessionOut[]> {
  const res = await apiV2.GET('/api/harness/sessions')
  return Array.from(unwrap(res, 'listOpenSessions'))
}
```

- [ ] **Step 2: Write the component** — create `frontend/src/components/supervisor/OpenSessions.tsx`:

```tsx
import { useEffect, useState, type JSX } from 'react'
import { listOpenSessions, enqueueTurn, type EmdashSessionOut } from '@/api/harness'

// The open emdash sessions the runner reported — see them, and drop a prompt into a
// specific one. Continue dispatches a repo turn carrying the session's emdash:{task}
// thread_key; the runner resolves that to the SessionLink the report upserted and
// open_and_sends into that exact task. No new send path.

function SessionRow({ session }: { session: EmdashSessionOut }): JSX.Element {
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<'ok' | string | null>(null)

  async function send(): Promise<void> {
    if (busy || prompt.trim() === '') return
    setBusy(true)
    setSent(null)
    try {
      await enqueueTurn({
        project: session.project,
        workspace: session.workspace,
        prompt: prompt.trim(),
        threadKey: `emdash:${session.emdash_task}`,
      })
      setSent('ok')
      setPrompt('')
    } catch (e) {
      setSent(e instanceof Error ? e.message : 'Failed to send')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3" data-testid={`session-${session.emdash_task}`}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-foreground">
          {session.project} · {session.emdash_task}
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{session.status}</span>
      </div>
      <div className="mt-2 flex gap-2">
        <input
          data-testid={`session-input-${session.emdash_task}`}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void send() }}
          placeholder="Continue this session…"
          className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground"
        />
        <button
          type="button"
          data-testid={`session-send-${session.emdash_task}`}
          onClick={() => void send()}
          disabled={busy || prompt.trim() === ''}
          className="rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {busy ? 'Sending…' : 'Continue'}
        </button>
      </div>
      {sent === 'ok' && (
        <p className="mt-1 text-[12px] text-success" data-testid={`session-sent-${session.emdash_task}`}>
          Sent to {session.emdash_task}.
        </p>
      )}
      {sent && sent !== 'ok' && <p className="mt-1 text-[12px] text-destructive">{sent}</p>}
    </div>
  )
}

export function OpenSessions(): JSX.Element {
  const [sessions, setSessions] = useState<EmdashSessionOut[] | null>(null)
  useEffect(() => {
    let cancelled = false
    listOpenSessions()
      .then((s) => { if (!cancelled) setSessions(s) })
      .catch(() => { if (!cancelled) setSessions([]) })
    return () => { cancelled = true }
  }, [])

  if (sessions === null) return <p className="text-[12px] text-muted-foreground">Loading sessions…</p>
  if (sessions.length === 0) {
    return <p className="text-[12px] text-muted-foreground" data-testid="sessions-empty">No open sessions.</p>
  }
  return (
    <div className="flex flex-col gap-2" data-testid="open-sessions">
      {sessions.map((s) => <SessionRow key={`${s.emdash_task}`} session={s} />)}
    </div>
  )
}
```

- [ ] **Step 3: Mount it in the page** (`frontend/src/pages/SupervisorPage.tsx`)

Add the import near the other supervisor imports:
```tsx
import { OpenSessions } from '@/components/supervisor/OpenSessions'
```
And add a section right after the `Dispatch` section's closing `</section>`:
```tsx
      <section>
        <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Open sessions
        </h2>
        <OpenSessions />
      </section>
```

- [ ] **Step 4: Build to typecheck**

Run: `cd frontend && npm run build 2>&1 | grep -E "error|✓ built"; cd ..`
Expected: `✓ built` with no errors.

- [ ] **Step 5: Seed a session for e2e** (`frontend/e2e/seed.py`, after the AgentSkill block, using the existing `ws`/`a` and a runner)

```python
from apps.harness.models import Runner, EmdashSession
from django.utils import timezone as _tz
_runner = Runner.objects.create(
    name="e2e-mbp", kind=Runner.EMDASH, host="e2e-host", paired_by=user, workspace=ws,
    status=Runner.ONLINE, last_heartbeat_at=_tz.now(), capabilities={"projects": ["canopy-web"]},
)
EmdashSession.objects.create(
    runner=_runner, workspace=ws, emdash_task="cloud-runner", project="canopy-web",
    status="in_progress", last_interacted_at=_tz.now(),
)
```

- [ ] **Step 6: Write the Playwright test** (`frontend/e2e/supervisor.spec.ts`, inside the `describe`)

```typescript
  test('open sessions list and continue dispatches into that exact task', async ({ page }) => {
    await page.goto('/supervisor')
    await expect(page.getByTestId('open-sessions')).toBeVisible()
    await expect(page.getByTestId('session-cloud-runner')).toBeVisible()

    let posted: Record<string, unknown> | null = null
    let url: string | null = null
    await page.route('**/api/w/*/harness/turns/', async (route) => {
      url = route.request().url()
      posted = route.request().postDataJSON()
      await route.fulfill({
        status: 201, contentType: 'application/json',
        body: JSON.stringify({ id: 't-9', agent_slug: null, project: 'canopy-web', target: 'canopy-web', status: 'queued' }),
      })
    })

    await page.getByTestId('session-input-cloud-runner').fill('rerun the failing test')
    await page.getByTestId('session-send-cloud-runner').click()

    await expect(page.getByTestId('session-sent-cloud-runner')).toBeVisible()
    expect(url).toContain('/api/w/dimagi/harness/turns/')  // tenant-pinned
    expect(posted).toMatchObject({ project: 'canopy-web', prompt: 'rerun the failing test' })
    expect((posted as { origin_ref?: { thread_key?: string } }).origin_ref?.thread_key).toBe('emdash:cloud-runner')
  })
```

- [ ] **Step 7: Run the frontend gates**

Run: `cd frontend && npm run test 2>&1 | grep "Tests " && npx playwright test -g "open sessions" 2>&1 | tail -3; cd ..`
Expected: vitest passes; the new Playwright test passes on desktop + mobile.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/harness.ts frontend/src/components/supervisor/OpenSessions.tsx frontend/src/pages/SupervisorPage.tsx frontend/e2e/seed.py frontend/e2e/supervisor.spec.ts
git commit -m "feat(supervisor): Open Sessions — see open emdash sessions and continue one"
```

---

## Final verification (before PR)

- [ ] Backend, CI-equivalent: `mv .env /tmp/.env.aside; uv run pytest -q; mv /tmp/.env.aside .env` — all pass.
- [ ] Runner: `cd packages/canopy_runner && uv run --with pytest pytest -q; cd ..` — all pass.
- [ ] Frontend: `cd frontend && npm run build && npm run test && npx playwright test; cd ..` — all pass.
- [ ] PR, CI green, merge, deploy (`run_migrations=true` — Task 1 adds a migration), and verify the live ECS image tag == the merge SHA.
- [ ] The runner half is inert until the laptop daemon updates; enabling it is the same operational step as repo dispatch (pull daemon checkout, restart).

## Self-review notes (coverage against the spec)

- Spec A1 (runner reports) → Task 4. A2 (store + serve, wholesale, SessionLink upsert, tenant + runner-live list) → Tasks 1–3. A3 (phone list + continue via reuse) → Task 5.
- `recent_messages` is carried through the model/schema/report as `[]` in Phase A so Phase B is purely additive (the runner starts filling it, no schema change).
- Continue deliberately adds NO `execute.py` path — it reuses `record_session` + the resolve/reuse path, per the spec's key decision.
- Not in Phase A (spec Phase B): transcript reading. Not built here.
