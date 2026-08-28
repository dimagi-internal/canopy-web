"""The emdash upgrade check.

The point of these tests is NOT that the check reports green on a healthy box — that
proves nothing, and a check that only ever passes is indistinguishable from a check
that cannot fail. Each one reconstructs a drift emdash has actually shipped and asserts
the check names it.
"""
from __future__ import annotations

import sqlite3

import pytest

from canopy_runner import upgrade_check

# emdash 1.2's shape, trimmed to the columns canopy reads.
_SCHEMA_12 = """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, deleted_at TEXT);
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL, status TEXT,
      archived_at TEXT, created_at TEXT, last_interacted_at TEXT,
      type TEXT DEFAULT 'task' NOT NULL, deleted_at TEXT
    );
    CREATE TABLE conversations (
      id TEXT PRIMARY KEY, task_id TEXT, agent_status TEXT,
      last_session_activity_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL,
      provider_session_id TEXT
    );
"""


def _db(tmp_path, schema=_SCHEMA_12):
    p = tmp_path / "emdash4.db"
    conn = sqlite3.connect(p)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return str(p)


def _session(db, *, repo, task, sid=None):
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)", (repo, repo))
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, type) VALUES (?, ?, ?, 'task')",
        (task, repo, task),
    )
    conn.execute(
        "INSERT INTO conversations (id, task_id, provider_session_id) VALUES (?, ?, ?)",
        (f"c-{task}", task, sid),
    )
    conn.commit()
    conn.close()


def _plant_transcript(home, claude_home, *, worktree_rel, sid):
    """A real worktree + the Claude transcript that belongs to it."""
    from canopy_transcript import encode_project_dir

    worktree = home / "emdash" / "worktrees" / worktree_rel
    worktree.mkdir(parents=True)
    proj = claude_home / encode_project_dir(worktree)
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text("{}\n")


@pytest.fixture()
def paths(tmp_path):
    home = tmp_path / "home"
    claude_home = home / ".claude" / "projects"
    claude_home.mkdir(parents=True)
    return home, claude_home


# --- the schema surface (the drift emdash 1.2 actually shipped) -------------------------

def test_a_dropped_column_is_named(tmp_path):
    """emdash 1.2 dropped `conversations.last_interacted_at`. The check has to say
    WHICH column: "something drifted" sends a human to read a diff of the whole app."""
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE conversations DROP COLUMN last_session_activity_at")
    conn.commit()
    conn.close()

    check = upgrade_check.check_schema(db)
    assert not check.ok
    assert any("conversations.last_session_activity_at missing" in n for n in check.notes)


def test_a_missing_db_is_not_reported_as_a_drift(tmp_path):
    """"emdash isn't here" is a setup problem, not a schema change, and conflating the
    two sends you looking for a column that was never missing."""
    check = upgrade_check.check_schema(str(tmp_path / "nope.db"))
    assert not check.ok
    assert "not found" in check.summary


# --- the worktree surface (the drift NOTHING was watching) ------------------------------

def test_an_unknown_worktree_layout_is_caught(paths, tmp_path):
    """The emdash 1.2 regression this check exists for. emdash knows the transcript —
    it recorded the provider's own session id — and our path convention cannot find
    it. Before this check, that state was completely silent: the session simply
    streamed nothing, forever."""
    home, claude_home = paths
    db = _db(tmp_path)
    _session(db, repo="canopy-web", task="emdash-check", sid="sid-1")
    # A layout no version of `_worktree_bases` knows about.
    _plant_transcript(home, claude_home, worktree_rel="totally-new-shape/emdash-check", sid="sid-1")

    check = upgrade_check.check_transcripts(db, home=home, claude_home=claude_home)
    assert not check.ok
    assert any("canopy-web/emdash-check" in n for n in check.notes)


def test_the_1_2_layout_passes(paths, tmp_path):
    """The same check, against the layout 1.2 actually uses — this is what pins the
    fix in place, so a future refactor of the path logic can't quietly undo it."""
    home, claude_home = paths
    db = _db(tmp_path)
    _session(db, repo="canopy-web", task="emdash-check", sid="sid-1")
    _plant_transcript(
        home, claude_home,
        worktree_rel="canopy-web-05b9fcc4/emdash-emdash-check-sq69z", sid="sid-1",
    )

    check = upgrade_check.check_transcripts(db, home=home, claude_home=claude_home)
    assert check.ok, check.notes


