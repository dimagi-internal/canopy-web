"""CDP executor — resolve → reuse-or-create, runner owns the routing lifecycle."""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import cdp_control, dialog, emdash, execute


def _cfg():
    # A real (but throwaway) state_path — execute_turn's fail/finish paths now write a
    # readiness marker next to it (see readiness.py); without one, readiness would fall
    # back to the real ~/.canopy and pollute the developer's machine during a test run.
    # Fresh tmp dir per call keeps every test's marker isolated from the others.
    tmp = Path(tempfile.mkdtemp(prefix="canopy-runner-test-"))
    return SimpleNamespace(cdp_port=9222, runner_id="r-1", emdash_db="/fake/emdash4.db",
                           state_path=str(tmp / "runner-state.json"))


@pytest.fixture(autouse=True)
def _db_says_live(monkeypatch):
    """Default: sqlite reports the linked task present. Tests that care override it."""
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")


class FakeClient:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []          # method log
        self.events = []
        self.started = []
        self.finished = []
        self.failed = []
        self.recorded = []

    def resolve_session(self, runner_id, agent, thread_key, *, project="", workspace=""):
        self.calls.append(("resolve", agent, thread_key, project, workspace))
        return dict(self.plan)

    def start(self, turn_id, session_id=""):
        self.started.append(turn_id)

    def finish(self, turn_id, note=""):
        self.finished.append((turn_id, note))

    def fail_turn(self, turn_id, note):
        self.failed.append((turn_id, note))

    def post_events(self, turn_id, events):
        self.events.append((turn_id, events))

    def record_session(self, runner_id, agent, thread_key, **kw):
        self.recorded.append((agent, thread_key, kw))



def _turn(**kw):
    d = {"id": "t-1", "agent_slug": "hal", "origin_ref": {}, "prompt": "do the thing"}
    d.update(kw)
    return d


def test_reuse_sends_into_existing_session(monkeypatch):
    sent = {}
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda task, text, port=9222: sent.update(task=task, text=text) or {"ok": True})
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda *a, **k: pytest.fail("must NOT create when reusing"))
    client = FakeClient({"reuse": True, "emdash_task_id": "shaky-baths-listen", "summary": ""})
    turn = _turn(origin_ref={"thread_id": "thr-1"})
    result = execute.execute_turn(_cfg(), client, "r-1", turn)
    assert result == "reused:t-1"
    assert sent == {"task": "shaky-baths-listen", "text": "do the thing"}
    assert client.started == ["t-1"] and client.finished and "existing session" in client.finished[0][1]


@pytest.mark.parametrize("state", ["archived", "absent"])
def test_db_says_gone_creates_fresh_without_asking_the_dom(monkeypatch, state):
    """sqlite is the truth for existence. A genuinely-gone task creates + rehydrates —
    and never pays for a CDP round trip to learn what one query already answered."""
    monkeypatch.setattr(emdash, "task_state", lambda db, name: state)
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda *a, **k: pytest.fail("must not touch the DOM once sqlite says gone"))
    created = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, task_name="", port=9222: created.update(project=project, prompt=prompt) or {"task": "new-task-x"})
    client = FakeClient({"reuse": True, "emdash_task_id": "gone-one", "summary": "prior ctx"})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result.startswith("created:t-1:new-task-x")
    assert created["project"] == "hal"
    assert "prior ctx" in created["prompt"]      # rehydrated on fallback


def test_dom_not_found_never_duplicates_a_task_sqlite_says_is_live(monkeypatch):
    """THE eva org-research bug (2026-07-15). The sidebar virtualizes, so a live task
    scrolled out of view reports TASK_NOT_FOUND. Trusting that spawned a cold duplicate
    and orphaned the real session's context. sqlite outranks the DOM: fail, don't fork."""
    def not_found(task, text, port=9222):
        raise cdp_control.CDPError('TASK_NOT_FOUND: no task "eva-org-research-790c-0715-1352"')
    monkeypatch.setattr(cdp_control, "open_and_send", not_found)
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda *a, **k: pytest.fail("must NOT duplicate a live session"))
    client = FakeClient({"reuse": True, "emdash_task_id": "eva-org-research-790c-0715-1352",
                         "summary": "ctx"})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result == "failed:t-1"
    assert client.recorded == []      # link still points at the original session


def test_unreadable_db_degrades_to_the_dom_verdict(monkeypatch):
    """No truth available (db missing/misconfigured) — don't wedge every turn; fall back
    to the legacy CDP verdict, which is no worse than the pre-sqlite behaviour."""
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "unknown")
    def not_found(task, text, port=9222):
        raise cdp_control.CDPError('TASK_NOT_FOUND: no task "x"')
    monkeypatch.setattr(cdp_control, "open_and_send", not_found)
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, task_name="", port=9222: {"task": "new-x"})
    client = FakeClient({"reuse": True, "emdash_task_id": "x", "summary": ""})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result.startswith("created:t-1:new-x")


