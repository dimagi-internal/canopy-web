# Close a session from the phone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `/supervisor` (and the chat list, and an open chat) a Close action that really ends a session — deleting the emdash task on a laptop runner, or archiving outright when there is no task on a box.

**Architecture:** One endpoint, `POST /api/canopy-sessions/{id}/close`, branching on one observed fact: *is a runner currently reporting an emdash task for this session?* If yes, the server **writes nothing** — it relays a `close_session` control frame down `ws/runner/{id}/`, the runner deletes the emdash task over CDP, and the runner's next session report carries the task name in the already-built-but-never-fired `archived:` closing signal, which `replace_reported_sessions` turns into `status=ARCHIVED`. If no (cloud runner, or a web chat that never bound), the server cancels the session's turns and archives directly, and it sticks because nothing will ever report it back.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2 · Django Channels (`apps/realtime`) · Python laptop runner + a Playwright/CDP Node sidecar driving emdash · React 19 + Vite + Tailwind 4 + Vitest.

**Spec:** `docs/superpowers/specs/2026-07-30-close-session-from-phone-design.md`

## Global Constraints

- **Framework tier only.** `apps/canopy_sessions`, `apps/harness`, `apps/realtime` are framework apps. They must never import product code (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`, `storyboards`). `tests/test_architecture_boundary.py` fails CI on a violation.
- **Cross-app imports inside a service stay lazy.** `apps/canopy_sessions/services.py` imports `apps.harness.models` *inside* the function, not at module scope — there is an import cycle. Follow `services.answer_menu`'s existing `from apps.harness.models import Runner  # framework->framework; lazy, import cycle`.
- **Refusals are `200` with `ok:false` and a stable reason string, never a 4xx.** Matches `answer_menu` and `reset`.
- **Reason vocabulary is closed:** `unavailable`, `already_closed`. Do not add `unbound` — a session with no binding is the second branch, not an error.
- **Regenerate OpenAPI types whenever `apps/**/api.py` or `apps/**/schemas.py` changes:** `cd frontend && npm run gen:api` (backend on :8000) or `npm run gen:api:local`. `regen-openapi.yml` fails the PR if `frontend/src/api/generated.ts` is stale.
- **Design tokens only** in frontend work — `bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-destructive`. No raw palette literals (`stone-*`, `red-*`, …).
- **Test commands:**
  - Django: `uv run pytest tests/<file>.py -v`
  - Laptop runner: `uv run --with pytest pytest tests/<file>.py -v` from `runner/canopy_runner`
  - Frontend: `cd frontend && npx vitest run <path>` and `npm run build` for the type check
- **Open the PR with auto-merge armed and no strategy flag:** `gh pr merge <n> --auto`, then verify with `gh pr view <n> --json autoMergeRequest`.

---

## File Structure

**Backend**
- `apps/canopy_sessions/models.py` — add `RunnerBinding.reported_at`. *(Task 1)*
- `apps/canopy_sessions/migrations/00XX_runnerbinding_reported_at.py` — generated. *(Task 1)*
- `apps/harness/services.py` — stamp `reported_at` in `replace_reported_sessions`. *(Task 1)*
- `apps/canopy_sessions/services.py` — `cancel_session_turns`, `_is_runner_reported`, `close_session`. *(Task 2)*
- `apps/realtime/consumers.py` — `runner_close_session` frame handler. *(Task 2)*
- `apps/canopy_sessions/api.py` — the `/close` route; `/stop` refactored onto `cancel_session_turns`. *(Task 3)*

**Runner**
- `runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs` — `close-task` command. *(Task 4)*
- `runner/canopy_runner/canopy_runner/cdp_control.py` — `close_task()` wrapper. *(Task 4)*
- `runner/canopy_runner/canopy_runner/sessions.py` — `request_close_report()` + drain into `archived`. *(Task 5)*
- `runner/canopy_runner/canopy_runner/close.py` — new; orchestrates delete-then-report. *(Task 5)*
- `runner/canopy_runner/canopy_runner/main.py` — route the `close_session` control frame. *(Task 5)*

**Frontend**
- `frontend/src/api/chat.ts` — `closeSession()`. *(Task 6)*
- `frontend/src/components/chat/closeAction.ts` — new; the pure derivation. *(Task 6)*
- `frontend/src/components/chat/ChatSessionsPanel.tsx` — per-row Close. *(Task 7)*
- `frontend/src/pages/ChatPage.tsx` — header Close. *(Task 8)*

---

### Task 1: `RunnerBinding.reported_at` — a signal only the report loop writes

**Why this task exists:** `close_session` must answer "is a runner reporting an emdash task for this session?" No existing field can. `record_session` (`apps/harness/services.py:1314-1365`) is called by **both** runners and stamps `session_key` *and* `live_seen_at` — the cloud runner just writes a Claude session id where the laptop writes an emdash task name. So we add a field written in exactly one place.

**Files:**
- Modify: `apps/canopy_sessions/models.py` (near `live_seen_at`, ~line 206)
- Create: `apps/canopy_sessions/migrations/00XX_runnerbinding_reported_at.py` (generated)
- Modify: `apps/harness/services.py:1517`
- Test: `tests/test_binding_reported_at.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RunnerBinding.reported_at: datetime | None`. Set by `apps.harness.services.replace_reported_sessions` only. Task 2 reads it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_binding_reported_at.py`:

```python
"""`reported_at` answers ONE question: is a runner reporting an emdash task for
this session? It exists because no other field can — `record_session` is called by
BOTH runners and stamps `session_key` and `live_seen_at`, so a cloud binding is
indistinguishable from a laptop one by either."""
import pytest
from django.contrib.auth.models import User

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
        self.status = ""
        self.last_interacted_at = None
        self.recent_messages = []


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", kind="laptop", host="jj-mbp", paired_by=user, workspace=ws
    )
    return user, ws, runner


def test_the_report_loop_stamps_reported_at():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    binding = RunnerBinding.objects.get(session_key="ddd")
    assert binding.reported_at is not None


def test_record_session_stamps_live_seen_at_but_not_reported_at():
    """The whole point of the field. `record_session` is the CLOUD runner's only
    binding write, so if it stamped this, a cloud session would take the local
    branch and canopy would relay a close to a box with no emdash to close."""
    user, ws, runner = _ctx()
    agent = None
    session = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    binding = RunnerBinding.objects.create(
        session=session, runner=runner, host=runner.host, session_key="", thread_key=str(session.id)
    )
    services.record_session(
        agent, str(session.id), runner=runner, project="canopy-web", workspace=ws,
        emdash_task_id="0d6f2c1e-1111-2222-3333-444455556666",
    )
    binding.refresh_from_db()
    assert binding.live_seen_at is not None
    assert binding.reported_at is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_binding_reported_at.py -v`
Expected: FAIL — `AttributeError`/`FieldError` on `reported_at` (the field does not exist).

- [ ] **Step 3: Add the field**

In `apps/canopy_sessions/models.py`, directly below `live_seen_at = models.DateTimeField(null=True, blank=True)`:

```python
    # Stamped ONLY by the report loop (apps/harness/services.py::
    # replace_reported_sessions) — deliberately NOT by record_session, which BOTH
    # runners call. That asymmetry is the whole point: it makes "a runner is
    # reporting an emdash task for this session" an observable fact rather than
    # something inferred from Runner.kind (which answers "what program is this",
    # a different question, and is already deprecated as a behavioural input).
    # `live_seen_at` cannot answer it — the cloud runner's record_session stamps
    # that too, and writes a Claude session id into session_key where the laptop
    # writes an emdash task name. Read against the same stale_cutoff() the session
    # list uses, so "reported" and "live" can never mean different windows.
    reported_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations canopy_sessions -n runnerbinding_reported_at`
Expected: one `AddField` migration. Open it and confirm it contains only that field.

- [ ] **Step 5: Stamp it in the report loop**

In `apps/harness/services.py`, inside `replace_reported_sessions`'s per-session loop, change:

```python
            binding.live_seen_at = timezone.now()
