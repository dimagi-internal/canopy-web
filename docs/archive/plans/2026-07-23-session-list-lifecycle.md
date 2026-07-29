# Session List Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the supervisor's append-only Sessions list an end — the runner reports which emdash tasks were archived, and sessions unseen for 3 days drop out of the default view.

**Architecture:** Explicit signals write, staleness derives. Closing a task in emdash (or a manual archive call) writes `Session.status = archived`; the residue is a read-time cutoff on `RunnerBinding.live_seen_at`. No cron, no sweep. Separately, a failed emdash read now raises instead of returning `[]`, so the runner skips the report rather than POSTing an empty list that clears every binding.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2, PostgreSQL, pytest; stdlib-only Python runner (`packages/canopy_runner`) tested with pytest; React 19 + Vite + vitest.

**Spec:** `docs/superpowers/specs/2026-07-23-session-list-lifecycle-design.md`

## Global Constraints

- **Framework/product boundary:** `canopy_sessions`, `harness`, and `realtime` are **framework** tier. They must never import product apps (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`). Enforced by `tests/test_architecture_boundary.py`.
- **`packages/canopy_runner` is stdlib-only.** No third-party imports, ever.
- **Staleness window is exactly 3 days**, defined once as `SESSION_STALE_AFTER = datetime.timedelta(days=3)` in `apps/canopy_sessions/staleness.py`. Never re-literal it — `services.py` re-exports it and the backfill migration imports it.
- **Never add a non-migration module to a `migrations/` package.** Django's loader treats every module there whose name does not start with `_` as a migration and raises `BadMigrationError` if it has no `Migration` class — which breaks every `migrate` call, not just the new one.
- **Nothing is ever deleted.** Archive is reversible; there is no delete route.
- **Backend commands run from the repo root with `uv run`** (e.g. `uv run pytest`). Runner tests run with `uv run pytest packages/canopy_runner/tests`.
- **Any change to `apps/**/schemas.py` or `apps/**/api.py` requires regenerating `frontend/src/api/generated.ts`** (Task 8). The `regen-openapi.yml` workflow fails the PR if it is stale.
- **Design tokens only** in frontend work — `bg-muted`, `text-muted-foreground`, `border-border`, `text-primary`. Never raw palette literals (`stone-*`, `orange-*`, `zinc-*`).

---

### Task 1: Runner — a failed emdash read raises instead of returning `[]`

The highest-value change in the plan, and it stands alone. Today `list_open_sessions` swallows every `sqlite3.Error` and returns `[]`; the 10s heartbeat POSTs that empty list and `replace_reported_sessions` clears **every** binding for the runner. A schema drift therefore blanks the supervisor with nothing in the log.

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/emdash.py` (add exception, change `list_open_sessions`)
- Modify: `packages/canopy_runner/canopy_runner/main.py:232-256` (`_maybe_report_sessions`)
- Test: `packages/canopy_runner/tests/test_emdash_sessions.py`
- Test: `packages/canopy_runner/tests/test_session_report_live.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `canopy_runner.emdash.EmdashReadError` (an `Exception` subclass). `list_open_sessions(db_path: str, limit: int = 30) -> list[dict]` — unchanged signature, now raises `EmdashReadError` on a read failure; still returns `[]` for a missing file.

- [ ] **Step 1: Write the failing test for the raise**

Replace the existing `test_a_broken_schema_returns_empty_not_raises` in `packages/canopy_runner/tests/test_emdash_sessions.py` with this. Also add the new import at the top of the file.

```python
import pytest
```

```python
def test_a_broken_schema_raises_rather_than_looking_empty(tmp_path):
    """A read failure must NOT look like "zero open sessions". Returning [] here made
    the runner POST an empty report, which clears every RunnerBinding server-side —
    a schema drift silently blanked the supervisor."""
    db = tmp_path / "bad.db"
    sqlite3.connect(str(db)).execute("CREATE TABLE tasks (id TEXT)")  # missing columns
    with pytest.raises(emdash.EmdashReadError):
        emdash.list_open_sessions(str(db))


def test_missing_db_still_returns_empty(tmp_path):
    """A MISSING file is "no emdash here", not a failure — that stays fail-soft."""
    assert emdash.list_open_sessions(str(tmp_path / "nope.db")) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest packages/canopy_runner/tests/test_emdash_sessions.py -v`
Expected: FAIL — `AttributeError: module 'canopy_runner.emdash' has no attribute 'EmdashReadError'`

- [ ] **Step 3: Add the exception and make the read raise**

In `packages/canopy_runner/canopy_runner/emdash.py`, add this class immediately after the existing `SchemaCheckError` class:

```python
class EmdashReadError(Exception):
    """The emdash DB exists but could not be READ (locked, corrupt, or a column the
    SQL names has been renamed).

    Distinct from a missing file, which is a legitimate "no emdash here" and stays
    fail-soft. The distinction is load-bearing: the caller must never mistake a read
    failure for "zero open sessions", because reporting an empty list clears every
    RunnerBinding server-side (`replace_reported_sessions`). Swallowing the error is
    what let a schema drift blank the supervisor with nothing in the log."""
```

Then change the `except` clause at the end of `list_open_sessions` from:

```python
    except sqlite3.Error:
        return []
```

to:

```python
    except sqlite3.Error as exc:
        raise EmdashReadError(f"emdash open-session read failed: {exc}") from exc
```

And update that function's docstring — replace the sentence `Like task_state this is a pure read that must NEVER raise: a missing DB, a renamed column, or an emdash schema change degrades to [] so the runner loop survives.` with:

```
    A MISSING db degrades to [] so the runner loop survives ("no emdash here"). A real
    READ failure raises EmdashReadError — the caller must be able to tell "I read zero
    open tasks" from "I could not read", because the two have opposite server-side
    consequences (nothing changes vs every binding is cleared).
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest packages/canopy_runner/tests/test_emdash_sessions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the wider runner suite to catch the other caller of this contract**

Run: `uv run pytest packages/canopy_runner/tests -v`
Expected: PASS. `test_emdash.py::test_read_schema_matches_the_actual_read_sql` asserts `list_open_sessions(db) == []` against a *clean* schema, which still holds. If anything else fails, it is asserting the old swallow-and-return-`[]` behaviour and must be updated to expect the raise.

- [ ] **Step 6: Write the failing test for skipping the report**

Add to `packages/canopy_runner/tests/test_session_report_live.py`:

```python
def test_a_failed_emdash_read_skips_the_report_entirely(tmp_path, monkeypatch):
    """An empty report CLEARS every binding server-side. When we could not read, we
    must say nothing at all rather than assert emptiness."""
    m._tail_readers.clear()
    m._last_session_report = 0.0

    def _boom(_db, *_a, **_k):
        raise m.emdash.EmdashReadError("schema drift")

    monkeypatch.setattr(m.emdash, "list_open_sessions", _boom)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.reports == 0            # nothing posted
    assert c.sessions_seen == []     # and certainly not an empty list
```

Extend the `_Client` stub in that file to record payloads:

```python
class _Client:
    def __init__(self):
        self.reports = 0
        self.sessions_seen = []

    def report_sessions(self, runner_id, sessions):
        self.reports += 1
        self.sessions_seen.append(sessions)
```

- [ ] **Step 7: Run it to verify it fails**

Run: `uv run pytest packages/canopy_runner/tests/test_session_report_live.py -v`
Expected: FAIL — the bare `except Exception` in `_maybe_report_sessions` currently catches `EmdashReadError` and returns, so `c.reports == 0` may already pass, but `sessions_seen` will not exist until the stub is updated. If the assertion passes for the wrong reason, that is fine — Step 8 makes the behaviour explicit and logged.

- [ ] **Step 8: Make the skip explicit and loud**

In `packages/canopy_runner/canopy_runner/main.py`, inside `_maybe_report_sessions`, replace:

```python
    try:
        sessions = emdash.list_open_sessions(cfg.emdash_db)
    except Exception:  # noqa: BLE001
        logger.debug("session list failed (non-fatal)", exc_info=True)
        return
```

with:

```python
    try:
        sessions = emdash.list_open_sessions(cfg.emdash_db)
    except emdash.EmdashReadError:
        # WARNING, not debug: this is the silent-degradation class verify-emdash
        # exists for. Skip the report entirely — an empty one would clear every
        # RunnerBinding server-side, which is the opposite of what we observed.
        logger.warning(
            "emdash session read FAILED — skipping this report so the server keeps the "
            "sessions it already knows. Run `canopy-runner verify-emdash` to check for "
            "schema drift.",
            exc_info=True,
        )
        return
    except Exception:  # noqa: BLE001
        logger.debug("session list failed (non-fatal)", exc_info=True)
        return
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest packages/canopy_runner/tests -v`
Expected: PASS (whole runner suite)

- [ ] **Step 10: Commit**

```bash
git add packages/canopy_runner/canopy_runner/emdash.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/tests/test_emdash_sessions.py packages/canopy_runner/tests/test_session_report_live.py
git commit -m "fix(runner): a failed emdash read must not look like zero sessions

list_open_sessions swallowed every sqlite3.Error and returned [], which the
10s heartbeat POSTed as an empty report — clearing every RunnerBinding. A
schema drift blanked the supervisor with nothing in the log. It now raises
EmdashReadError and the report is skipped (logged at WARNING); a missing DB
file still returns [], because that is 'no emdash here', not a failure."
```

---

### Task 2: Runner — config-driven report cap, 30 → 100

`LIMIT 30` silently truncates. Once Task 5 lands, truncation causes auto-archive, so silent truncation stops being cosmetic. `session_tail_count` (30) stays a **separate** knob — it bounds how many *transcripts* are read, which is the expensive part of a tick.

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/config.py` (add field)
- Modify: `packages/canopy_runner/canopy_runner/main.py` (pass it)
- Test: `packages/canopy_runner/tests/test_config.py`
- Test: `packages/canopy_runner/tests/test_session_report_live.py`

**Interfaces:**
- Consumes: `emdash.list_open_sessions(db_path, limit)` from Task 1.
- Produces: `Config.session_report_limit: int = 100`.

- [ ] **Step 1: Write the failing tests**

Add to `packages/canopy_runner/tests/test_config.py`:

```python
def test_session_report_limit_defaults_to_100():
    """Separate from session_tail_count (30): that bounds expensive TRANSCRIPT reads,
    this bounds how many tasks are reported at all. Truncation here auto-archives."""
    from canopy_runner.config import Config

    cfg = Config(base_url="http://x", token="t", runner_id="r", emdash_db="/db")
    assert cfg.session_report_limit == 100
    assert cfg.session_tail_count == 30
```

Add to `packages/canopy_runner/tests/test_session_report_live.py`:

```python
def test_report_honours_the_configured_limit(tmp_path, monkeypatch):
    m._tail_readers.clear()
    m._last_session_report = 0.0
    seen = {}

    def _list(db, limit=30):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(m.emdash, "list_open_sessions", _list)
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    m._maybe_report_sessions(_Cfg(), _Client(), now_fn=lambda: 100.0)
    assert seen["limit"] == 100
```

Add `session_report_limit = 100` to the `_Cfg` stub class in that file.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/canopy_runner/tests/test_config.py packages/canopy_runner/tests/test_session_report_live.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'session_report_limit'` and `assert 30 == 100`

- [ ] **Step 3: Add the field**

In `packages/canopy_runner/canopy_runner/config.py`, add immediately after `session_report_seconds: int = 10`:

```python
    # How many emdash tasks the report carries. DISTINCT from session_tail_count
    # below (which bounds the expensive transcript reads): a task truncated off THIS
    # limit stops being reported at all, and after SESSION_STALE_AFTER the server
    # auto-archives it. Silent truncation is therefore not cosmetic — keep it well
    # above any realistic open-task count.
    session_report_limit: int = 100
```

- [ ] **Step 4: Pass it at the call site**

In `packages/canopy_runner/canopy_runner/main.py`, in `_maybe_report_sessions`, change:

```python
        sessions = emdash.list_open_sessions(cfg.emdash_db)
```

to:

```python
        sessions = emdash.list_open_sessions(cfg.emdash_db, cfg.session_report_limit)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest packages/canopy_runner/tests -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/canopy_runner/canopy_runner/config.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/tests/test_config.py packages/canopy_runner/tests/test_session_report_live.py
git commit -m "fix(runner): raise the session report cap to 100, config-driven

LIMIT 30 truncated silently. Once staleness auto-archives unreported tasks,
the 31st task would vanish from the supervisor for no visible reason."
```

---

### Task 3: Runner — report which tasks were archived

The closing signal. Without it, a task falling off the report is indistinguishable from a dead runner or a truncated list, so nothing can safely retire a row.

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/emdash.py` (new read)
- Modify: `packages/canopy_runner/canopy_runner/client.py:81-83` (`report_sessions`)
- Modify: `packages/canopy_runner/canopy_runner/main.py` (`_maybe_report_sessions`)
- Test: `packages/canopy_runner/tests/test_emdash_sessions.py`
- Test: `packages/canopy_runner/tests/test_client_report_payload.py` (create)
- Test: `packages/canopy_runner/tests/test_session_report_live.py`

**Interfaces:**
- Consumes: `EmdashReadError` (Task 1), `Config.session_report_limit` (Task 2).
- Produces: `emdash.list_recently_archived_tasks(db_path: str, limit: int = 100) -> list[str]` (task NAMES, newest-archived first). `Client.report_sessions(runner_id: str, sessions: list[dict], archived: list[str] | None = None) -> None`, which POSTs `{"sessions": [...], "archived": [...]}`.

- [ ] **Step 1: Write the failing test for the new read**

Add to `packages/canopy_runner/tests/test_emdash_sessions.py`:

```python
def test_lists_recently_archived_task_names_newest_first(tmp_path):
    """The CLOSING signal: without it the server cannot tell "you archived this" from
    "I lost sight of it", so it can never retire a row."""
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks VALUES ('t5','p1','older','done','2026-07-01T00:00:00',"
        "'2026-07-01T00:00:00','task')"
    )
    # an archived AUTOMATION-RUN must not leak in either — it was never a session
    conn.execute(
        "INSERT INTO tasks VALUES ('t6','p1','auto-gone','done','2026-07-20T00:00:00',"
        "'2026-07-20T00:00:00','automation-run')"
    )
    conn.commit()
    conn.close()

    names = emdash.list_recently_archived_tasks(str(db))
    assert names == ["old", "older"]          # newest-archived first; open tasks absent


def test_archived_list_is_fail_soft_on_a_missing_db_and_loud_on_a_bad_one(tmp_path):
    assert emdash.list_recently_archived_tasks(str(tmp_path / "nope.db")) == []
    bad = tmp_path / "bad.db"
    sqlite3.connect(str(bad)).execute("CREATE TABLE tasks (id TEXT)")
    with pytest.raises(emdash.EmdashReadError):
        emdash.list_recently_archived_tasks(str(bad))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest packages/canopy_runner/tests/test_emdash_sessions.py -v`
Expected: FAIL — `AttributeError: module 'canopy_runner.emdash' has no attribute 'list_recently_archived_tasks'`

- [ ] **Step 3: Implement the read**

Add to `packages/canopy_runner/canopy_runner/emdash.py`, immediately after `list_open_sessions`:

```python
def list_recently_archived_tasks(db_path: str, limit: int = 100) -> list[str]:
    """READ-ONLY: the NAMES of recently-archived emdash tasks, newest-archived first.

    The closing signal. `list_open_sessions` tells the server what IS open; absence
    from it is ambiguous (archived? runner dead? truncated? DB unreadable?), so the
    server cannot retire a session on absence alone. This read makes "you archived
    it" observable, leaving only the genuinely-vanished residue to a staleness rule.

    `type='task'` for the same reason as the open read: an archived automation-run was
    never a session. Same contract as the reads above — missing file returns [], a
    real read failure raises EmdashReadError (the caller omits the field rather than
    asserting "nothing was archived", which would un-archive every closed task).
    """
    if not Path(db_path).exists():
        return []
    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT t.name AS emdash_task
                FROM tasks t
                WHERE t.archived_at IS NOT NULL AND t.type = 'task'
                ORDER BY t.archived_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [r["emdash_task"] for r in rows]
    except sqlite3.Error as exc:
        raise EmdashReadError(f"emdash archived-task read failed: {exc}") from exc
```

`READ_SCHEMA` needs no change — `tasks.archived_at`, `tasks.name`, and `tasks.type` are already listed, so `verify-emdash` already covers this read.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest packages/canopy_runner/tests/test_emdash_sessions.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the client payload**

`test_client.py` drives a real `HTTPServer`, which is more machinery than a payload-shape assertion needs. Create a separate file, `packages/canopy_runner/tests/test_client_report_payload.py`:

```python
"""Wire-shape of the session report. Pinned against the server contract in
apps/harness/schemas.py::ReportSessionsIn."""
from canopy_runner.client import Client


def _client(monkeypatch):
    """A Client whose transport is replaced by a recorder — no socket needed."""
    c = Client(base_url="http://x", token="tok")
    sent = []

    def _call(method, path, body=None):
        sent.append({"method": method, "path": path, "body": body})
        return 200, {}

    monkeypatch.setattr(c, "_call", _call)
    return c, sent


def test_report_sessions_carries_the_archived_list(monkeypatch):
    c, sent = _client(monkeypatch)
    c.report_sessions("r1", [{"emdash_task": "a"}], ["gone", "also-gone"])
    assert sent[-1]["path"] == "/runners/r1/sessions"
    assert sent[-1]["body"] == {
        "sessions": [{"emdash_task": "a"}],
        "archived": ["gone", "also-gone"],
    }


def test_report_sessions_defaults_archived_to_empty(monkeypatch):
    """An empty list, never a missing key — the server must be able to tell 'nothing
    was archived' from an older runner that cannot report it."""
    c, sent = _client(monkeypatch)
    c.report_sessions("r1", [])
    assert sent[-1]["body"] == {"sessions": [], "archived": []}
```

Check `Client.__init__`'s real signature before writing this (it is at the top of `packages/canopy_runner/canopy_runner/client.py`) and match it — if it takes `runner_id` or a different keyword, adjust the constructor call accordingly.

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest packages/canopy_runner/tests/test_client_report_payload.py -v`
Expected: FAIL — `TypeError: report_sessions() takes 3 positional arguments but 4 were given`

- [ ] **Step 7: Widen the client**

In `packages/canopy_runner/canopy_runner/client.py`, replace `report_sessions` with:

```python
    def report_sessions(
        self, runner_id: str, sessions: list[dict], archived: list[str] | None = None
    ) -> None:
        """Report the open emdash sessions this runner can see (wholesale), plus the
        task names it has seen ARCHIVED — the closing signal that lets the server
        retire a session instead of inferring it from absence."""
        self._call(
            "POST",
            f"/runners/{runner_id}/sessions",
            {"sessions": sessions, "archived": archived or []},
        )
```

- [ ] **Step 8: Run it to verify it passes**

Run: `uv run pytest packages/canopy_runner/tests/test_client_report_payload.py -v`
Expected: PASS (2 tests)

- [ ] **Step 9: Write the failing test for wiring it into the report**

Add to `packages/canopy_runner/tests/test_session_report_live.py`. First extend the `_Client` stub to capture the archived arg:

```python
class _Client:
    def __init__(self):
        self.reports = 0
        self.sessions_seen = []
        self.archived_seen = []

    def report_sessions(self, runner_id, sessions, archived=None):
        self.reports += 1
        self.sessions_seen.append(sessions)
        self.archived_seen.append(archived)
```

Then:

```python
def test_report_carries_archived_task_names(tmp_path, monkeypatch):
    m._tail_readers.clear()
    m._last_session_report = 0.0
    monkeypatch.setattr(m.emdash, "list_open_sessions", lambda _db, _l=100: [])
    monkeypatch.setattr(m.emdash, "list_recently_archived_tasks", lambda _db, _l=100: ["gone"])
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.archived_seen[-1] == ["gone"]


def test_a_failed_archived_read_still_reports_open_sessions(tmp_path, monkeypatch):
    """Fail-soft in the other direction: losing the closing signal must not cost us
    the open-session report too."""
    m._tail_readers.clear()
    m._last_session_report = 0.0

    def _boom(_db, _l=100):
        raise m.emdash.EmdashReadError("drift")

    monkeypatch.setattr(m.emdash, "list_open_sessions", lambda _db, _l=100: [])
    monkeypatch.setattr(m.emdash, "list_recently_archived_tasks", _boom)
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.reports == 1
    assert c.archived_seen[-1] == []
```

- [ ] **Step 10: Run it to verify it fails**

Run: `uv run pytest packages/canopy_runner/tests/test_session_report_live.py -v`
Expected: FAIL — `AssertionError: assert None == ['gone']`

- [ ] **Step 11: Wire it in**

In `packages/canopy_runner/canopy_runner/main.py`, in `_maybe_report_sessions`, replace the final try block:

```python
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        client.report_sessions(cfg.runner_id, sessions)
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)
```

with:

```python
    # Read the closing signal only on a tick we're actually going to report on.
    # Fail-soft in the opposite direction to the open-session read: losing the
    # archived list must not cost us the report, so omit the field and carry on.
    try:
        archived = emdash.list_recently_archived_tasks(
            cfg.emdash_db, cfg.session_report_limit
        )
    except emdash.EmdashReadError:
        logger.debug("archived-task read failed (non-fatal, omitting)", exc_info=True)
        archived = []
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        client.report_sessions(cfg.runner_id, sessions, archived)
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)
```

- [ ] **Step 12: Run the whole runner suite**

Run: `uv run pytest packages/canopy_runner/tests -v`
Expected: PASS

- [ ] **Step 13: Commit**

```bash
git add packages/canopy_runner
git commit -m "feat(runner): report archived emdash tasks as a closing signal

Absence from the open-session report is ambiguous — archived, dead runner,
truncated, or unreadable all look identical, so the server could never retire
a row. Report the archived task names explicitly and the ambiguity collapses
to just the genuinely-vanished residue."
```

---

### Task 4: Server — apply the archive signal

**Files:**
- Modify: `apps/harness/schemas.py:118-119` (`ReportSessionsIn`)
- Modify: `apps/harness/services.py:686-770` (`replace_reported_sessions`)
- Modify: `apps/harness/api.py:356-367` (`report_sessions`)
- Test: `tests/test_session_archive_signal.py` (create)

**Interfaces:**
- Consumes: the runner's `{"sessions": [...], "archived": [...]}` payload (Task 3).
- Produces: `replace_reported_sessions(runner, workspace, sessions, archived=None) -> int` — `archived` is a `list[str]` of `session_key`s. Sets `Session.status` to `"archived"` for matching bindings on this runner; clears it back to `"active"` for any session re-reported as open.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_archive_signal.py`:

```python
"""The closing signal: a runner reporting an archived task retires its session row,
and re-opening the task in emdash brings it back."""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


class _Reported:
    """Duck-types ReportedSessionIn — services reads attributes, not dict keys."""

    def __init__(self, task, project="canopy-web"):
        self.emdash_task = task
        self.project = project
        self.status = "in_progress"
        self.last_interacted_at = timezone.now()
        self.recent_messages = []


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    return user, ws, runner


def test_an_archived_task_archives_its_session():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    services.replace_reported_sessions(runner, ws, [_Reported("live")], archived=["ddd"])

    by_key = {b.session_key: b.session for b in RunnerBinding.objects.select_related("session")}
    assert by_key["ddd"].status == Session.ARCHIVED
    assert by_key["live"].status == Session.ACTIVE


def test_reopening_a_task_unarchives_it():
    """The WRITTEN half must be cleared explicitly — unlike the derived staleness
    half, it does not heal itself."""
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    services.replace_reported_sessions(runner, ws, [], archived=["ddd"])
    assert Session.objects.get().status == Session.ARCHIVED

    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    assert Session.objects.get().status == Session.ACTIVE


def test_a_runner_cannot_archive_another_runners_session():
    """session_key is an emdash task NAME and names collide across machines. Scope the
    archive to the reporting runner's own bindings or one laptop retires another's."""
    user, ws, runner_a = _ctx()
    runner_b = Runner.objects.create(
        name="jj-mini", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    services.replace_reported_sessions(runner_a, ws, [_Reported("ddd")])
    services.replace_reported_sessions(runner_b, ws, [_Reported("ddd")])
    assert Session.objects.count() == 2

    services.replace_reported_sessions(runner_b, ws, [], archived=["ddd"])
    statuses = {b.runner_id: b.session.status for b in RunnerBinding.objects.select_related("session")}
    assert statuses[runner_a.id] == Session.ACTIVE      # untouched
    assert statuses[runner_b.id] == Session.ARCHIVED


def test_an_unknown_archived_name_is_ignored():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")], archived=["never-existed", ""])
    assert Session.objects.get().status == Session.ACTIVE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_session_archive_signal.py -v`
Expected: FAIL — `TypeError: replace_reported_sessions() got an unexpected keyword argument 'archived'`

- [ ] **Step 3: Widen the service**

In `apps/harness/services.py`, change the signature:

```python
@transaction.atomic
def replace_reported_sessions(runner: Runner, workspace, sessions: list) -> int:
```

to:

```python
@transaction.atomic
def replace_reported_sessions(
    runner: Runner, workspace, sessions: list, archived: list[str] | None = None
) -> int:
```

Add to that function's docstring, after the first paragraph:

```
    `archived` is the CLOSING signal — emdash task names this runner has seen
    archived. Absence from `sessions` is ambiguous (archived? runner down?
    truncated?), so it can never retire a row on its own; an explicit name here can.
    Scoped to THIS runner's bindings, because a task name is not unique across
    machines and one laptop must never retire another's session.
```

Then insert this block **immediately after** the `for s in deduped:` upsert loop and **before** the existing `RunnerBinding.objects.filter(runner=runner).exclude(...)` clear. Order is load-bearing: the clear sets `runner=None`, after which `runner_binding__runner=runner` no longer matches the rows we need to archive.

```python
    from apps.canopy_sessions.models import Session as _Session

    # Un-archive anything re-reported as open. The DERIVED staleness half of
    # `state=active` recomputes on every read, but this WRITTEN half does not heal
    # itself — without this, a task you reopened in emdash stays archived forever.
    if now_keys:
        _Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=now_keys,
            status=_Session.ARCHIVED,
        ).update(status=_Session.ACTIVE)

    # Apply the closing signal. `now_keys` wins over `archived`: emdash task names are
    # not unique, so an open task must never be retired by an archived namesake.
    closed = [k for k in (archived or []) if k and k not in now_keys]
    if closed:
        _Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=closed,
        ).update(status=_Session.ARCHIVED)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_session_archive_signal.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Accept the field on the wire**