def test_transient_reuse_send_failure_never_duplicates(monkeypatch):
    """The bug that spawned two Hal sessions: a send glitch on an EXISTING task must
    fail the turn, NOT create a duplicate + re-point the link."""
    def glitch(task, text, port=9222):
        raise cdp_control.CDPError("locator.click: Timeout 30000ms exceeded")  # not TASK_NOT_FOUND
    monkeypatch.setattr(cdp_control, "open_and_send", glitch)
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda *a, **k: pytest.fail("must NOT create a duplicate on a transient send failure"))
    client = FakeClient({"reuse": True, "emdash_task_id": "live-session", "summary": "ctx"})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result == "failed:t-1"
    assert client.failed and "not spawning a duplicate" in client.failed[0][1]
    assert client.recorded == []   # link NOT re-pointed — the original session stays canonical


def _collision_then(second_action="sent-cleared"):
    """A stateful open_and_send: first call returns a collision, the clear_first re-call
    returns `second_action`. Records the calls so a test can assert the second was a clear."""
    calls = []

    def send(task, text, clear_first=False, port=9222):
        calls.append({"task": task, "text": text, "clear_first": clear_first})
        if not clear_first and len(calls) == 1:
            return {"ok": True, "action": "collision", "task": task,
                    "line": "and then we should also che"}
        return {"ok": True, "action": second_action, "task": task}

    return send, calls


def test_collision_clear_and_send_stays_in_the_existing_session(monkeypatch):
    """Human chose 'Clear & send': the leaked text is cleared and the turn lands in the
    SAME session (a re-call with clear_first), and the link is recorded — no new session."""
    send, calls = _collision_then("sent-cleared")
    monkeypatch.setattr(cdp_control, "open_and_send", send)
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda *a, **k: pytest.fail("Clear & send must NOT create a new session"))
    monkeypatch.setattr(dialog, "collision_choice", lambda task, line, **k: dialog.CLEAR)
    client = FakeClient({"reuse": True, "emdash_task_id": "busy-session", "summary": ""})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result == "reused:t-1"
    assert [c["clear_first"] for c in calls] == [False, True]   # detect, then clear-and-send
    assert client.recorded and client.recorded[0][2]["emdash_task_id"] == "busy-session"


def test_collision_new_session_leaves_prompt_and_creates_fresh(monkeypatch):
    """Human chose 'New session' (also the timeout default): the existing prompt is left
    untouched (no clear re-call) and the turn routes to a NEW session."""
    send, calls = _collision_then()
    monkeypatch.setattr(cdp_control, "open_and_send", send)
    created = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, task_name="", port=9222: created.update(project=project) or {"task": "fresh-one"})
    monkeypatch.setattr(dialog, "collision_choice", lambda task, line, **k: dialog.NEW)
    client = FakeClient({"reuse": True, "emdash_task_id": "busy-session", "summary": "prior ctx"})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result.startswith("created:t-1:fresh-one")
    assert [c["clear_first"] for c in calls] == [False]         # detected, never cleared
    assert created["project"] == "hal"


def test_collision_cancel_defers_the_turn_without_touching_anything(monkeypatch):
    """Human chose 'Cancel': deliver nothing, don't create, requeue for a later tick."""
    send, calls = _collision_then()
    monkeypatch.setattr(cdp_control, "open_and_send", send)
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda *a, **k: pytest.fail("Cancel must NOT create a session"))
    monkeypatch.setattr(dialog, "collision_choice", lambda task, line, **k: dialog.CANCEL)
    client = FakeClient({"reuse": True, "emdash_task_id": "busy-session", "summary": ""})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result == "deferred:t-1"
    assert [c["clear_first"] for c in calls] == [False]         # only the detect call
    assert client.failed and "cancelled by human" in client.failed[0][1]
    assert client.recorded == []                               # link untouched


def test_create_new_thread_rehydrates_from_summary(monkeypatch):
    created = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, task_name="", port=9222: created.update(prompt=prompt) or {"task": "fresh"})
    # other-account plan: reuse False but summary present
    client = FakeClient({"reuse": False, "new_thread": False, "emdash_task_id": "etask-A",
                         "summary": "what account A did"})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn(origin_ref={"thread_id": "thr-1"}))
    assert result.startswith("created:t-1:fresh")
    assert "what account A did" in created["prompt"]
    assert client.recorded and client.recorded[0][0] == "hal"


def test_create_failure_fails_the_turn(monkeypatch):
    def boom(project, prompt, task_name="", port=9222):
        raise cdp_control.CDPError("emdash not on debug port")
    monkeypatch.setattr(cdp_control, "create_task", boom)
    client = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    result = execute.execute_turn(_cfg(), client, "r-1", _turn())
    assert result == "failed:t-1"
    assert client.failed and "emdash create failed" in client.failed[0][1]


