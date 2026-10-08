"""A session canopy created that never started is reported as waiting on you (#1190).

The case behind this: on 2026-10-06 a dispatched turn sat for 16 minutes on Claude
Code's "Claude in Chrome extension detected" startup dialog. There was no hook
menu, no transcript, and canopy-web showed it as idle.
"""
import json

import pytest

from canopy_runner import emdash, startup_watch, transcript
from canopy_runner import sessions as sessions_mod

CREATED = 1_000.0


def _session(task="c-goal-6a8f", project="hal"):
    return {"emdash_task": task, "project": project}


def _attach(sessions, *, now, started=()):
    startup_watch.attach_stall_markers(
        sessions, has_transcript=lambda p, t: t in started, now=lambda: now)


def test_a_created_session_with_no_transcript_is_waiting_on_you_after_the_stall_window():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)

    early = [_session()]
    _attach(early, now=CREATED + startup_watch.STALL_AFTER_S - 1)
    assert "question" not in early[0]

    late = [_session()]
    _attach(late, now=CREATED + startup_watch.STALL_AFTER_S + 600)
    q = late[0]["question"]
    assert q["options"] == []          # words, not buttons: it does not know the dialog
    assert q["source"] == "startup"
    assert "emdash" in q["question"]


def test_the_marker_is_identical_on_every_report():
    """The server compares the whole dict on each report and treats any change as a
    new menu (a session.menu frame, possibly a push). The marker must not change from
    one report to the next."""
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    a, b = [_session()], [_session()]
    _attach(a, now=CREATED + 200)
    _attach(b, now=CREATED + 2_000)
    assert a[0]["question"] == b[0]["question"]


def test_a_session_that_started_is_never_marked_and_stops_being_watched():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    s = [_session()]
    _attach(s, now=CREATED + 600, started={"c-goal-6a8f"})
    assert "question" not in s[0]
    assert startup_watch._created == {}


def test_a_session_canopy_did_not_create_is_never_marked():
    """A task a human opened and has not typed into has no transcript either."""
    s = [_session(task="bednet")]
    _attach(s, now=CREATED + 10_000)
    assert "question" not in s[0]


def test_a_real_menu_is_never_overwritten():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    menu = {"question": "Delete it?", "options": [{"number": 1, "label": "Yes"}]}
    s = [{**_session(), "question": menu}]
    _attach(s, now=CREATED + 600)
    assert s[0]["question"] is menu


def test_a_task_that_left_the_open_set_is_forgotten():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    _attach([_session(task="something-else")], now=CREATED + 600)
    assert startup_watch._created == {}


def test_an_old_stall_is_dropped_rather_than_waiting_forever():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    s = [_session()]
    _attach(s, now=CREATED + startup_watch.MAX_AGE_S)
    assert "question" not in s[0]
    assert startup_watch._created == {}


def test_matches_on_the_name_when_emdash_reports_a_different_project_string():
    startup_watch.note_created("canopy-web", "c-goal-6a8f", now=lambda: CREATED)
    s = [_session(project="Canopy Web")]
    _attach(s, now=CREATED + 600)
    assert s[0]["question"]["source"] == "startup"


def test_an_ambiguous_name_matches_nothing():
    startup_watch.note_created("ada", "editing", now=lambda: CREATED)
    startup_watch.note_created("eva", "editing", now=lambda: CREATED)
    s = [_session(task="editing", project="other")]
    _attach(s, now=CREATED + 600)
    assert "question" not in s[0]


def test_the_watch_survives_a_restart(tmp_path):
    path = tmp_path / "startup-watch.json"
    startup_watch.configure(path)
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)

    startup_watch._created.clear()      # the process restarts...
    startup_watch.configure(path)       # ...and loads its store on first use
    s = [_session()]
    _attach(s, now=CREATED + 600)
    assert s[0]["question"]["source"] == "startup"


@pytest.mark.parametrize("payload", ["not json", "{}", json.dumps([{"task": 3}])])
def test_an_unreadable_store_is_an_empty_watch(tmp_path, payload):
    path = tmp_path / "startup-watch.json"
    path.write_text(payload)
    startup_watch.configure(path)
    s = [_session()]
    _attach(s, now=CREATED + 600)
    assert "question" not in s[0]
    # ...and the watch still works, and rewrites the file whole.
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)
    assert json.loads(path.read_text()) == [
        {"project": "hal", "task": "c-goal-6a8f", "created_at": CREATED}]


def test_a_failing_resolver_never_breaks_the_report():
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)

    def boom(*_a):
        raise OSError("disk")

    s = [_session()]
    startup_watch.attach_stall_markers(s, has_transcript=boom, now=lambda: CREATED + 600)
    assert "question" not in s[0]


class _Cfg:
    session_tail_count = 30
    session_tail_limit = 8
    session_report_seconds = 10
    session_report_limit = 100
    emdash_db = "/nonexistent"
    runner_id = "r"


class _Client:
    def __init__(self):
        self.sessions_seen = []

    def report_sessions(self, runner_id, sessions, archived=None, complete=False):
        self.sessions_seen.append(sessions)


def test_the_session_report_carries_the_stall_marker(monkeypatch):
    sessions_mod._tail_readers.clear()
    sessions_mod._last_session_report = 0.0
    monkeypatch.setattr(emdash, "list_open_sessions",
                        lambda _db, _l=100: [{"emdash_task": "c-goal-6a8f", "project": "hal"}])
    monkeypatch.setattr(emdash, "list_recently_archived_tasks", lambda _db, _l=100: [])
    monkeypatch.setattr(transcript, "attach_recent_tail", lambda _s, **_k: None)
    monkeypatch.setattr(transcript, "resolve_transcript", lambda *_a, **_k: None)
    monkeypatch.setattr(startup_watch.time, "time", lambda: CREATED + 600)
    startup_watch.note_created("hal", "c-goal-6a8f", now=lambda: CREATED)

    c = _Client()
    sessions_mod.maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.sessions_seen[-1][0]["question"]["source"] == "startup"


def test_a_create_puts_the_new_session_on_the_watch(monkeypatch):
    from canopy_runner import cdp_control, execute
    from tests.test_execute import FakeClient, _cfg, _turn

    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, task_name="", port=9222: {"task": "c-goal-6a8f"})
    client = FakeClient({"reuse": False, "new_thread": True, "summary": ""})
    execute.execute_turn(_cfg(), client, "r-1", _turn())
    assert ("hal", "c-goal-6a8f") in startup_watch._created