In `apps/harness/schemas.py`, replace `ReportSessionsIn`:

```python
class ReportSessionsIn(Schema):
    sessions: list[ReportedSessionIn] = []
    # emdash task names this runner has seen ARCHIVED. Defaulted so an older runner
    # (which does not send it) keeps working unchanged — it simply never closes a row.
    archived: list[str] = []
```

In `apps/harness/api.py`, in `report_sessions`, change:

```python
    count = services.replace_reported_sessions(runner, ws, payload.sessions)
```

to:

```python
    count = services.replace_reported_sessions(
        runner, ws, payload.sessions, payload.archived
    )
```

- [ ] **Step 6: Write and run the end-to-end route test**

Append to `tests/test_session_archive_signal.py`:

```python
def test_the_route_applies_the_archive_signal(client_and_token=None):
    """End-to-end through the runner-authed route, not just the service."""
    from django.test import Client as DjangoClient

    user, ws, runner = _ctx()
    c = DjangoClient()
    c.force_login(user)
    body = {
        "sessions": [{"emdash_task": "live", "project": "canopy-web"}],
        "archived": ["ddd"],
    }
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    resp = c.post(
        f"/api/harness/runners/{runner.id}/sessions",
        data=body,
        content_type="application/json",
    )
    assert resp.status_code == 200
    by_key = {b.session_key: b.session for b in RunnerBinding.objects.select_related("session")}
    assert by_key["ddd"].status == Session.ARCHIVED
```