def test_keyless_turn_gets_a_fresh_per_turn_key(monkeypatch):
    """A turn with no explicit thread_key/thread_id is a self-contained unit of work (a
    cron fire, a board turn). It must NOT collapse onto a shared '{agent}:main' sink —
    that piled every keyless turn (all of an agent's cron fires, all board dispatches)
    into one ever-growing session. It keys on the turn's own id, so each opens fresh."""
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: {"task": "x"})
    client = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    execute.execute_turn(_cfg(), client, "r-1", _turn(id="t-1", origin_ref={}))
    assert client.calls[0] == ("resolve", "hal", "hal:t-1", "", "")


def test_two_keyless_turns_get_distinct_keys(monkeypatch):
    """The core anti-collision property: different keyless turns never share a session."""
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: {"task": "x"})
    c1 = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    c2 = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    execute.execute_turn(_cfg(), c1, "r-1", _turn(id="t-1", origin_ref={}))
    execute.execute_turn(_cfg(), c2, "r-1", _turn(id="t-2", origin_ref={}))
    assert c1.calls[0][2] == "hal:t-1" and c2.calls[0][2] == "hal:t-2"
    assert c1.calls[0][2] != c2.calls[0][2]


def test_explicit_thread_key_still_continues_one_session(monkeypatch):
    """Continuity is opt-in and preserved: an explicit thread_key/thread_id is honored
    verbatim (the phone's persistent thread, a 'continue this session' dispatch)."""
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: {"task": "x"})
    client = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    execute.execute_turn(_cfg(), client, "r-1",
                         _turn(id="t-1", origin_ref={"thread_key": "phone:jj:hal"}))
    assert client.calls[0] == ("resolve", "hal", "phone:jj:hal", "", "")


def test_a_project_turn_drives_the_repo_and_carries_its_tenant(monkeypatch):
    """A repo turn (agent_slug None, project set) resolves + drives against the
    project name, and threads project + workspace to the session-link calls so
    the durable link is tenant-scoped. cdp_control is unchanged — it always took
    a project name."""
    created = {}
    monkeypatch.setattr(
        cdp_control, "create_task",
        lambda project, prompt, **k: created.update(project=project, prompt=prompt) or {"task": "ct"},
    )
    client = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    turn = {
        "id": "t-9", "agent_slug": None, "project": "canopy-web",
        "workspace_slug": "canopy", "origin_ref": {}, "prompt": "fix the header",
    }

    result = execute.execute_turn(_cfg(), client, "r-1", turn)

    assert result == "created:t-9:ct"
    # resolve keyed on the repo (fresh-per-turn via the turn id), tagged with project + workspace
    assert client.calls[0] == ("resolve", "", "canopy-web:t-9", "canopy-web", "canopy")
    # the CDP create drove the repo as its emdash project, with the composer's prompt
    assert created == {"project": "canopy-web", "prompt": "fix the header"}
    # the durable link was recorded with the repo's tenant
    agent_arg, thread_key, kw = client.recorded[0]
    assert agent_arg == ""
    assert kw["project"] == "canopy-web" and kw["workspace"] == "canopy"


def test_task_name_is_readable_subject_plus_stamp():
    import datetime as dt
    now = dt.datetime(2026, 7, 14, 15, 32)
    # keyless turn -> thread key is '{agent}:{turn_id}', so the discriminator comes
    # from the turn id (last 4 of 'halt1') rather than a shared 'main'.
    t = _turn(id="t-1", origin="email", origin_ref={"subject": "Re: Bednet demo!!"})
    assert execute._task_name("hal", t, now=now) == "hal-re-bednet-demo-alt1-0714-1532"
    # no subject -> agent + stamp
    assert execute._task_name("hal", _turn(id="t-1", origin="manual", origin_ref={}), now=now) == "hal-manual-alt1-0714-1532"


def test_task_name_distinguishes_threads_with_same_subject():
    """The observed bug: two DIFFERENT threads with the same subject in the same minute
    got the same name. The thread discriminator must keep them distinct."""
    import datetime as dt
    now = dt.datetime(2026, 7, 14, 15, 14)
    t1 = _turn(origin="email", origin_ref={"subject": "Security alert", "thread_id": "19f4c06eeb986355"})
    t2 = _turn(origin="email", origin_ref={"subject": "Security alert", "thread_id": "19f425675a9855a4"})
    n1 = execute._task_name("hal", t1, now=now)
    n2 = execute._task_name("hal", t2, now=now)
    assert n1 == "hal-security-alert-6355-0714-1514"
    assert n2 == "hal-security-alert-55a4-0714-1514"
    assert n1 != n2
