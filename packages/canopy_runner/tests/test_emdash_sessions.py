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
        INSERT INTO projects VALUES ('p1','canopy-web','/x/canopy-web');
        INSERT INTO tasks VALUES ('t1','p1','cloud-runner','in_progress',NULL,'2026-07-16T15:52:00','task');
        INSERT INTO tasks VALUES ('t2','p1','ddd','in_progress',NULL,'2026-07-16T12:41:00','task');
        INSERT INTO tasks VALUES ('t3','p1','old','done','2026-07-15T00:00:00','2026-07-15T00:00:00','task');
        -- an un-promoted automation-run: emdash hides it under "Automations", so must NOT show.
        INSERT INTO tasks VALUES ('t4','p1','plain-keys-rescue','in_progress',NULL,'2026-07-13T15:01:00','automation-run');
        """
    )
    conn.commit()
    conn.close()


def test_lists_unarchived_tasks_newest_first(tmp_path):
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    out = emdash.list_open_sessions(str(db))
    # 'old' archived → excluded; 'plain-keys-rescue' is an automation-run → excluded.
    assert [s["emdash_task"] for s in out] == ["cloud-runner", "ddd"]
    assert out[0]["project"] == "canopy-web"
    assert out[0]["last_interacted_at"] == "2026-07-16T15:52:00"


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


def test_lists_recently_archived_task_names_newest_first(tmp_path):
    """The CLOSING signal: without it the server cannot tell "you archived this" from
    "I lost sight of it", so it can never retire a row.

    Row-insertion order here is deliberately the OPPOSITE of the asserted result: the
    older-archived row ('older', archived 2026-07-01) goes in BEFORE the newer-archived
    one ('old', archived 2026-07-15, re-inserted after `_make_db`'s copy is deleted). With
    no `ORDER BY` at all, sqlite's natural (rowid) order would yield ["older", "old"] —
    the WRONG order — so the asserted ["old", "older"] can only pass via a real
    `ORDER BY t.archived_at DESC`. (An earlier version of this test inserted 'old' before
    'older', which matched insertion order too, so the assertion passed even with no
    ORDER BY clause at all — vacuous.)"""
    db = tmp_path / "emdash4.db"
    _make_db(str(db))
    conn = sqlite3.connect(str(db))
    # Remove _make_db's 'old' row so we control its insertion position below.
    conn.execute("DELETE FROM tasks WHERE name='old'")
    # OLDER-archived row inserted FIRST...
    conn.execute(
        "INSERT INTO tasks VALUES ('t5','p1','older','done','2026-07-01T00:00:00',"
        "'2026-07-01T00:00:00','task')"
    )
    # ...NEWER-archived row inserted SECOND — reversed vs. the expected output order.
    conn.execute(
        "INSERT INTO tasks VALUES ('t3','p1','old','done','2026-07-15T00:00:00',"
        "'2026-07-15T00:00:00','task')"
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