Run: `uv run pytest tests/test_session_archive_signal.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Run the harness + session suites for regressions**

Run: `uv run pytest tests/test_harness_emdash_sessions.py tests/test_session_list_unified.py tests/test_session_liveness.py tests/test_harness_session_reuse.py tests/test_session_activity_and_reuse.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add apps/harness/schemas.py apps/harness/services.py apps/harness/api.py tests/test_session_archive_signal.py
git commit -m "feat(sessions): apply the runner's archive signal to session rows

A reported-archived task retires its session; re-reporting it as open brings
it back. Scoped to the reporting runner's own bindings — emdash task names
collide across machines, and one laptop must never retire another's session.
The archive write lands BEFORE the live-pointer clear, which would otherwise
null the runner FK the scoping depends on."
```

---

### Task 5: Server — `state` and `limit` on the sessions list

**Files:**
- Create: `apps/canopy_sessions/staleness.py` (owns the window + cutoff)
- Modify: `apps/canopy_sessions/services.py` (re-export both)
- Modify: `apps/canopy_sessions/api.py:102-125` (`list_sessions`)
- Test: `tests/test_session_list_state.py` (create)

**Interfaces:**
- Consumes: `Session.status` as written by Task 4.
- Produces: `apps.canopy_sessions.staleness.SESSION_STALE_AFTER: datetime.timedelta` and `stale_cutoff(now=None) -> datetime`, both re-exported from `apps.canopy_sessions.services` so existing `services.`-qualified callers and tests keep working. `GET /api/canopy-sessions/?state=active|archived|all&limit=<n>`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_list_state.py`:

```python
"""`state=active` is the union of two rules: not explicitly archived, AND (for runner
sessions) seen by a runner recently. Web sessions are exempt from the second — they
have no runner to be seen by."""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.services import SESSION_STALE_AFTER
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    return user, ws, c, runner


def _runner_session(ws, runner, key, seen_ago: dt.timedelta):
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title=key)
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key=key,
        last_interacted_at=timezone.now() - seen_ago,
        live_seen_at=timezone.now() - seen_ago,
    )
    return s


def test_active_hides_an_explicitly_archived_session():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", dt.timedelta(minutes=1))
    gone = _runner_session(ws, runner, "closed", dt.timedelta(minutes=1))
    Session.objects.filter(pk=gone.pk).update(status=Session.ARCHIVED)

    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert ids == {str(fresh.id)}


def test_active_hides_a_runner_session_unseen_past_the_cutoff():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", SESSION_STALE_AFTER - dt.timedelta(hours=1))
    stale = _runner_session(ws, runner, "vanished", SESSION_STALE_AFTER + dt.timedelta(hours=1))

    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert ids == {str(fresh.id)}, "the just-inside-cutoff session must survive"
    assert str(stale.id) not in ids


def test_a_web_session_never_goes_stale():
    """No runner reports it, so 'unseen by a runner' is meaningless. Only an explicit
    archive ends a web chat."""
    user, ws, c, _runner = _ctx()
    old = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    Session.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - SESSION_STALE_AFTER - dt.timedelta(days=30)
    )
    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert str(old.id) in ids


def test_archived_and_all_return_the_complements():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", dt.timedelta(minutes=1))
    stale = _runner_session(ws, runner, "vanished", SESSION_STALE_AFTER + dt.timedelta(hours=1))

    archived = {r["id"] for r in c.get("/api/canopy-sessions/?state=archived").json()}
    assert archived == {str(stale.id)}

    every = {r["id"] for r in c.get("/api/canopy-sessions/?state=all").json()}
    assert every == {str(fresh.id), str(stale.id)}


def test_an_unknown_state_is_422_not_a_silent_full_list():
    user, ws, c, runner = _ctx()
    assert c.get("/api/canopy-sessions/?state=bogus").status_code == 422


def test_limit_applies_after_the_running_first_sort():
    """A queryset slice would order by -created_at and could cut the running row; the
    limit must bite AFTER the sort that actually decides what matters."""
    user, ws, c, runner = _ctx()
    # Created FIRST and running now. The queryset orders by -created_at, so a
    # queryset-level slice would drop exactly this row — the one the sort floats.
    live = _runner_session(ws, runner, "live", dt.timedelta(seconds=5))
    # Created SECOND (so newest by created_at) but idle — a slice would keep it.
    _idle = _runner_session(ws, runner, "idle", dt.timedelta(hours=2))

    rows = c.get("/api/canopy-sessions/?limit=1").json()
    assert [r["id"] for r in rows] == [str(live.id)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_session_list_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'SESSION_STALE_AFTER' from 'apps.canopy_sessions.services'`

