"""Resolving a transcript from emdash's OWN record instead of a path convention.

The convention is a guess at a layout emdash owns and has changed twice. emdash 1.2
records `conversations.cwd` and `conversations.provider_session_id` — the worktree and
the id that NAMES the .jsonl — so the answer can be read rather than reconstructed.

What these tests defend is the FALLBACK as much as the new path: this must be strictly
additive. It may make resolution succeed where the convention failed; it must never
make it fail where the convention would have worked.
"""
from __future__ import annotations

import sqlite3

import pytest

from canopy_runner import transcript
from canopy_runner.emdash import session_transcript_ref
from canopy_transcript import encode_project_dir

_SCHEMA = """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, deleted_at TEXT);
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL, type TEXT DEFAULT 'task',
      archived_at TEXT, deleted_at TEXT
    );
    CREATE TABLE conversations (
      id TEXT PRIMARY KEY, task_id TEXT, cwd TEXT, provider_session_id TEXT,
      last_session_activity_at TEXT,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL
    );
"""

_PRE_1_2 = """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT, name TEXT, type TEXT, archived_at TEXT);
    CREATE TABLE conversations (id TEXT PRIMARY KEY, task_id TEXT);
"""


def _db(tmp_path, schema=_SCHEMA):
    p = tmp_path / "emdash4.db"
    conn = sqlite3.connect(p)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return str(p)


def _row(db, *, project, task, cwd=None, sid=None, at="2026-08-28T12:00:00Z", deleted=None):
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)", (project, project))
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, project_id, name, type, deleted_at) "
        "VALUES (?, ?, ?, 'task', ?)",
        (f"{project}/{task}", project, task, deleted),
    )
    conn.execute(
        "INSERT INTO conversations (id, task_id, cwd, provider_session_id, last_session_activity_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"c-{project}-{sid or at}", f"{project}/{task}", cwd, sid, at),
    )
    conn.commit()
    conn.close()


def _plant(claude_home, cwd, sid):
    d = claude_home / encode_project_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("{}\n")
    return f


@pytest.fixture()
def paths(tmp_path):
    home = tmp_path / "home"
    ch = home / ".claude" / "projects"
    ch.mkdir(parents=True)
    return home, ch


# --- the read ---------------------------------------------------------------------------

def test_ref_returns_both_halves(tmp_path):
    db = _db(tmp_path)
    _row(db, project="canopy-web", task="emdash-check", cwd="/w/x", sid="sid-1")
    assert session_transcript_ref(db, "canopy-web", "emdash-check") == ("/w/x", "sid-1")


def test_ref_is_none_when_the_provider_has_not_reported_an_id(tmp_path):
    """emdash knows the task the instant it is created; Claude Code reports its session
    id later. A half-answer must read as no answer, or the caller skips the fallback and
    the session resolves to nothing during exactly the window it is being set up."""
    db = _db(tmp_path)
    _row(db, project="ace", task="fresh", cwd="/w/fresh", sid=None)
    assert session_transcript_ref(db, "ace", "fresh") is None


def test_ref_is_none_on_a_pre_1_2_emdash(tmp_path):
    """An older emdash has neither column. That is a legitimate state, so the read
    degrades to "ask the convention" rather than raising and taking the runner with it."""
    db = _db(tmp_path, _PRE_1_2)
    assert session_transcript_ref(db, "ace", "anything") is None


def test_ref_ignores_a_soft_deleted_task(tmp_path):
    db = _db(tmp_path)
    _row(db, project="ace", task="binned", cwd="/w/binned", sid="sid-x", deleted="2026-08-28T00:00:00Z")
    assert session_transcript_ref(db, "ace", "binned") is None


