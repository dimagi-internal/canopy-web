"""`list_projects` — the repos this box can actually drive.

`capabilities["projects"]` was typed by a human at pairing and nothing kept it
true, so a turn at a repo the box HAS but nobody listed sat QUEUED forever (labs
2026-07-28, repo `canopy`). The answer was in emdash's own `projects` table the
whole time; this read is what lets the runner report it instead.

Same fail-soft contract as the two session reads next door, for the same reason
one notch worse: a MISSING db is a legitimate "no emdash here" and returns [],
but a real READ failure must RAISE, because the caller has to be able to omit
the field rather than assert "I can drive nothing" — an empty report would blank
the stored list and make every repo turn on this runner unclaimable.
"""
import sqlite3

import pytest

from canopy_runner import emdash


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT, name TEXT, path TEXT);
        CREATE TABLE tasks (id TEXT, project_id TEXT, name TEXT, status TEXT,
                            archived_at TEXT, last_interacted_at TEXT, type TEXT);
        INSERT INTO projects VALUES ('p2','canopy-web','/x/canopy-web');
        INSERT INTO projects VALUES ('p1','canopy','/x/canopy');
        INSERT INTO projects VALUES ('p3','','/x/nameless');
        """
    )
    conn.commit()
    conn.close()


def test_lists_every_project_name_sorted(tmp_path):
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    # Sorted, not insertion-ordered: this list is compared against the stored one
    # on every heartbeat, and an unstable order would look like a change forever.
    assert emdash.list_projects(str(db)) == ["canopy", "canopy-web"]


def test_a_nameless_project_is_dropped(tmp_path):
    """`Runner.project_names()` strips empties server-side because a session turn
    has project="" — a stray "" would make the runner match every session turn via
    `project__in`. Don't ship one it has to strip."""
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    assert "" not in emdash.list_projects(str(db))


def test_a_missing_db_is_no_emdash_here_not_an_error(tmp_path):
    """A box with no emdash genuinely drives no emdash projects — [] is the truth,
    and the runner loop must survive it."""
    assert emdash.list_projects(str(tmp_path / "nope.db")) == []


def test_an_empty_projects_table_is_a_real_empty_list(tmp_path):
    """A fresh emdash with no projects yet. Distinguishable from a read failure,
    which is the entire point of raising in that case."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("CREATE TABLE projects (id TEXT, name TEXT, path TEXT);")
    conn.commit()
    conn.close()
    assert emdash.list_projects(str(db)) == []


def test_a_broken_schema_raises_rather_than_looking_empty(tmp_path):
    """The one that matters. Returning [] on a drifted/locked DB would report "I
    can drive nothing", blanking capabilities.projects and making every repo turn
    on this runner unclaimable — the `replace_reported_sessions` failure, with a
    bigger blast radius."""
    db = tmp_path / "bad.db"
    sqlite3.connect(str(db)).execute("CREATE TABLE projects (id TEXT)")  # no `name`
    with pytest.raises(emdash.EmdashReadError):
        emdash.list_projects(str(db))