- [ ] **Step 3: Add the staleness leaf module**

Create `apps/canopy_sessions/staleness.py`. It lives apart from `services.py` because the backfill migration (Task 7) must import the window too, and a migration importing the full service layer — which imports models, signals, and the realtime bridge — is how migrations rot. This module imports nothing from the app.

```python
"""The staleness window, defined once.

Separate from services.py so the backfill migration can import it without dragging
in models, signals, and the realtime bridge. Nothing in here imports app code, so it
is safe for a migration to depend on it long after the rest of the app has moved on.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Q
from django.utils import timezone

# How long a runner-discovered session survives with no runner sighting before it
# drops out of `state=active`. NOT "idle for 3 days": live_seen_at is stamped on
# every reported session each tick, and the runner reports every OPEN emdash task
# regardless of activity — so this measures "fell off the report", i.e. archived,
# deleted, truncated, or the runner was down. An open-but-idle task never expires.
SESSION_STALE_AFTER = dt.timedelta(days=3)


def stale_cutoff(now=None):
    """The live_seen_at floor for `state=active`. A binding last seen before this is
    treated as archived — derived, never written, so it un-archives itself the moment
    the task is reported again."""
    return (now or timezone.now()) - SESSION_STALE_AFTER


def unseen_q() -> Q:
    """Runner-origin sessions with no recent sighting. A session with NO binding at
    all counts as unseen, not as fresh. Web sessions never match — no runner reports
    them, so only an explicit archive ends one."""
    return Q(origin="runner") & (
        Q(runner_binding__live_seen_at__lt=stale_cutoff())
        | Q(runner_binding__live_seen_at__isnull=True)
    )
```