def test_ref_prefers_the_newest_conversation(tmp_path):
    """Task names are reused and a task owns several conversations. An earlier one must
    not answer for the live session — that is how you tail a file nobody is writing."""
    db = _db(tmp_path)
    _row(db, project="ace", task="dup", cwd="/w/old", sid="old", at="2026-08-27T00:00:00Z")
    _row(db, project="ace", task="dup", cwd="/w/new", sid="new", at="2026-08-28T00:00:00Z")
    assert session_transcript_ref(db, "ace", "dup") == ("/w/new", "new")


def test_ref_does_not_cross_projects(tmp_path):
    db = _db(tmp_path)
    _row(db, project="ace", task="shared-name", cwd="/w/ace", sid="a")
    _row(db, project="eva", task="shared-name", cwd="/w/eva", sid="e")
    assert session_transcript_ref(db, "eva", "shared-name") == ("/w/eva", "e")


# --- resolution, and the fallback ---------------------------------------------------------

def test_emdash_record_resolves_a_layout_no_convention_knows(paths, tmp_path):
    """The whole point. This worktree matches none of the three conventions, and it
    resolves anyway — so the NEXT layout change costs a fallback, not an outage."""
    home, ch = paths
    db = _db(tmp_path)
    cwd = "/somewhere/emdash/has/not/told/us/about"
    _row(db, project="canopy-web", task="emdash-check", cwd=cwd, sid="sid-1")
    want = _plant(ch, cwd, "sid-1")

    got = transcript.resolve_transcript(
        "canopy-web", "emdash-check", home=home, claude_home=ch, emdash_db=db
    )
    assert got == want
    # ...and without the DB, the same lookup finds nothing at all.
    assert transcript.resolve_transcript(
        "canopy-web", "emdash-check", home=home, claude_home=ch
    ) is None


def test_falls_back_when_emdash_names_a_file_that_does_not_exist_yet(paths, tmp_path):
    """emdash reports a session id before Claude Code has created the file. Returning
    None there would be a REGRESSION against the convention, which can still find an
    older transcript for the same task."""
    home, ch = paths
    db = _db(tmp_path)
    _row(db, project="ace", task="spark", cwd="/w/not-written-yet", sid="sid-missing")
    worktree = home / "emdash" / "worktrees" / "ace" / "emdash" / "spark-ry12q"
    worktree.mkdir(parents=True)
    want = _plant(ch, str(worktree), "conventional")

    got = transcript.resolve_transcript(
        "ace", "spark", home=home, claude_home=ch, emdash_db=db
    )
    assert got == want


def test_falls_back_when_there_is_no_emdash_db_at_all(paths, tmp_path):
    home, ch = paths
    worktree = home / "emdash" / "worktrees" / "ace" / "emdash" / "spark-ry12q"
    worktree.mkdir(parents=True)
    want = _plant(ch, str(worktree), "conventional")

    got = transcript.resolve_transcript(
        "ace", "spark", home=home, claude_home=ch, emdash_db=str(tmp_path / "nope.db")
    )
    assert got == want


def test_the_exact_answer_beats_the_conventional_one(paths, tmp_path):
    """Both resolve; emdash's wins. The convention takes the NEWEST .jsonl in the dir,
    which is the wrong session whenever a worktree has hosted more than one — emdash
    names the actual conversation."""
    home, ch = paths
    db = _db(tmp_path)
    worktree = home / "emdash" / "worktrees" / "ace" / "emdash" / "spark-ry12q"
    worktree.mkdir(parents=True)
    _row(db, project="ace", task="spark", cwd=str(worktree), sid="the-real-one")
    exact = _plant(ch, str(worktree), "the-real-one")
    newer = _plant(ch, str(worktree), "a-later-unrelated-session")
    import os, time
    os.utime(newer, (time.time() + 3600, time.time() + 3600))  # convention prefers newest

    got = transcript.resolve_transcript(
        "ace", "spark", home=home, claude_home=ch, emdash_db=db
    )
    assert got == exact
    assert transcript.resolve_transcript(
        "ace", "spark", home=home, claude_home=ch
    ) == newer