```

to:

```python
            binding.live_seen_at = timezone.now()
            # The one write site. See RunnerBinding.reported_at — `close` branches
            # on this, and it is only trustworthy because record_session leaves it
            # alone.
            binding.reported_at = binding.live_seen_at
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_binding_reported_at.py tests/test_session_polled_liveness.py tests/test_harness_emdash_sessions.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/models.py apps/canopy_sessions/migrations/ apps/harness/services.py tests/test_binding_reported_at.py
git commit -m "feat(sessions): a binding signal only the report loop writes

close() must know whether a runner is reporting an emdash task for a session.
No existing field can say: record_session is called by BOTH runners and stamps
live_seen_at and session_key, so a cloud binding looks exactly like a laptop one.
reported_at has a single write site, which is what makes it trustworthy."
```

---

### Task 2: `close_session` — two branches, one question

**Files:**
- Modify: `apps/canopy_sessions/services.py`
- Modify: `apps/realtime/consumers.py` (beside `runner_menu_answer`, ~line 259)
- Test: `tests/test_session_close.py`

**Interfaces:**
- Consumes: `RunnerBinding.reported_at` (Task 1); `apps.canopy_sessions.staleness.stale_cutoff()`; `apps.realtime.groups.publish` / `groups.runner_group`.
- Produces:
  - `services.close_session(*, session: Session) -> str` returning `"closing" | "closed" | "unavailable" | "already_closed"`.
  - `services.cancel_session_turns(session: Session) -> bool`.
  - Control frame on `runner.{id}`: `{"type": "runner.close_session", "session_id": str, "session_key": str}`, delivered to the runner as `{"type": "close_session", "session_id": ..., "session_key": ...}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_close.py`:

```python
"""Closing a session. Two branches on ONE question — is a runner reporting an
emdash task for this session? — because a server-only archive does not survive
a local session: replace_reported_sessions un-archives anything re-reported as
open, and the runner re-reports every ~10s."""
import pytest
from unittest.mock import patch
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import services
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return user, ws


def _runner(user, ws, *, status=Runner.ONLINE):
    return Runner.objects.create(
        name="jj-mbp", kind="laptop", host="jj-mbp", paired_by=user, workspace=ws,
        status=status, last_heartbeat_at=timezone.now(),
    )


def _local_session(user, ws, runner, *, key="ddd"):
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title=key
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host, session_key=key,
        thread_key=f"emdash:{key}", live_seen_at=timezone.now(),
        reported_at=timezone.now(),
    )
    return s


def test_a_reported_session_relays_and_writes_nothing():
    """The emdash task is the truth for a local session. Writing here would make
    canopy a second source of truth that the next report can disagree with."""
    user, ws = _ctx()
    s = _local_session(user, ws, _runner(user, ws))
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closing"
    s.refresh_from_db()
    assert s.status == Session.ACTIVE
    frame = pub.call_args[0][1]
    assert frame["type"] == "runner.close_session"
    assert frame["session_key"] == "ddd"
    assert frame["session_id"] == str(s.id)


def test_an_unreported_session_archives_here_and_sticks():
    """Cloud sessions and never-bound web chats. Nothing on a box to delete, and
    nothing will ever report them back, so the write is safe and final."""
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    s.refresh_from_db()
    assert s.status == Session.ARCHIVED
    assert pub.call_count == 0


def test_a_cloud_binding_takes_the_unreported_branch():
    """A cloud runner calls record_session too, so the binding exists and carries a
    session_key. reported_at is what tells them apart."""
    user, ws = _ctx()
    runner = _runner(user, ws)
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host,
        session_key="0d6f2c1e-1111-2222-3333-444455556666",
        thread_key=str(s.id), live_seen_at=timezone.now(), reported_at=None,
    )
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    assert pub.call_count == 0


def test_a_stale_report_takes_the_unreported_branch():
    """Reported three minutes ago is not reported now. Relaying to a box that is not
    listening would archive nothing and report success."""
    user, ws = _ctx()
    runner = _runner(user, ws)
    s = _local_session(user, ws, runner)
    binding = s.runner_binding
    binding.reported_at = timezone.now() - timezone.timedelta(hours=1)
    binding.save(update_fields=["reported_at"])
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    assert pub.call_count == 0


def test_an_unreachable_runner_refuses_up_front():
    """Never queue a close. A close that sits until a box comes back is
    indistinguishable from one that worked."""
    user, ws = _ctx()
    runner = _runner(user, ws, status=Runner.PAUSED)
    s = _local_session(user, ws, runner)
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "unavailable"
    s.refresh_from_db()
    assert s.status == Session.ACTIVE
    assert pub.call_count == 0


def test_already_archived_is_a_refusal_not_a_second_close():
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB,
        title="done", status=Session.ARCHIVED,
    )
    assert services.close_session(session=s) == "already_closed"


def test_the_unreported_branch_cancels_non_terminal_turns():
    """A queued turn on a closed session would wake it up again."""
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    turn = Turn.objects.create(chat_session=s, status=Turn.QUEUED, prompt="hi")
    services.close_session(session=s)
    turn.refresh_from_db()
    assert turn.status not in Turn.NON_TERMINAL


def test_the_reported_branch_cancels_turns_too_before_relaying():
    """Deleting the emdash task kills the process the turn is running in. Cancel
    first or canopy is left holding an EXECUTING turn whose runner will never
    finish it — it sits until the lease sweep, wedging the agent via
    one_executing_turn_per_agent."""
    user, ws = _ctx()
    s = _local_session(user, ws, _runner(user, ws))
    turn = Turn.objects.create(chat_session=s, status=Turn.QUEUED, prompt="hi")
    with patch("apps.realtime.groups.publish"):
        assert services.close_session(session=s) == "closing"
    turn.refresh_from_db()
    assert turn.status not in Turn.NON_TERMINAL
    s.refresh_from_db()
    assert s.status == Session.ACTIVE   # still not archived here — the report does that


def test_a_closed_name_never_retires_a_still_open_namesake():
    """emdash task names are not unique. The closing signal this feature finally
    produces must not retire a DIFFERENT, still-open task that happens to share a
    name — `now_keys` wins over `archived` (apps/harness/services.py)."""
    from apps.harness import services as harness_services

    user, ws = _ctx()
    runner = _runner(user, ws)

    class _Reported:
        def __init__(self, task):
            self.emdash_task = task
            self.project = "canopy-web"
            self.status = ""
            self.last_interacted_at = None
            self.recent_messages = []

    harness_services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    # The runner deleted one "ddd" and re-reports another still open under the
    # same name in the SAME wholesale call.
    harness_services.replace_reported_sessions(
        runner, ws, [_Reported("ddd")], archived=["ddd"]
    )
    assert Session.objects.get(runner_binding__session_key="ddd").status == Session.ACTIVE