Then re-export from `apps/canopy_sessions/services.py`, immediately after the existing `RUNNING_WINDOW = _dt.timedelta(seconds=120)`, so `services.stale_cutoff()` and `from apps.canopy_sessions.services import SESSION_STALE_AFTER` keep working for every existing caller:

```python
# Re-exported so callers keep one import surface; DEFINED in staleness.py, which the
# backfill migration also imports (see the module docstring there).
from .staleness import SESSION_STALE_AFTER, stale_cutoff, unseen_q  # noqa: E402,F401
```

- [ ] **Step 4: Filter the list**

In `apps/canopy_sessions/api.py`, replace the whole body of `list_sessions` (lines 103-125) with:

```python
def list_sessions(request: HttpRequest, state: str = "active", limit: int = 200):
    # The ONE unified list (Plan 4): every session the caller can see in their
    # workspaces — their own web sessions UNION any session that has a
    # RunnerBinding (runner-discovered or live). Deduped, running-first, then
    # newest. Replaces the creator-only list + the harness OpenSessions projection.
    #
    # `state` gives that list an END. Two rules combine into "archived":
    #   - WRITTEN: status == archived (the runner saw the emdash task archived, or
    #     a human called /archive). Durable.
    #   - DERIVED: a RUNNER-origin session whose binding has not been seen within
    #     SESSION_STALE_AFTER. Computed here, never stored, so it reverses itself
    #     the moment the task is reported again. Web sessions are exempt — they
    #     have no runner to be seen by, so only an explicit archive ends them.
    from django.db.models import Max, Q

    if state not in ("active", "archived", "all"):
        raise HttpError(422, "state must be one of: active, archived, all")

    slugs = _visible_slugs(request)
    rows = (
        Session.objects.select_related("agent", "runner_binding", "runner_binding__runner")
        .filter(workspace_id__in=slugs)
        .filter(Q(created_by=request.user) | Q(runner_binding__isnull=False))
        .annotate(_last_msg_at=Max("messages__created_at"))
        .distinct()
        .order_by("-created_at")
    )
    unseen = services.unseen_q()   # defined once in staleness.py; see Step 3
    if state == "active":
        rows = rows.filter(status=Session.ACTIVE).exclude(unseen)
    elif state == "archived":
        rows = rows.filter(Q(status=Session.ARCHIVED) | unseen)

    out = [_out(s) for s in rows]
    # Running first, then genuinely-most-recent. Sorting by created_at made a
    # dead repo and a live one interleave arbitrarily (both "created" in the
    # same report sweep); last_activity_at is the real signal. The client can
    # re-group by project — this is the default order.
    out.sort(key=lambda r: (not r["running"], -(r["last_activity_at"].timestamp())))
    # Clamp AFTER the sort, never as a queryset slice: the queryset is ordered by
    # -created_at, so slicing it could drop the running session this sort exists to
    # float. `state=active` already bounds the set; this is a payload backstop.
    return out[: clamp_limit(limit)]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_session_list_state.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the existing list suites for regressions**

Run: `uv run pytest tests/test_session_list_unified.py tests/test_session_liveness.py -v`
Expected: PASS. If a pre-existing test creates a `RunnerBinding` without `live_seen_at`, it will now be filtered out of the default `state=active` — that is the intended new behaviour; update the fixture to set `live_seen_at=timezone.now()` rather than weakening the filter.

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/services.py apps/canopy_sessions/api.py tests/test_session_list_state.py
git commit -m "feat(sessions): give the list an end — state=active|archived|all

Archived is written (an emdash close) OR derived (a runner session unseen for
SESSION_STALE_AFTER). The derived half never persists, so it reverses itself
when the task is reported again. Web sessions are exempt from staleness — no
runner reports them, so only an explicit archive ends one. limit clamps after
the running-first sort, since a queryset slice orders by -created_at and could
cut the very row the sort floats."
```

---

### Task 6: Server — manual archive / unarchive

**Files:**
- Modify: `apps/canopy_sessions/api.py` (two routes, after `list_messages`)
- Test: `tests/test_session_archive_routes.py` (create)

