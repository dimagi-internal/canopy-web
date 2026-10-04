"""A phone-triggered DELETE or ANSWER can never act on another agent's same-named session.

emdash task names are unique per PROJECT, not per emdash. On 2026-10-04 ada and
eva each had a session called "editing", and a chat message meant for eva's was
typed into ada's (turn 22662f53) — the sidecar took the first sidebar row with
that label. The send path was fixed (#1115); the two phone-triggered verbs that
still drove a session by name alone were:

  * CLOSE  — deletes the emdash task. Irreversible. By name alone it deleted the
             FIRST "editing" in the sidebar: ada's, when eva's was meant.
  * ANSWER — presses keys in the session's terminal. In the wrong session a
             number lands in its prompt as an instruction.

Both now aim by (project, task): the server sends the project, emdash's DB checks
it, and with no project the name is only trusted when exactly one project holds
it. A delete that cannot be placed is REFUSED, never done by name.

The fake sidecar here behaves exactly like the real one did: with a project it
acts on that project's row; without one, on the first row with the label — ada's,
which sits above eva's in the live sidebar.
"""
import sqlite3
import types

import pytest

from canopy_runner import close, hooks, sessions, streams
from canopy_runner.main import make_control_handler
from canopy_runner.wake import WakeListener

SIDEBAR_ORDER = ["ada", "eva"]          # as read from the live emdash, 2026-10-04

DIALOG = """\
 Bash command
   rm target.txt
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend
"""


def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, deleted_at TEXT);
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
          archived_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL,
          type TEXT DEFAULT 'task' NOT NULL, deleted_at TEXT
        );
    """)
    for i, (project, task) in enumerate(rows):
        conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)", (project, project))
        conn.execute("INSERT INTO tasks (id, project_id, name) VALUES (?, ?, ?)",
                     (f"t{i}", project, task))
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def two_editings(tmp_path):
    """ada and eva each own a live emdash task called "editing"."""
    return _db(tmp_path / "emdash4.db", [("ada", "editing"), ("eva", "editing")])


@pytest.fixture
def one_editing(tmp_path):
    return _db(tmp_path / "emdash4.db", [("ada", "editing")])


def _pick(project):
    return project if project else SIDEBAR_ORDER[0]


@pytest.fixture
def deleted(monkeypatch):
    """Which project's "editing" the (fake) sidecar deleted."""
    out = []

    def close_task(task, port=9222, project=""):
        out.append((_pick(project), task))
        return {"ok": True, "action": "deleted"}

    monkeypatch.setattr(close.cdp_control, "close_task", close_task)
    sessions._PENDING_CLOSED.clear()
    getattr(close, "_warned", set()).clear()
    return out


class _Terminals:
    """Each project's "editing" pane; keys land in whichever row the lookup picks."""

    def __init__(self):
        self.pressed = []

    def read_terminal(self, task, *, port=9222, project=""):
        return DIALOG

    def send_keys(self, task, keys, *, port=9222, project=""):
        self.pressed.append((_pick(project), task, keys))
        return {"ok": True}


@pytest.fixture
def terminals(monkeypatch):
    t = _Terminals()
    monkeypatch.setattr(hooks, "cdp_control", t)
    monkeypatch.setattr(hooks, "note_answer_outcome", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "request_report_now", lambda: None)
    return t


class _Client:
    def __init__(self, closes=(), answers=()):
        self._closes, self._answers, self.retired = list(closes), list(answers), []

    def sync_closes(self, runner_id):
        return self._closes

    def sync_menu_answers(self, runner_id):
        return self._answers

    def post_menu_answer_result(self, runner_id, session_id, answer_id, outcome):
        self.retired.append(outcome)


def _cfg(db):
    return types.SimpleNamespace(cdp_port=9222, runner_id="r1", base_url="http://x",
                                 token="t", state_path=None, emdash_db=db)


def _on_control(db, client=None):
    return make_control_handler(_cfg(db), WakeListener("http://x", "t", "r1"), client)


# ── CLOSE ───────────────────────────────────────────────────────────────────

def test_a_close_from_the_phone_deletes_the_named_projects_session(two_editings, deleted):
    _on_control(two_editings)({"type": "close_session", "session_id": "s",
                               "session_key": "editing", "project": "eva"})
    assert deleted == [("eva", "editing")], "deleted ada's session instead of eva's"


def test_a_close_from_the_poll_tick_deletes_the_named_projects_session(two_editings, deleted):
    client = _Client(closes=[{"session_id": "s", "session_key": "editing", "project": "eva"}])
    streams.drain_closes(_cfg(two_editings), client)
    assert deleted == [("eva", "editing")]


def test_a_close_with_no_project_and_two_namesakes_is_refused(two_editings, deleted):
    """An older server sends no project. Two projects hold the name, so a delete
    would be a guess — and a delete cannot be taken back. Nothing is touched."""
    _on_control(two_editings)({"type": "close_session", "session_id": "s",
                               "session_key": "editing"})
    with pytest.raises(close.CloseRefused):
        close.close_session("editing", emdash_db=two_editings)
    assert deleted == []
    assert sessions._PENDING_CLOSED == set()     # and nothing reported as closed


def test_a_close_with_no_project_and_an_unreadable_db_is_refused(tmp_path, deleted):
    with pytest.raises(close.CloseRefused):
        close.close_session("editing", emdash_db=str(tmp_path / "missing.db"))
    assert deleted == []


def test_a_close_with_no_project_and_one_holder_deletes_that_one(one_editing, deleted):
    assert close.close_session("editing", emdash_db=one_editing) == "deleted"
    assert deleted == [("ada", "editing")]


def test_a_close_for_a_project_that_no_longer_holds_the_task_touches_nothing(
        one_editing, deleted):
    """eva's "editing" is gone; ada's is not. Closing eva's must not find ada's."""
    assert close.close_session("editing", project="eva", emdash_db=one_editing) == "absent"
    assert deleted == []
    assert sessions._PENDING_CLOSED == {"editing"}


# ── ANSWER ──────────────────────────────────────────────────────────────────

def test_an_answer_from_the_phone_presses_keys_in_the_named_projects_session(
        two_editings, terminals):
    _on_control(two_editings, _Client())({
        "type": "menu_answer", "session_id": "s", "answer_id": "a",
        "session_key": "editing", "project": "eva", "option": 1})
    assert terminals.pressed == [("eva", "editing", ["1", "\r"])], \
        "pressed the key in ada's session instead of eva's"


def test_an_answer_from_the_poll_tick_presses_keys_in_the_named_projects_session(
        two_editings, terminals):
    client = _Client(answers=[{"session_id": "s", "session_key": "editing",
                               "project": "eva", "answer_id": "a", "option": 1}])
    streams.drain_menu_answers(_cfg(two_editings), client)
    assert terminals.pressed == [("eva", "editing", ["1", "\r"])]


def test_an_answer_with_no_project_and_two_namesakes_presses_nothing(two_editings, terminals):
    client = _Client(answers=[{"session_id": "s", "session_key": "editing",
                               "answer_id": "a", "option": 1}])
    streams.drain_menu_answers(_cfg(two_editings), client)
    assert terminals.pressed == []
    assert client.retired == [hooks.AMBIGUOUS]   # and the phone is told why
    assert hooks.ANSWER_NOTES[hooks.AMBIGUOUS]