```

> If `Runner.PAUSED` is not the right constant for "not reachable", check
> `apps/harness/models.py::Runner` for the `live_status` values and use whichever
> one is neither `ONLINE` nor `DEGRADED`. `services.answer_menu` uses the same
> `reachable = {Runner.ONLINE, Runner.DEGRADED}` set — copy it exactly.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/test_session_close.py -v`
Expected: FAIL — `AttributeError: module 'apps.canopy_sessions.services' has no attribute 'close_session'`.

- [ ] **Step 3: Add the service functions**

In `apps/canopy_sessions/services.py`, beside `answer_menu`:

```python
def cancel_session_turns(session: Session) -> bool:
    """Cancel every non-terminal turn on a session. Returns whether anything moved.

    ALL non-terminal turns, not just the newest: a mid-reply send queues a second
    turn behind the one still running, so both must be reached — the running one
    gets cancel_requested, the queued one is finished CANCELLED.

    Deliberately NOT `any(cancel_turn(t) for t in turns)`: any() short-circuits on
    the first truthy result and would skip every turn after it.
    """
    from apps.harness import services as harness_services  # framework->framework; lazy
    from apps.harness.models import Turn

    cancelled = False
    for turn in Turn.objects.filter(chat_session=session, status__in=list(Turn.NON_TERMINAL)):
        if harness_services.cancel_turn(turn) is not None:
            cancelled = True
    return cancelled


def _is_runner_reported(binding) -> bool:
    """Is a runner CURRENTLY reporting an emdash task for this session?

    The one question `close_session` branches on, observed rather than inferred.
    `Runner.kind` would answer "what program is this" — a different question, and
    already deprecated as a behavioural input. `live_seen_at` and `session_key`
    cannot answer it at all: `record_session` is called by BOTH runners and stamps
    both, with the cloud runner writing a Claude session id where the laptop writes
    an emdash task name. Hence `reported_at`, which only the report loop writes.

    Read against the same `stale_cutoff()` the session list uses, so "reported" and
    "live" can never drift into meaning different windows.
    """
    from .staleness import stale_cutoff

    if binding is None or binding.runner_id is None or not binding.session_key:
        return False
    if binding.reported_at is None:
        return False
    return binding.reported_at >= stale_cutoff()


def close_session(*, session: Session) -> str:
    """End a session for good. Returns
    "closing" | "closed" | "unavailable" | "already_closed".

    Two branches on one question — see `_is_runner_reported`.

    REPORTED (a laptop's emdash task): cancel the turns, then relay a close and
    write NOTHING to the session. The emdash task is the truth for a local session,
    and `replace_reported_sessions` un-archives anything re-reported as open, so a
    status write here would be undone within ~10s anyway. The runner deletes the
    task and puts its name in the `archived:` closing signal on its next report;
    that is what retires the row.

    UNREPORTED (a cloud session, a web chat that never bound): nothing exists on a
    box. Cancel the turns so a queued one cannot wake it, archive, done — and it
    sticks, because nothing will ever report it back.

    A refusal is a returned reason, never a raise: a session can go stale between
    the phone rendering the list and a thumb reaching it, which is ordinary rather
    than a client error. `unavailable` deliberately does NOT queue — a close that
    sits until a box returns is indistinguishable from one that worked.
    """
    from apps.harness.models import Runner  # framework->framework; lazy, import cycle

    if session.status == Session.ARCHIVED:
        return "already_closed"

    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    if _is_runner_reported(binding):
        reachable = {Runner.ONLINE, Runner.DEGRADED}
        if binding.runner.live_status not in reachable:
            return "unavailable"
        # Cancel BEFORE relaying. Deleting the emdash task kills the process the
        # turn runs in, so a live turn would otherwise stay EXECUTING with nobody
        # left to finish it — held until the lease sweep, wedging the agent through
        # one_executing_turn_per_agent. Cancelling first also means the ledger
        # records a cancellation rather than a turn that merely stops emitting.
        cancel_session_turns(session)
        from apps.realtime import groups

        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.close_session",
            "session_id": str(session.id),
            "session_key": binding.session_key,
        })
        return "closing"

    cancel_session_turns(session)
    session.status = Session.ARCHIVED
    session.save(update_fields=["status", "updated_at"])
    return "closed"
```

- [ ] **Step 4: Add the consumer handler**

In `apps/realtime/consumers.py`, directly after `runner_menu_answer`:

```python
    async def runner_close_session(self, message):
        # runner.{id} group_send type="runner.close_session" — a human closed this
        # session from the web. The runner deletes the emdash task and reports the
        # name in its `archived:` closing signal; the server wrote nothing, so the
        # emdash task stays the single source of truth for a local session and a
        # failed delete simply leaves the row where it is.
        await self.send_json({
            "type": "close_session",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
        })
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_session_close.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/canopy_sessions/services.py apps/realtime/consumers.py tests/test_session_close.py
git commit -m "feat(sessions): close_session — relay for a reported session, archive otherwise

A server-only archive does not end a local session: replace_reported_sessions
un-archives anything re-reported as open, and the runner re-reports every ~10s.
So the reported branch writes nothing and lets the runner's `archived:` closing
signal — built long ago, never fired because emdash deletes rather than archives —
retire the row. The unreported branch (cloud, unbound web chat) archives here,
where it sticks."
```

---

### Task 3: The `/close` route

**Files:**
- Modify: `apps/canopy_sessions/api.py` (beside `answer_menu` ~line 358; `/stop` at ~line 375)
- Modify: `frontend/src/api/generated.ts` (regenerated, committed)
- Test: `tests/test_session_close_route.py`

**Interfaces:**
- Consumes: `services.close_session`, `services.cancel_session_turns` (Task 2); `_session_or_404`.
- Produces: `POST /api/canopy-sessions/{session_id}/close` → `{"ok": bool, "closing": bool, "reason": str}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_close_route.py`:

```python
"""The HTTP surface. Refusals are 200 with ok:false — a session can go stale
between the phone rendering the list and a thumb reaching it."""
import pytest
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def test_closing_a_web_session_archives_it_and_drops_it_from_the_list():
    user, ws, c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web"
    )
    resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "closing": False, "reason": ""}
    assert c.get("/api/canopy-sessions/").json() == []


def test_closing_a_reported_session_reports_closing_and_leaves_it_listed():
    """It is still open until the runner says otherwise. Saying so is the honest
    answer; the client renders a pending state."""
    user, ws, c = _ctx()
    runner = Runner.objects.create(
        name="jj-mbp", kind="laptop", host="jj-mbp", paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="ddd"
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host, session_key="ddd",
        thread_key="emdash:ddd", live_seen_at=timezone.now(), reported_at=timezone.now(),
    )
    with patch("apps.realtime.groups.publish"):
        resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "closing": True, "reason": ""}
    assert [r["id"] for r in c.get("/api/canopy-sessions/").json()] == [str(s.id)]


def test_a_refusal_is_200_with_a_reason():
    user, ws, c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB,
        title="done", status=Session.ARCHIVED,
    )
    resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "closing": False, "reason": "already_closed"}


def test_a_non_member_gets_404_not_403():
    user, ws, _c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web"
    )
    other = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    c2 = Client()
    c2.force_login(other)
    assert c2.post(f"/api/canopy-sessions/{s.id}/close").status_code == 404
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_session_close_route.py -v`
Expected: FAIL — 404 on the unrouted `/close` path.

- [ ] **Step 3: Add the route**

In `apps/canopy_sessions/api.py`, directly after the `answer_menu` route:

```python
@router.post("/{session_id}/close", response=dict, summary="Close a session for good")
def close_session(request: HttpRequest, session_id: uuid.UUID):
    """End a session — delete its emdash task if a runner is reporting one, or
    archive it outright if nothing exists on a box.

    `closing: true` means the close was relayed to a runner and the row is still
    listed: the runner deletes the task and its next report retires the session.
    `closing: false` with `ok: true` means it is already done. A refusal is a 200
    with `ok:false` and a stable reason (`unavailable`, `already_closed`), never a
    4xx — same shape `answer-menu` and `reset` use, for the same reason.

    There is deliberately no `unbound` refusal: a session with no binding has
    nothing on a box, which is the second branch rather than an error.
    """
    session = _session_or_404(request, session_id)   # membership gate: non-member -> 404
    outcome = services.close_session(session=session)
    ok = outcome in ("closing", "closed")
    return {"ok": ok, "closing": outcome == "closing", "reason": "" if ok else outcome}
```

- [ ] **Step 4: Refactor `/stop` onto the shared helper**

Replace the body of `stop_session_turn` so the two paths cannot drift:

```python
@router.post("/{session_id}/stop", response=dict, summary="Cancel every non-terminal turn on this session")
def stop_session_turn(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    # Shared with close_session's unreported branch — a closed session must not be
    # woken by a turn that was still queued, and the "all non-terminal turns, and
    # not via any()" reasoning belongs in one place.
    return {"cancelled": services.cancel_session_turns(session)}
```

Delete the now-unused `Turn` / `harness_services` imports in `api.py` **only if nothing else in the file uses them** — check with `grep -n "harness_services\.\|Turn\." apps/canopy_sessions/api.py` first.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_session_close_route.py tests/test_chat_api.py tests/test_session_archive_routes.py -v`
Expected: all PASS.

- [ ] **Step 6: Regenerate the OpenAPI types**

With the backend running on :8000: `cd frontend && npm run gen:api`
(or `npm run gen:api:local` against a dumped `openapi.json`).
Confirm `frontend/src/api/generated.ts` now contains a `/api/canopy-sessions/{session_id}/close` path.

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/api.py frontend/src/api/generated.ts tests/test_session_close_route.py
git commit -m "feat(sessions): POST /api/canopy-sessions/{id}/close

closing:true means relayed and still listed — the runner's report retires it.
/stop now shares cancel_session_turns with close's unreported branch so the two
cannot drift."
```

---

### Task 4: The CDP `close-task` command

**Files:**
- Create (temporarily): `runner/canopy_runner/canopy_runner/cdp/probe-close.mjs`
- Modify: `runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs`
- Modify: `runner/canopy_runner/canopy_runner/cdp_control.py`
- Test: `runner/canopy_runner/tests/test_cdp_control.py`

**Interfaces:**
- Consumes: the sidecar's existing `scrollToFind` / `clickLabel` / `openTask` helpers.
- Produces: `cdp_control.close_task(task: str, *, port: int = 9222) -> dict` returning `{"ok": True, "action": "deleted" | "absent"}`, raising `CDPError` otherwise.

> **This is the one task that cannot be verified from this repo.** emdash's DOM is
> not ours. Do Step 1 against a live emdash before writing anything.

- [ ] **Step 1: Probe the live emdash for the delete affordance**