**Interfaces:**
- Consumes: `_session_or_404` and `_out` (existing, in the same module); `Session.ARCHIVED` / `Session.ACTIVE`.
- Produces: `POST /api/canopy-sessions/{session_id}/archive` and `.../unarchive`, both returning `SessionOut`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_archive_routes.py`:

```python
"""Manual archive: the escape hatch for a web chat (which no runner will ever close)
and for force-retiring a row without touching emdash."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def test_archive_then_unarchive_round_trips():
    user, ws, c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")

    resp = c.post(f"/api/canopy-sessions/{s.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    assert {r["id"] for r in c.get("/api/canopy-sessions/").json()} == set()

    resp = c.post(f"/api/canopy-sessions/{s.id}/unarchive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert {r["id"] for r in c.get("/api/canopy-sessions/").json()} == {str(s.id)}


def test_archiving_twice_is_a_no_op_not_an_error():
    user, ws, c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200
    assert Session.objects.get(pk=s.pk).status == Session.ARCHIVED


def test_a_non_member_gets_404_not_403():
    """Same as every other route on this router — no existence leak across tenants."""
    _user, ws, _c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="web")
    other = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=other_ws, role=WorkspaceMembership.OWNER)
    c2 = Client()
    c2.force_login(other)
    assert c2.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 404
    assert Session.objects.get(pk=s.pk).status == Session.ACTIVE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_session_archive_routes.py -v`
Expected: FAIL — 404 on the archive POST (no such route)

- [ ] **Step 3: Add the routes**

In `apps/canopy_sessions/api.py`, add immediately after the `list_messages` route:

```python
@router.post("/{session_id}/archive", response=SessionOut, summary="Archive a session")
def archive_session(request: HttpRequest, session_id: uuid.UUID):
    """Retire a session by hand. The escape hatch for a web chat — no runner will ever
    report it archived — and for force-retiring a row without touching emdash.
    Idempotent, and never destructive: /unarchive brings it straight back."""
    return _set_status(request, session_id, Session.ARCHIVED)


@router.post("/{session_id}/unarchive", response=SessionOut, summary="Unarchive a session")
def unarchive_session(request: HttpRequest, session_id: uuid.UUID):
    """Undo an archive. Note this clears only the WRITTEN half: a runner session that
    is also past SESSION_STALE_AFTER stays out of `state=active` until its runner
    reports it again, because that half is derived on every read."""
    return _set_status(request, session_id, Session.ACTIVE)
```

And add this helper next to `_session_or_404`:

```python
def _set_status(request: HttpRequest, session_id: uuid.UUID, status: str) -> dict:
    session = _session_or_404(request, session_id)   # membership gate: non-member -> 404
    if session.status != status:
        session.status = status
        session.save(update_fields=["status", "updated_at"])
    return _out(session)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_session_archive_routes.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/canopy_sessions/api.py tests/test_session_archive_routes.py
git commit -m "feat(sessions): manual archive/unarchive routes

A web chat has no runner to close it, so without this it could never end.
Idempotent, membership-gated (non-member 404s, no existence leak), and never
destructive — there is deliberately no delete."
```

---

### Task 7: Migration — collapse the existing backlog

**Files:**
- Modify: `apps/canopy_sessions/staleness.py` (add `archive_stale_sessions`)
- Create: `apps/canopy_sessions/migrations/0009_archive_stale_sessions.py`
- Test: `tests/test_session_archive_backfill.py` (create)

**Interfaces:**
- Consumes: `apps.canopy_sessions.staleness.unseen_q()` (Task 5); `Session.status`, `Session.origin`, `RunnerBinding.live_seen_at`.
- Produces: `staleness.archive_stale_sessions(session_model) -> int` — takes the model class as an argument so the migration can pass its historical model and the test can pass the real one.

- [ ] **Step 1: Confirm the migration leaf**

Run: `uv run python manage.py showmigrations canopy_sessions`
Expected: the last applied migration is `0008_runnerbinding_backfill_requested`. Use it as the dependency. If a later migration exists, depend on that one instead.

- [ ] **Step 2: Write the failing test**

Create `tests/test_session_archive_backfill.py`:

```python
"""The 0009 backfill: today's labs list is entirely rows nobody can retire, because
until now nothing could. Apply the new rule once so the list starts clean."""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    return user, ws, runner


def test_backfill_archives_only_stale_runner_sessions():
    from apps.canopy_sessions.staleness import archive_stale_sessions

    user, ws, runner = _ctx()
    fresh = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="live")
    RunnerBinding.objects.create(session=fresh, runner=runner, session_key="live",
                                 live_seen_at=timezone.now())
    stale = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="gone")
    RunnerBinding.objects.create(session=stale, runner=runner, session_key="gone",
                                 live_seen_at=timezone.now() - dt.timedelta(days=9))
    orphan = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="orphan")
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")

    archive_stale_sessions(Session)

    assert Session.objects.get(pk=fresh.pk).status == Session.ACTIVE
    assert Session.objects.get(pk=stale.pk).status == Session.ARCHIVED
    assert Session.objects.get(pk=orphan.pk).status == Session.ARCHIVED  # no binding = unseen
    assert Session.objects.get(pk=web.pk).status == Session.ACTIVE       # web is exempt
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_session_archive_backfill.py -v`
Expected: FAIL — `ImportError: cannot import name 'archive_stale_sessions' from 'apps.canopy_sessions.staleness'`

- [ ] **Step 4: Add the backfill helper to the staleness module**

Append to `apps/canopy_sessions/staleness.py` (created in Task 5). It takes the model class as an argument because a data migration receives *historical* models via `apps.get_model` — those have no custom managers or methods, so the rule cannot live on the model itself.

```python
def archive_stale_sessions(session_model) -> int:
    """Archive runner-origin sessions with no recent runner sighting. The one-shot
    backfill for rows that predate any means of retiring them. Web sessions are exempt
    (no runner reports them). Returns the number of rows changed.

    Takes the model class so the migration can pass its historical model and the test
    can pass the real one — the rule itself is identical for both.
    """
    return (
        session_model.objects.filter(status="active")
        .filter(unseen_q())
        .update(status="archived")
    )
```

String literals (`"runner"`, `"active"`, `"archived"`) rather than `Session.ACTIVE` — a historical model has the fields but not the class constants.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_session_archive_backfill.py -v`
Expected: PASS

- [ ] **Step 6: Write the migration**

Create `apps/canopy_sessions/migrations/0009_archive_stale_sessions.py`:

```python
"""Collapse the pre-lifecycle Sessions backlog.

Until now nothing could ever retire a session row, so labs accumulated one per emdash
task any runner ever reported — most of them tasks that no longer exist. Apply the new
staleness rule once so the list starts clean.