def test_a_session_with_no_transcript_yet_is_not_a_drift(paths, tmp_path):
    """A brand-new session has written nothing. Counting it as a miss would make the
    check red on a healthy box every time someone opened a task — and a check that
    cries wolf gets ignored exactly when it is right."""
    home, claude_home = paths
    db = _db(tmp_path)
    _session(db, repo="ace", task="fresh", sid=None)

    check = upgrade_check.check_transcripts(db, home=home, claude_home=claude_home)
    assert check.ok, check.notes


# --- the DOM surface --------------------------------------------------------------------

def test_a_missing_required_dom_contract_fails(monkeypatch):
    """A sidebar that renders but exposes no `New task for` labels means the aria-label
    convention changed — `create` would fail on the next real turn."""
    from canopy_runner import cdp_control

    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **_: True)
    monkeypatch.setattr(cdp_control, "probe", lambda **_: {
        "ok": True, "open_task_labels": 4, "new_task_labels": 0,
        "sidebar_scroller": 1, "xterm_any": 1, "xterm_rows": 1,
        "terminal_input": 1, "claude_tabs": 1,
    })
    check = upgrade_check.check_cdp(port=9222)
    assert not check.ok
    assert any("new_task_labels" in n for n in check.notes)


def test_an_absent_terminal_is_a_note_not_a_failure(monkeypatch):
    """The probe opens nothing, so with no task on screen there is no terminal. That
    is the ordinary state of an idle emdash, not a drift — calling it one would make
    the check unrunnable on exactly the quiet box you want to check before a shift."""
    from canopy_runner import cdp_control

    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **_: True)
    monkeypatch.setattr(cdp_control, "probe", lambda **_: {
        "ok": True, "open_task_labels": 0, "new_task_labels": 7,
        "sidebar_scroller": 1, "xterm_any": 0, "xterm_rows": 0,
        "terminal_input": 0, "claude_tabs": 0,
    })
    check = upgrade_check.check_cdp(port=9222)
    assert check.ok
    assert any("not conclusive" in n for n in check.notes)


def test_emdash_not_running_skips_rather_than_fails(monkeypatch):
    from canopy_runner import cdp_control

    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **_: False)
    check = upgrade_check.check_cdp(port=9222)
    assert check.ok and check.skipped


# --- the whole run ----------------------------------------------------------------------

def test_run_is_red_when_any_surface_drifts(paths, tmp_path, monkeypatch):
    home, claude_home = paths
    db = _db(tmp_path)
    _session(db, repo="canopy-web", task="emdash-check", sid="sid-1")
    _plant_transcript(home, claude_home, worktree_rel="totally-new-shape/emdash-check", sid="sid-1")
    monkeypatch.setattr(upgrade_check, "record_verified", lambda *_: None)
    from canopy_runner import cdp_control
    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **_: False)

    checks, code = upgrade_check.run(db, port=9222, home=home, claude_home=claude_home)
    assert code == 1
    assert [c.name for c in checks if not c.ok] == ["transcripts"]


def test_an_unreadable_db_exits_2_not_1(paths, tmp_path, monkeypatch):
    """2 is "I could not look", 1 is "I looked and it drifted". A human triages those
    differently, and the exit code is what a launchd job or a script branches on."""
    home, claude_home = paths
    from canopy_runner import cdp_control
    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **_: False)

    _, code = upgrade_check.run(str(tmp_path / "missing.db"), port=9222,
                                home=home, claude_home=claude_home)
    assert code == 2


def test_a_pre_1_2_emdash_skips_rather_than_reporting_a_drift(paths, tmp_path):
    """`provider_session_id` is the referee and only exists from 1.2. Running against
    an older emdash is a legitimate state, so the check loses its ground truth and
    says so — it does not call the older emdash broken."""
    home, claude_home = paths
    old_schema = _SCHEMA_12.replace(",\n      provider_session_id TEXT", "")
    db = _db(tmp_path, old_schema)

    check = upgrade_check.check_transcripts(db, home=home, claude_home=claude_home)
    assert check.ok and check.skipped
    assert "predates provider_session_id" in check.summary