Launch emdash with `--remote-debugging-port=9222` and at least one throwaway task in the sidebar. Create `runner/canopy_runner/canopy_runner/cdp/probe-close.mjs` (it must live in this directory so `playwright-core` resolves from the sidecar's own `node_modules`):

```javascript
// TEMPORARY probe — delete after recording its output. Dumps every interactive
// affordance reachable from a task row, so `close-task` can be written against
// what emdash actually renders rather than a guess.
import { chromium } from 'playwright-core';

const task = process.argv[2];
const port = process.argv[3] || 9222;
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
const page = browser.contexts()[0]?.pages()[0];
if (!page) { console.log('no renderer page'); process.exit(1); }

const describe = (sel) => page.evaluate((s) => {
  const seen = [...document.querySelectorAll(s)].map(e => ({
    tag: e.tagName,
    label: e.getAttribute('aria-label'),
    title: e.getAttribute('title'),
    text: (e.textContent || '').trim().slice(0, 60),
    cls: (e.className || '').toString().slice(0, 80),
  }));
  return seen;
}, sel);

// 1. The row itself, and anything inside it.
const row = await page.evaluate((t) => {
  const btn = [...document.querySelectorAll('button')]
    .find(b => b.getAttribute('aria-label') === `Open task ${t}`);
  if (!btn) return null;
  const host = btn.closest('li,[role=listitem],div');
  return host ? host.outerHTML.slice(0, 3000) : btn.outerHTML.slice(0, 3000);
}, task);
console.log('--- ROW HTML ---\n', row);

// 2. Hover it — emdash may only render the control on hover.
await page.evaluate((t) => {
  const btn = [...document.querySelectorAll('button')]
    .find(b => b.getAttribute('aria-label') === `Open task ${t}`);
  btn?.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
  btn?.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
}, task);
await page.waitForTimeout(600);
console.log('--- BUTTONS AFTER HOVER ---\n',
  JSON.stringify(await describe('button'), null, 1).slice(0, 6000));

// 3. Right-click it — a context menu is the other likely shape.
await page.evaluate((t) => {
  const btn = [...document.querySelectorAll('button')]
    .find(b => b.getAttribute('aria-label') === `Open task ${t}`);
  btn?.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
}, task);
await page.waitForTimeout(600);
console.log('--- MENU/DIALOG AFTER RIGHT-CLICK ---\n',
  JSON.stringify(await describe('[role=menu] *, [role=menuitem], [role=dialog] button'), null, 1).slice(0, 6000));

await browser.close();
```

Run it from the repo root:

```bash
node runner/canopy_runner/canopy_runner/cdp/probe-close.mjs "<a throwaway task name>" 9222
```

Record, in the commit message for this task, the exact affordance you found: the
`aria-label` / text of the delete control, whether it needs a hover or a
right-click first, and whether a confirmation dialog follows (and its confirm
button's text). **Everything below is written against that finding — substitute
what the probe actually returned.**

- [ ] **Step 2: Write the failing Python test**

In `runner/canopy_runner/tests/test_cdp_control.py`, append:

```python
def test_close_task_passes_the_task_and_port_through(monkeypatch):
    calls = {}

    def fake_run(command, args, **kwargs):
        calls["command"] = command
        calls["args"] = args
        return {"ok": True, "action": "deleted"}

    monkeypatch.setattr(cdp_control, "_run", fake_run)
    assert cdp_control.close_task("ddd", port=9333) == {"ok": True, "action": "deleted"}
    assert calls["command"] == "close-task"
    assert calls["args"] == {"task": "ddd", "port": 9333}


def test_close_task_reports_an_already_gone_task_as_absent(monkeypatch):
    """A double-tap from the phone, or a task a human just deleted in emdash, both
    land here. Neither is a failure — the desired state already holds."""
    monkeypatch.setattr(
        cdp_control, "_run", lambda c, a, **k: {"ok": True, "action": "absent"}
    )
    assert cdp_control.close_task("gone")["action"] == "absent"
```

> Check the top of the existing file for how it imports and monkeypatches `_run`
> and mirror that exactly; if `_run` is patched by a different name there, use theirs.

- [ ] **Step 3: Run it and watch it fail**

Run (from `runner/canopy_runner`): `uv run --with pytest pytest tests/test_cdp_control.py -v`
Expected: FAIL — `AttributeError: module 'canopy_runner.cdp_control' has no attribute 'close_task'`.

- [ ] **Step 4: Add the sidecar command**

In `runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs`, add a branch beside `interrupt` (substitute the selectors your probe found):

```javascript
  } else if (command === 'close-task') {
    // Delete `task` from the sidebar. emdash offers DELETE only — there is no
    // archive, which is why the server's `archived:` closing signal has never had
    // a producer until now.
    //
    // ABSENT IS SUCCESS, not TASK_NOT_FOUND. Unlike open-send, where absence means
    // "we must not create a duplicate", here the caller wants the task gone and it
    // already is — a double-tap from the phone and a human who just deleted it in
    // emdash both land here.
    const { task } = args;
    const found = await scrollToFind(`Open task ${task}`);
    if (!found) { out({ ok: true, action: 'absent' }); }
    else {
      // <<< SUBSTITUTE: whatever the probe found. If it is a hover-revealed
      // button, dispatch mouseover/mouseenter on the row first, then click the
      // control by its aria-label. If it is a context menu, dispatch contextmenu
      // then click the menuitem by text. >>>
      const opened = await page.evaluate((t) => {
        const btn = [...document.querySelectorAll('button')]
          .find(x => x.getAttribute('aria-label') === `Open task ${t}`);
        if (!btn) return false;
        btn.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
        btn.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
        btn.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
        return true;
      }, task);
      if (!opened) fail(`could not reach the controls for task "${task}"`);
      await page.waitForTimeout(400);

      const clicked = await page.evaluate(() => {
        const item = [...document.querySelectorAll('[role=menuitem], button')]
          .find(e => /^\s*delete\b/i.test(e.textContent || '')
                  || /delete/i.test(e.getAttribute('aria-label') || ''));
        if (!item) return false; item.click(); return true;
      });
      if (!clicked) fail(`no delete control for task "${task}" — emdash's UI may have changed; re-run probe-close.mjs`);
      await page.waitForTimeout(500);

      // Confirmation dialog, if emdash shows one. Absence of a dialog is fine.
      await page.evaluate(() => {
        const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
        if (!dlg) return;
        const yes = [...dlg.querySelectorAll('button')]
          .find(b => /delete|confirm|yes/i.test(b.textContent || '')
                  && !/cancel|close/i.test(b.textContent || ''));
        yes?.click();
      });
      await page.waitForTimeout(900);

      // VERIFY. The whole design rests on this: the server wrote nothing, so a
      // close we merely attempted must not be reported as done.
      const gone = !(await scrollToFind(`Open task ${task}`));
      if (!gone) fail(`task "${task}" is still in the sidebar after the delete`);
      out({ ok: true, action: 'deleted' });
    }
```

Also add the command to the header comment block at the top of the file, alongside `interrupt`:

```javascript
//   close-task {task}                 -> {ok, action:"deleted"|"absent"} DELETES the task
//                                        from emdash (there is no archive). Verifies it is
//                                        gone before reporting success; "absent" means it
//                                        already was.
```

- [ ] **Step 5: Add the Python wrapper**

In `runner/canopy_runner/canopy_runner/cdp_control.py`, after `interrupt`:

```python
def close_task(task: str, *, port: int = 9222) -> dict:
    """DELETE `task` from emdash. Returns {"action": "deleted"} or {"action": "absent"}.

    emdash offers delete only — there is no archive — so this is what "close" means
    for a local session, and it is not undoable in emdash. It is not destructive to
    the record: canopy keeps the Session, its Turns and their ledger, and Claude
    Code's transcript (under ~/.claude/projects, resolved by path and never deleted
    by Claude Code), so the conversation stays readable and re-derivable.

    "absent" is SUCCESS, not TASK_NOT_FOUND: a double-tap from the phone and a task
    a human just deleted both land here, and the desired state already holds. This is
    the opposite of `open_and_send`, where absence means "do not create a duplicate".

    The sidecar re-checks the sidebar before reporting "deleted". That verification
    is load-bearing: the server writes nothing when it relays a close, so a close we
    only attempted must never be reported as done.
    """
    return _run("close-task", {"task": task, "port": port})
```

- [ ] **Step 6: Run the tests and syntax-check the sidecar**

Run: `node --check runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs`
Run (from `runner/canopy_runner`): `uv run --with pytest pytest tests/test_cdp_control.py -v`
Expected: clean, then all PASS.

- [ ] **Step 7: Verify against the live emdash**

Create a throwaway task in emdash, then from the repo root:

```bash
uv run python -c "
from runner.canopy_runner.canopy_runner import cdp_control
print(cdp_control.close_task('<throwaway task name>'))
print(cdp_control.close_task('<throwaway task name>'))
"
```

Expected: `{'ok': True, 'action': 'deleted'}` then `{'ok': True, 'action': 'absent'}`, and the task is gone from the sidebar. If the import path fails, run the sidecar directly instead:
`node runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs close-task '{"task":"<name>"}'`

- [ ] **Step 8: Delete the probe and commit**

```bash
rm runner/canopy_runner/canopy_runner/cdp/probe-close.mjs
git add runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs runner/canopy_runner/canopy_runner/cdp_control.py runner/canopy_runner/tests/test_cdp_control.py
git commit -m "feat(runner): close-task — delete an emdash task over CDP

emdash offers delete only, so this is what closing a local session means.
Verifies the task is gone before reporting success: the server writes nothing
when it relays a close, so an attempted close must never read as a done one.
Absent is success — a double-tap and a hand-deleted task both land here.

emdash affordance found by probe: <RECORD IT HERE>"
```

---

### Task 5: The runner half — delete, then tell the server

**Files:**
- Create: `runner/canopy_runner/canopy_runner/close.py`
- Modify: `runner/canopy_runner/canopy_runner/sessions.py`
- Modify: `runner/canopy_runner/canopy_runner/main.py` (`_on_control`, ~line 616)
- Test: `runner/canopy_runner/tests/test_close_session.py`

**Interfaces:**
- Consumes: `cdp_control.close_task` (Task 4); the `close_session` control frame (Task 2).
- Produces:
  - `sessions.request_close_report(task_name: str) -> None`
  - `close.close_session(session_key: str, *, cdp_port: int = 9222) -> str` returning the CDP action (`"deleted"` / `"absent"`).

- [ ] **Step 1: Write the failing tests**

Create `runner/canopy_runner/tests/test_close_session.py`:

```python
"""Closing, from the runner's side: delete the emdash task, then TELL the server.

Telling matters. The server wrote nothing when it relayed the close, and absence
alone takes SESSION_LIVE_WINDOW (3 min) to retire a row — the `archived:` closing
signal says it in one report."""
from canopy_runner import close, sessions


def test_a_close_queues_the_task_for_the_closing_signal(monkeypatch):
    sessions._PENDING_CLOSED.clear()
    monkeypatch.setattr(close.cdp_control, "close_task",
                        lambda t, port=9222: {"ok": True, "action": "deleted"})
    assert close.close_session("ddd") == "deleted"
    assert sessions._PENDING_CLOSED == {"ddd"}


def test_an_already_absent_task_still_queues_the_signal(monkeypatch):
    """The task is gone but the server may not know: a human deleted it in emdash
    between the phone rendering the list and the tap landing."""
    sessions._PENDING_CLOSED.clear()
    monkeypatch.setattr(close.cdp_control, "close_task",
                        lambda t, port=9222: {"ok": True, "action": "absent"})
    assert close.close_session("gone") == "absent"
    assert sessions._PENDING_CLOSED == {"gone"}


def test_a_failed_delete_queues_nothing(monkeypatch):
    """The row must stay where it is. Nothing was written server-side, so doing
    nothing here is already the correct outcome — reporting the close would be the
    only way to get it wrong."""
    sessions._PENDING_CLOSED.clear()

    def boom(task, port=9222):
        raise close.cdp_control.CDPError("no delete control")

    monkeypatch.setattr(close.cdp_control, "close_task", boom)
    try:
        close.close_session("ddd")
    except close.cdp_control.CDPError:
        pass
    else:
        raise AssertionError("expected the CDP error to propagate to the caller")
    assert sessions._PENDING_CLOSED == set()


def test_a_pending_close_forces_a_report_even_when_nothing_changed(monkeypatch):
    """The report is change-driven plus a heartbeat. A close must not wait out the
    heartbeat — that is the latency the whole relay design is trying to avoid."""
    sessions._PENDING_CLOSED.clear()
    sent = {}

    class _Client:
        def report_sessions(self, runner_id, payload, archived=None):
            sent["archived"] = archived
            sent["sessions"] = payload

    cfg = _cfg(monkeypatch)
    monkeypatch.setattr(sessions.emdash, "list_open_sessions", lambda *a, **k: [])
    monkeypatch.setattr(sessions.emdash, "list_recently_archived_tasks", lambda *a, **k: [])
    monkeypatch.setattr(sessions, "session_changed", lambda *a, **k: False)
    sessions.request_close_report("ddd")
    sessions.maybe_report_sessions(cfg, _Client(), now_fn=lambda: 0.0)
    assert sent["archived"] == ["ddd"]
    assert sessions._PENDING_CLOSED == set()
```

Add the `_cfg` helper at the top of the file, copying whatever the existing
`runner/canopy_runner/tests/test_session_report_live.py` uses to build a `Config`
(read it first — reuse its fixture verbatim rather than inventing one):

```python
def _cfg(monkeypatch):
    # Copy the Config construction from tests/test_session_report_live.py.
    ...
```

- [ ] **Step 2: Run them and watch them fail**

Run (from `runner/canopy_runner`): `uv run --with pytest pytest tests/test_close_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canopy_runner.close'`.

- [ ] **Step 3: Add the pending-close queue to `sessions.py`**

Near the other module-level state (`_last_session_report`):

```python
# Task names deleted by a `close_session` control frame, waiting to ride the next
# report's `archived:` list.
#
# Queued rather than POSTed on its own because the report is WHOLESALE: the server
# reconciles `archived` against the open set in ONE call, and `now_keys` must win
# (emdash task names are not unique, so an open task must never be retired by a
# closed namesake — apps/harness/services.py). Sending the closing signal separately
# would throw that ordering away.
_PENDING_CLOSED: set[str] = set()


def request_close_report(task_name: str) -> None:
    """Queue a deleted task's name for the next report's closing signal, and make
    that report happen on the very next tick rather than at the next heartbeat.

    Without this the row would wait out SESSION_LIVE_WINDOW (3 min) on absence
    alone — the latency the relay design exists to avoid.
    """
    _PENDING_CLOSED.add(task_name)
```

In `maybe_report_sessions`, change the change-check:

```python
    changed = session_changed(cfg, sessions) or bool(_PENDING_CLOSED)
```

and fold the queue into the outgoing `archived` list, clearing it only after a
successful POST:

```python
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        client.report_sessions(cfg.runner_id, sessions, sorted(set(archived) | _PENDING_CLOSED))
        # Cleared only on success. A dropped POST must not lose the closing signal —
        # the next tick retries it, and re-reporting an already-retired name is a
        # no-op server-side.
        _PENDING_CLOSED.clear()
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)
```

- [ ] **Step 4: Add `close.py`**

Create `runner/canopy_runner/canopy_runner/close.py`:

```python
"""Closing a session from the web: delete the emdash task, then TELL the server.

The server wrote NOTHING when it relayed the close — for a local session the
emdash task is the truth, and `replace_reported_sessions` would un-archive a
server-side write within ~10s anyway. So this module's report is the answer:
`sessions.request_close_report` puts the task name on the next report's
`archived:` list, which apps/harness/services.py turns into status=ARCHIVED.
Absence alone would say the same thing three minutes later.

The consequence of writing nothing server-side is that a FAILED close needs no
cleanup: the task keeps being reported, the row stays, and the list is telling
the truth. Signal only what actually happened.
"""
from __future__ import annotations

import logging

from . import cdp_control, sessions

logger = logging.getLogger(__name__)


def close_session(session_key: str, *, cdp_port: int = 9222) -> str:
    """Delete `session_key`'s emdash task and queue its closing signal.

    Returns the CDP action — "deleted", or "absent" when the task was already gone
    (a double-tap, or a human who deleted it in emdash a moment earlier). Both
    queue the signal: the task is gone either way, and the server may not know.

    Raises CDPError if the delete could not be completed. The caller logs it and
    moves on; nothing needs undoing.
    """
    result = cdp_control.close_task(session_key, port=cdp_port)
    action = str(result.get("action") or "deleted")
    sessions.request_close_report(session_key)
    logger.info("closed emdash task %s (%s)", session_key, action)
    return action
```

- [ ] **Step 5: Route the control frame in `main.py`**

In `_on_control`, after the `menu_answer` branch:

```python
        elif msg.get("type") == "close_session" and msg.get("session_key"):
            # A human closed this session from the web. Runs on the wake-listener
            # thread and must never raise: this socket also carries cancel and wake,
            # and losing it would cost the runner its liveness for one delete.
            try:
                close.close_session(str(msg["session_key"]), cdp_port=cfg.cdp_port)
            except Exception:  # noqa: BLE001
                logger.warning("close failed for %s", msg.get("session_key"),
                               exc_info=True)
```

Add `close` to the module imports at the top of `main.py`, matching how `hooks`
and `sessions` are imported there.

- [ ] **Step 6: Run the tests**

Run (from `runner/canopy_runner`): `uv run --with pytest pytest tests/test_close_session.py tests/test_session_report_live.py tests/test_main.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add runner/canopy_runner/canopy_runner/close.py runner/canopy_runner/canopy_runner/sessions.py runner/canopy_runner/canopy_runner/main.py runner/canopy_runner/tests/test_close_session.py
git commit -m "feat(runner): act on a close_session frame — delete the task, then signal it

The closing signal finally has a producer. A runner that deliberately deleted a
task knows it closed it, so it says so on the next report instead of leaving the
server to infer it from three minutes of absence. A failed delete signals nothing
and needs no cleanup, because nothing was written server-side."
```

> **Deploy note for whoever ships this:** the laptop runner is an installed
> package, not a checkout. After merge + deploy, the auto-updater picks this up
> within 30 minutes; to take it immediately, re-run
> `runner/canopy_runner/scripts/install-runner.sh` (not mid-turn — it restarts the
> daemon).

---

### Task 6: Frontend — the API call and the pure decision

**Files:**
- Modify: `frontend/src/api/chat.ts`
- Create: `frontend/src/components/chat/closeAction.ts`
- Test: `frontend/src/components/chat/closeAction.test.ts`

**Interfaces:**
- Consumes: `POST /api/canopy-sessions/{id}/close` (Task 3); the `ChatSession` type already exported from `chat.ts`.
- Produces:
  - `closeSession(id: string): Promise<CloseResult>` where `CloseResult = { ok: boolean; closing: boolean; reason: string }`
  - `closeIntent(s): CloseIntent` where `CloseIntent = { kind: 'ready'; confirm: boolean } | { kind: 'blocked'; why: string }`
  - `closeResultMessage(r: CloseResult, s): string | null`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/chat/closeAction.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { closeIntent, closeResultMessage } from './closeAction'

const base = {
  status: 'active',
  running: false,
  runner_online: true,
  runner_status: 'online',
  runner_name: 'jj-mbp',
} as const

describe('closeIntent', () => {
  it('is ready without confirmation for an idle session', () => {
    expect(closeIntent(base)).toEqual({ kind: 'ready', confirm: false })
  })

  it('asks first when the agent is mid-turn', () => {
    expect(closeIntent({ ...base, running: true })).toEqual({ kind: 'ready', confirm: true })
  })

  it('blocks when the bound runner cannot act, naming the box and why', () => {
    const intent = closeIntent({ ...base, runner_online: false, runner_status: 'paused' })
    expect(intent.kind).toBe('blocked')
    expect(intent.kind === 'blocked' && intent.why).toContain('jj-mbp')
    expect(intent.kind === 'blocked' && intent.why).toContain('paused')
  })

  it('blocks an already-closed session', () => {
    expect(closeIntent({ ...base, status: 'archived' }).kind).toBe('blocked')
  })

  it('FAILS OPEN when liveness is merely unknown', () => {
    // runner_online: null means unbound — a cloud session or a web chat that has
    // never sent. Those close server-side and always work. Blocking them would
    // disable the button on exactly the sessions closing is guaranteed to fix.
    expect(closeIntent({ ...base, runner_online: null, runner_status: null }))
      .toEqual({ kind: 'ready', confirm: false })
  })
})

describe('closeResultMessage', () => {
  it('says nothing on success', () => {
    expect(closeResultMessage({ ok: true, closing: true, reason: '' }, base)).toBeNull()
  })

  it('treats an already-closed race as done, not as an error', () => {
    // Double-tap, or someone else closed it first. The row is going away either
    // way; an error toast would be noise about a wish that came true.
    expect(closeResultMessage({ ok: false, closing: false, reason: 'already_closed' }, base))
      .toBeNull()
  })

  it('explains an unreachable runner in terms of the box', () => {
    const msg = closeResultMessage({ ok: false, closing: false, reason: 'unavailable' }, base)
    expect(msg).toContain('jj-mbp')
  })
})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/components/chat/closeAction.test.ts`
Expected: FAIL — cannot resolve `./closeAction`.

- [ ] **Step 3: Write the derivation**

Create `frontend/src/components/chat/closeAction.ts`. It imports `CloseResult`,
which Step 4 adds — this file will not type-check until Step 4 lands, which is why
the tests are not run until Step 5.

```ts
import type { ChatSession, CloseResult } from '@/api/chat'

/**
 * Can this session be closed right now, and should we ask first?
 *
 * Pure and tested without a component, mirroring `chatPageLogic.ts::sendBlockReason`
 * — the decision is the part worth testing, not the markup around it.
 *
 * FAILS OPEN, deliberately: `runner_online === null` means UNBOUND (a cloud session,
 * or a web chat that has never sent), and those close server-side and always
 * succeed. Treating unknown as blocked would disable the button on exactly the
 * sessions closing is guaranteed to work for.
 */
export type CloseSubject = Pick<
  ChatSession,
  'status' | 'running' | 'runner_online' | 'runner_status' | 'runner_name'
>

export type CloseIntent =
  | { kind: 'ready'; confirm: boolean }
  | { kind: 'blocked'; why: string }

export function closeIntent(s: CloseSubject): CloseIntent {
  if (s.status !== 'active') return { kind: 'blocked', why: 'Already closed' }
  if (s.runner_online === false) {
    const box = s.runner_name ?? 'its runner'
    const why = s.runner_status ?? 'offline'
    return { kind: 'blocked', why: `Can’t close — ${box} is ${why}` }
  }
  // Mid-turn is not a block. A session stuck in a loop is precisely when you most
  // want it gone; it just gets one confirmation first.
  return { kind: 'ready', confirm: Boolean(s.running) }
}

/**
 * What to tell the user afterwards, or null for "say nothing".
 *
 * `already_closed` is a success from where the user sits: they wanted it gone and
 * it is gone. It comes back `ok:false` because the API describes what it did, not
 * how the user feels about it — the translation belongs here.
 */
export function closeResultMessage(r: CloseResult, s: CloseSubject): string | null {
  if (r.ok) return null
  if (r.reason === 'already_closed') return null
  if (r.reason === 'unavailable') {
    const box = s.runner_name ?? 'its runner'
    return `Couldn’t close — ${box} is ${s.runner_status ?? 'offline'}`
  }
  return 'Couldn’t close this session'
}
```

- [ ] **Step 4: Add the API call**

In `frontend/src/api/chat.ts`, after `resetSession`:

```ts
export type CloseResult = { ok: boolean; closing: boolean; reason: string };

/**
 * Close a session for good.
 *
 * `closing: true` means it was relayed to a runner and the row is STILL LISTED —
 * the runner deletes the emdash task and its next report retires the session, so
 * the client shows a pending state rather than removing the row itself. Removing
 * it optimistically would be a lie if the delete failed.
 *
 * Never throws on a refusal: `ok: false` with a `reason` to render.
 */
export function closeSession(id: string): Promise<CloseResult> {
  return request<CloseResult>(`/api/canopy-sessions/${encodeURIComponent(id)}/close`, {
    method: "POST",
  });
}
```

- [ ] **Step 5: Run the tests**

Run: `cd frontend && npx vitest run src/components/chat/closeAction.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/components/chat/closeAction.ts frontend/src/components/chat/closeAction.test.ts
git commit -m "feat(chat): closeSession client + the pure close decision

Fails open on unknown liveness — an unbound session closes server-side and
always works, so blocking it would disable the button on exactly the sessions
this is guaranteed to fix. already_closed reads as success to the user."
```

---

### Task 7: Close on a session row

**Files:**
- Modify: `frontend/src/components/chat/ChatSessionsPanel.tsx`
- Test: `frontend/src/components/chat/ChatSessionsPanel.test.tsx`

**Interfaces:**
- Consumes: `closeSession` / `closeIntent` / `closeResultMessage` (Task 6).
- Produces: a `data-testid="close-session-${id}"` button per row.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/components/chat/ChatSessionsPanel.test.tsx` (follow the file's existing render/mock helpers — read them first and reuse them rather than adding new ones):

```tsx
it('closes an idle session without asking', async () => {
  const close = vi.fn().mockResolvedValue({ ok: true, closing: false, reason: '' })
  // wire `close` in via the same module mock the file already uses for chat api calls
  renderPanel([{ ...aSession, id: 's1', running: false }])
  await userEvent.click(await screen.findByTestId('close-session-s1'))
  expect(close).toHaveBeenCalledWith('s1')
})

it('asks first when the agent is mid-turn', async () => {
  const close = vi.fn().mockResolvedValue({ ok: true, closing: true, reason: '' })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  renderPanel([{ ...aSession, id: 's1', running: true }])
  await userEvent.click(await screen.findByTestId('close-session-s1'))
  expect(confirm).toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
})

it('does not navigate into the chat when Close is clicked', async () => {
  // The row is a Link; the close control must not be inside it.
  renderPanel([{ ...aSession, id: 's1' }])
  const btn = await screen.findByTestId('close-session-s1')
  expect(btn.closest('a')).toBeNull()
})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/components/chat/ChatSessionsPanel.test.tsx`
Expected: FAIL — no element with testid `close-session-s1`.

- [ ] **Step 3: Restructure the row and add the control**

The row is currently `<li>{header}<Link>…</Link></li>`. A `<button>` **cannot** live
inside an `<a>` — interactive content nested in an anchor is invalid HTML and swallows
the click. Wrap the link and the button as siblings.

Add `const intent = closeIntent(s)` beside the existing `const parkedWhy = parkedReason(s)`
in the map body, so the derivation runs once per row:

```tsx
              <li key={s.id}>
                {header && (
                  <div className="bg-muted/40 px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {header}
                  </div>
                )}
                {/* Link and Close are SIBLINGS: a button inside an anchor is
                    invalid HTML and the anchor eats the click. */}
                <div className="flex items-stretch">
                  <Link
                    to={`/w/${s.workspace}/chat/${s.id}`}
                    data-testid={parkedWhy ? `session-parked-${s.id}` : undefined}
                    className={`flex min-w-0 flex-1 items-center justify-between gap-3 px-3 py-2.5 hover:bg-muted${
                      parkedWhy ? ' opacity-60' : ''
                    }`}
                  >
                    {/* …existing contents, unchanged… */}
                  </Link>
                  <button
                    type="button"
                    data-testid={`close-session-${s.id}`}
                    aria-label={`Close ${s.title?.trim() || 'Untitled chat'}`}
                    title={intent.kind === 'blocked'
                      ? intent.why
                      : 'Close this session (deletes its emdash task)'}
                    disabled={intent.kind === 'blocked' || closingId === s.id}
                    onClick={() => void onClose(s)}
                    className="shrink-0 px-3 text-muted-foreground hover:text-destructive disabled:opacity-40"
                  >
                    {closingId === s.id ? '…' : '×'}
                  </button>
                </div>
              </li>
```

Add the handler and state near the panel's other state:

```tsx
  const [closingId, setClosingId] = useState<string | null>(null)
  const [closeError, setCloseError] = useState<string | null>(null)

  // The row is NOT removed here. `closing: true` means the close was relayed and
  // the emdash task is still the truth — the row leaves on the next
  // `supervisor.sessions` push, once the runner's report has actually retired it.
  // Removing it optimistically would be a lie whenever the delete failed.
  const onClose = async (s: ChatSession) => {
    const intent = closeIntent(s)
    if (intent.kind === 'blocked') return
    if (intent.confirm && !window.confirm(
      `${s.title?.trim() || 'This chat'} is still working. Close it anyway?`
    )) return
    setClosingId(s.id)
    setCloseError(null)
    try {
      const result = await closeSession(s.id)
      const message = closeResultMessage(result, s)
      if (message) setCloseError(message)
      // The panel has no extracted reload — its load lives inline in a useEffect
      // and a 20s interval refresh. Re-fetch with the SAME state the effects use
      // so a closed row leaves without waiting out the interval.
      else setSessions(await listSessions(showArchived ? 'all' : 'active'))
    } catch {
      setCloseError('Couldn’t close this session')
    } finally {
      setClosingId(null)
    }
  }
```

Render `closeError` beside the existing `error` banner:

```tsx
      {closeError && <div className="py-2 text-sm text-destructive">{closeError}</div>}
```

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run src/components/chat/ChatSessionsPanel.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Type-check the build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/chat/ChatSessionsPanel.tsx frontend/src/components/chat/ChatSessionsPanel.test.tsx
git commit -m "feat(chat): close a session from its row

Lands on /supervisor and /w/:ws/chat at once — the panel is shared. The row is
not removed optimistically: closing:true means the emdash task is still the
truth, so the row leaves when the runner's report says it did."
```

---

### Task 8: Close from an open chat

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx` (header, ~line 558)

**Interfaces:**
- Consumes: `closeSession` / `closeIntent` / `closeResultMessage` (Task 6).
- Produces: nothing downstream.

- [ ] **Step 1: Add the control beside "Reset from transcript"**

In the header's `<div className="ml-auto flex shrink-0 items-center gap-2">`, before the reset button:

```tsx
          {closeNote && <span className="text-[12px] text-muted-foreground">{closeNote}</span>}
          <button
            type="button"
            onClick={() => void closeThisSession()}
            disabled={closing}
            title="Close this session — deletes its emdash task. The transcript is kept."
            className="rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
          >
            {closing ? 'Closing…' : 'Close session'}
          </button>
```

- [ ] **Step 2: Add the handler**

Beside the existing `resetFromTranscript` handler:

```tsx
  const [closing, setClosing] = useState(false)
  const [closeNote, setCloseNote] = useState<string | null>(null)

  // Navigate away only when the session is REALLY gone (`closing: false`). When the
  // close was relayed to a runner the emdash task is still the truth, so stay put
  // and say so — bouncing to the list would claim a result we do not have yet.
  const closeThisSession = async () => {
    if (!meta) return
    const intent = closeIntent(meta)
    if (intent.kind === 'blocked') { setCloseNote(intent.why); return }
    if (intent.confirm && !window.confirm(
      'This chat is still working. Close it anyway?'
    )) return
    setClosing(true)
    setCloseNote(null)
    try {
      const result = await closeSession(id)
      const message = closeResultMessage(result, meta)
      if (message) setCloseNote(message)
      else if (result.closing) setCloseNote('Closing on the runner…')
      else navigate(`/w/${meta.workspace}/chat`)
    } catch {
      setCloseNote('Couldn’t close this session')
    } finally {
      setClosing(false)
    }
  }
```

`meta` is the page's existing `ChatSessionDetail | null` (line 60) and carries
`status` / `running` / `runner_online` / `runner_status` / `runner_name` /
`workspace`; `id` is from `useParams()` (line 59). `navigate` is new — widen the
existing router import on line 2:

```tsx
import { useNavigate, useParams } from 'react-router-dom'
```

and add `const navigate = useNavigate()` beside the `useParams` call.

- [ ] **Step 3: Type-check and run the page's tests**

Run: `cd frontend && npm run build && npx vitest run src/pages/chatPageLogic.test.ts`
Expected: clean, PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx
git commit -m "feat(chat): close the session you are reading

Navigates away only when the close really landed. A relayed close leaves you
here with a note — bouncing to the list would claim a result we do not have."
```

---

## Verification before opening the PR

- [ ] Full backend suite: `uv run pytest`
- [ ] Runner suite: from `runner/canopy_runner`, `uv run --with pytest pytest`
- [ ] Frontend: `cd frontend && npm run build && npx vitest run`
- [ ] Architecture boundary holds: `uv run pytest tests/test_architecture_boundary.py -v`
- [ ] OpenAPI types are fresh: `cd frontend && npm run gen:api` produces no diff
- [ ] **End-to-end on a real box**, since Task 4 is the only unverifiable piece:
  1. Confirm a laptop runner is ONLINE and reporting sessions (`/supervisor` → Runners).
  2. Create a throwaway emdash task; wait for it to appear in `/supervisor` → Sessions.
  3. Tap Close. Expected: `closing` state, then the row leaves within a few seconds, and the task is gone from emdash's sidebar.
  4. Confirm the transcript survives: open the closed session and hit "Reset from transcript" — the conversation rebuilds.
  5. Create a **cloud** chat, send one message, then Close it. Expected: the row leaves immediately and stays gone.
- [ ] `gh pr merge <n> --auto` (no strategy flag), then `gh pr view <n> --json autoMergeRequest` to confirm it armed.