Irreversible by design, and safe to be: un-archiving happens naturally on the next
report, so the reverse is a genuine no-op rather than lost information.
"""
from django.db import migrations

from apps.canopy_sessions.staleness import archive_stale_sessions


def forwards(apps, schema_editor):
    archive_stale_sessions(apps.get_model("canopy_sessions", "Session"))


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0008_runnerbinding_backfill_requested"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
```

- [ ] **Step 7: Verify the migration applies cleanly**

Run: `uv run python manage.py migrate canopy_sessions`
Expected: `Applying canopy_sessions.0009_archive_stale_sessions... OK`

Run: `uv run python manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (this migration adds no schema change)

- [ ] **Step 8: Commit**

```bash
git add apps/canopy_sessions/staleness.py apps/canopy_sessions/migrations/0009_archive_stale_sessions.py tests/test_session_archive_backfill.py
git commit -m "feat(sessions): backfill — archive the pre-lifecycle stale backlog

Every row on labs predates any means of retiring one. Apply the staleness rule
once so the list starts clean. Irreversible on purpose: un-archiving happens
naturally on the next report, so the reverse really is a no-op."
```

---

### Task 8: Frontend — `Show archived` toggle

**Files:**
- Modify: `frontend/src/api/chat.ts:100-102` (`listSessions`)
- Modify: `frontend/src/components/chat/ChatSessionsPanel.tsx`
- Regenerate: `frontend/src/api/generated.ts`
- Test: `frontend/src/api/sessionsQuery.test.ts` (create)

**Interfaces:**
- Consumes: `GET /api/canopy-sessions/?state=…` (Task 5).
- Produces: `sessionsPath(state: SessionState): string` exported from `frontend/src/api/chat.ts`; `listSessions(state?: SessionState)`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/sessionsQuery.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { sessionsPath } from "./chat";

describe("sessionsPath", () => {
  it("omits the param for the default state, so the URL stays the cached one", () => {
    expect(sessionsPath("active")).toBe("/api/canopy-sessions/");
  });

  it("passes a non-default state through", () => {
    expect(sessionsPath("archived")).toBe("/api/canopy-sessions/?state=archived");
    expect(sessionsPath("all")).toBe("/api/canopy-sessions/?state=all");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/api/sessionsQuery.test.ts`
Expected: FAIL — `No "sessionsPath" export is defined on the "./chat" mock` / import error

- [ ] **Step 3: Implement it**

In `frontend/src/api/chat.ts`, replace `listSessions`:

```typescript
export type SessionState = "active" | "archived" | "all";

/** The list URL for a state. `active` is the server default, so it sends no param. */
export function sessionsPath(state: SessionState = "active"): string {
  return state === "active"
    ? "/api/canopy-sessions/"
    : `/api/canopy-sessions/?state=${state}`;
}

export function listSessions(state: SessionState = "active"): Promise<ChatSession[]> {
  return request<ChatSession[]>(sessionsPath(state));
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/api/sessionsQuery.test.ts`
Expected: PASS (2 tests)

- [ ] **Step 5: Add the toggle to the panel**

In `frontend/src/components/chat/ChatSessionsPanel.tsx`:

Add to the imports from `@/api/chat`: `type SessionState`.

Add beside the existing `sort` state:

```tsx
  const [showArchived, setShowArchived] = useState(false)
```

Derive the state and thread it through both fetches. Replace `listSessions()` in the mount effect's `jobs` array with `listSessions(showArchived ? 'all' : 'active')`, add `showArchived` to that effect's dependency array, and replace the poll body:

```tsx
  // A slow REST refresh keeps the unified list current (the live push into the
  // list is a deferred follow-up; per-row liveness is live inside ChatPanel).
  useEffect(() => {
    const state: SessionState = showArchived ? 'all' : 'active'
    const id = window.setInterval(() => {
      listSessions(state)
        .then(setSessions)
        .catch(() => { /* keep last-good; the mount fetch owns first-error surfacing */ })
    }, 20_000)
    return () => window.clearInterval(id)
  }, [showArchived])
```

Add the toggle button inside the existing sort row, immediately after the `.map` over `['time', 'project']` and before that `<div>` closes:

```tsx
          <button
            type="button"
            onClick={() => setShowArchived((v) => !v)}
            aria-pressed={showArchived}
            className={
              showArchived
                ? 'ml-2 rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 font-medium text-primary'
                : 'ml-2 rounded-md border border-border px-2 py-0.5 text-muted-foreground hover:bg-muted'
            }
          >
            Show archived
          </button>
```

The sort row is currently gated on `sessions.length > 1`. Change that condition to `sessions.length > 1 || showArchived` so the toggle does not disappear when the archived view is empty and you need to get back.

- [ ] **Step 6: Regenerate the API types**

Start the backend if it is not running (`uv run python manage.py runserver`), then:

Run: `cd frontend && npm run gen:api`
Expected: `frontend/src/api/generated.ts` updated with `archived` on `ReportSessionsIn`, the `state`/`limit` query params on the sessions list, and the two new archive operations.

- [ ] **Step 7: Type-check and build**

Run: `cd frontend && npm run build`
Expected: build succeeds with no type errors

Run: `cd frontend && npm run test`
Expected: PASS (whole vitest suite)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/api/generated.ts frontend/src/api/sessionsQuery.test.ts frontend/src/components/chat/ChatSessionsPanel.tsx
git commit -m "feat(sessions): Show archived toggle on the unified list

Default view is state=active; the toggle switches both the mount fetch and the
20s poll to state=all. The toggle stays visible when the list is empty, so an
empty archived view is not a dead end."
```

---

### Task 9: Full verification and documentation

**Files:**
- Modify: `CLAUDE.md` (the `apps/canopy_sessions` API section)
- Modify: `packages/canopy_runner/README.md` (the fail-soft note that Task 1 invalidated)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing importable.

- [ ] **Step 1: Run the entire backend suite**

Run: `uv run pytest`
Expected: PASS, no failures. Pay particular attention to `tests/test_architecture_boundary.py` — the new code stays framework-tier and must not have introduced a product import.

- [ ] **Step 2: Run the entire runner suite**

Run: `uv run pytest packages/canopy_runner/tests`
Expected: PASS

- [ ] **Step 3: Run the frontend build and tests**

Run: `cd frontend && npm run build && npm run test`
Expected: both succeed

- [ ] **Step 4: Correct the runner README**

`packages/canopy_runner/README.md` around lines 8, 115, and 129 describes `list_open_sessions` as a read that answers "does this session still exist?" and never crashes. Update those passages to state that a MISSING db still returns `[]`, but a READ FAILURE now raises `EmdashReadError` and the runner skips the report — and that `verify-emdash` remains the proactive check for the drift that causes it.

- [ ] **Step 5: Update CLAUDE.md**

In the `### Chat (apps/canopy_sessions)` section, update the endpoint list:

```markdown
- `GET /api/canopy-sessions/?state=active|archived|all&limit=<n>` — List sessions (default `active`). A session is archived when it is **written** so (the runner reported its emdash task archived, or `/archive` was called) **or derived** so (a `runner`-origin session whose binding has not been seen within `SESSION_STALE_AFTER` = 3 days). The derived half is recomputed every read, so a reported task un-archives itself; web sessions are exempt from it. Nothing is ever deleted.
- `POST /api/canopy-sessions/{id}/archive` · `POST /api/canopy-sessions/{id}/unarchive` — Retire / restore a session by hand (idempotent; the escape hatch for web chats, which no runner will ever close).
```

And in the harness runner-route list, note that `POST /api/harness/runners/{runner_id}/sessions` now carries `archived: [<emdash task name>]` alongside `sessions` — the closing signal that lets the server retire a row instead of inferring it from absence.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md packages/canopy_runner/README.md
git commit -m "docs: session lifecycle — archive semantics and the fail-loud emdash read"
```

- [ ] **Step 7: Open the PR**

```bash
git push -u origin HEAD
gh pr create --title "Give the supervisor Sessions list an end" --body "$(cat <<'EOF'
The unified Sessions list (#355) was append-only: no delete, no archive, no filter,
no limit. A row was created the first time any runner reported an emdash task and
lived forever — because canopy could not tell "you closed it" from "I lost sight of
it". A task falling off a report looked identical to a dead runner, a renamed
column, or truncation past `LIMIT 30`.

**Explicit signals write, staleness derives.**

- The runner now reports the emdash tasks it has seen **archived**. That closing
  signal retires the session row; re-opening the task brings it back.
- The residue — a task that vanished with no closing signal — drops out of
  `state=active` after 3 days unseen. Derived at read time, so it reverses itself.
  Web sessions are exempt: no runner reports them.
- Manual `/archive` + `/unarchive` for web chats. Nothing deletes.
- A migration applies the rule once, collapsing the existing labs backlog.

**Also fixes the silent blanking.** `list_open_sessions` swallowed every
`sqlite3.Error` and returned `[]`; the 10s heartbeat POSTed that as an empty report
and cleared *every* binding. A schema drift blanked the supervisor with nothing in
the log. It now raises, and the runner skips the report — "I could not look" instead
of "everything is gone".

Spec: `docs/superpowers/specs/2026-07-23-session-list-lifecycle-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Deployment note

The runner half (Tasks 1-3) ships by `git pull` + `launchctl kickstart` on the laptop checkout at `~/emdash-projects/canopy-web`, **not** by the labs deploy. The server tolerates an old runner (`archived` defaults to `[]`, so it simply never closes a row), and a new runner tolerates an old server (the extra key is ignored by Pydantic). Order does not matter — but until the runner is updated, only the derived staleness half is doing any work.
