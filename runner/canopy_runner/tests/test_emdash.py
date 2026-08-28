import sqlite3
import uuid
from pathlib import Path

import pytest

from canopy_runner.emdash import (
    READ_SCHEMA,
    SchemaCheckError,
    check_read_schema,
    list_open_sessions,
    task_state,
)


def _emdash_schema() -> str:
    """The CDP-path's read surface: the `tasks` and `projects` columns task_state()
    and list_open_sessions() name. Deliberately a superset-shaped, minimal stand-in
    for emdash's real schema — only the read columns matter here."""
    return """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, deleted_at TEXT);
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
          archived_at TEXT, last_interacted_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL,
          type TEXT DEFAULT 'task' NOT NULL, automation_run_id TEXT, deleted_at TEXT
        );
        CREATE TABLE conversations (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_status TEXT,
          last_session_activity_at TEXT,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
    """


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = tmp_path / "emdash4.db"
    conn = sqlite3.connect(path)
    conn.executescript(_emdash_schema())
    conn.execute("INSERT INTO projects (id, name) VALUES ('proj-1', 'canopy-web')")
    conn.commit()
    conn.close()
    return str(path)


# --------------------------------------------------------------------------------------
# check_read_schema — verify-emdash's one job: the read columns still exist
# --------------------------------------------------------------------------------------

def test_check_read_schema_intact(db):
    assert check_read_schema(db) == ([], [])


def test_check_read_schema_names_a_dropped_column(db):
    """A renamed/dropped column the reads depend on must be reported by name — this is
    the SILENT failure verify-emdash exists to make loud (the reads swallow it)."""
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tasks RENAME COLUMN last_interacted_at TO touched_at")
    conn.commit()
    conn.close()
    problems, older = check_read_schema(db)
    assert problems == ["tasks.last_interacted_at missing"]
    assert older == []


def test_check_read_schema_reports_a_missing_table(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (name TEXT, archived_at TEXT, created_at TEXT, status TEXT, "
        "last_interacted_at TEXT, type TEXT, project_id TEXT, deleted_at TEXT);"
    )  # no `projects` table at all
    conn.commit()
    conn.close()
    problems, _ = check_read_schema(str(path))
    assert "table 'projects' missing (or has no columns)" in problems


def test_check_read_schema_raises_when_db_absent(tmp_path):
    """A missing DB is distinct from a drifted schema — it raises rather than returning
    a (misleading) list of 'missing columns'."""
    with pytest.raises(SchemaCheckError):
        check_read_schema(str(tmp_path / "nope.db"))


def test_check_read_schema_does_not_create_a_db_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(SchemaCheckError):
        check_read_schema(str(missing))
    assert not missing.exists()


def test_read_schema_matches_the_actual_read_sql(db):
    """Guard against READ_SCHEMA drifting from the SQL it's supposed to mirror: every
    column named in READ_SCHEMA must be a real column the live reads can select. If this
    fails, either the SQL changed without READ_SCHEMA, or vice-versa."""
    conn = sqlite3.connect(db)
    try:
        for table, cols in READ_SCHEMA.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert set(cols) <= present, f"{table}: {set(cols) - present}"
    finally:
        conn.close()
    # ...and the reads themselves run clean against a schema that has exactly these cols.
    assert list_open_sessions(db) == []
    assert task_state(db, "never-existed") == "absent"


# --------------------------------------------------------------------------------------
# task_state — READ-ONLY existence truth for the reuse decision (see execute.execute_turn)
# --------------------------------------------------------------------------------------

def _add_task(db, name, *, archived_at=None, task_id=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, status, archived_at) VALUES (?, 'proj-1', ?, 'in_progress', ?)",
        (task_id or str(uuid.uuid4()), name, archived_at),
    )
    conn.commit()
    conn.close()


def test_task_state_live(db):
    _add_task(db, "eva-org-research-790c-0715-1352")
    assert task_state(db, "eva-org-research-790c-0715-1352") == "live"


def test_task_state_archived(db):
    _add_task(db, "eva-org-research-790c-0715-1216", archived_at="2026-07-15T19:07:21.147Z")
    assert task_state(db, "eva-org-research-790c-0715-1216") == "archived"


def test_task_state_absent(db):
    assert task_state(db, "never-existed") == "absent"


def test_task_state_unknown_when_db_unreadable(tmp_path):
    """An unreadable/missing DB must NOT masquerade as 'absent' — that would be a false
    'gone' and duplicate a live session. The caller degrades to the CDP verdict instead."""
    assert task_state(str(tmp_path / "nope.db"), "anything") == "unknown"


def test_task_state_does_not_create_a_db_file(tmp_path):
    """sqlite3.connect() creates an empty file by default — don't litter emdash's dir."""
    missing = tmp_path / "nope.db"
    task_state(str(missing), "anything")
    assert not missing.exists()


def test_task_state_prefers_the_newest_row_for_a_reused_name(db):
    """Task names aren't unique in emdash's schema. If a name was reused, the newest row
    decides — an old archived namesake must not report the live one as gone."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, status, archived_at, created_at) "
        "VALUES ('old', 'proj-1', 'dup-name', 'in_progress', '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, status, archived_at, created_at) "
        "VALUES ('new', 'proj-1', 'dup-name', 'in_progress', NULL, '2026-07-15T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    assert task_state(db, "dup-name") == "live"


# --------------------------------------------------------------------------------------
# list_open_sessions — the phone's continuable-session list; fail-soft, type='task' only
# --------------------------------------------------------------------------------------

def test_list_open_sessions_returns_unarchived_tasks_joined_to_projects(db):
    _add_task(db, "live-one")
    rows = list_open_sessions(db)
    assert len(rows) == 1
    assert rows[0]["emdash_task"] == "live-one"
    assert rows[0]["project"] == "canopy-web"  # joined from `projects`


def test_list_open_sessions_excludes_automation_runs_and_archived(db):
    _add_task(db, "real-session")
    _add_task(db, "gone", archived_at="2026-07-15T00:00:00Z")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, status, type) "
        "VALUES ('a1', 'proj-1', 'phantom', 'in_progress', 'automation-run')"
    )
    conn.commit()
    conn.close()
    names = {r["emdash_task"] for r in list_open_sessions(db)}
    assert names == {"real-session"}  # archived + automation-run rows excluded


def test_list_open_sessions_is_fail_soft_on_missing_db(tmp_path):
    assert list_open_sessions(str(tmp_path / "nope.db")) == []


# --------------------------------------------------------------------------------------
# agent_status — emdash's OWN "is this working right now", the one signal a transcript
# cannot give: a session inside a long tool call is silent, not finished.
# --------------------------------------------------------------------------------------

def _add_conversation(db, task_id, agent_status, *, at="2026-08-12T23:00:00Z", column="last_session_activity_at"):
    """`column` picks which timestamp carries the ordering. emdash 1.2 populates
    `last_session_activity_at` sparsely (10 of 26 rows on the fleet laptop), so the
    read COALESCEs onto the always-present `updated_at` — pass "updated_at" to build
    the rows that exercise that leg."""
    conn = sqlite3.connect(db)
    conn.execute(
        f"INSERT INTO conversations (id, task_id, agent_status, {column}) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), task_id, agent_status, at),
    )
    conn.commit()
    conn.close()


def test_list_open_sessions_reports_the_agent_status(db):
    _add_task(db, "pov-grad", task_id="t-1")
    _add_conversation(db, "t-1", "working")
    (row,) = list_open_sessions(db)
    assert row["agent_status"] == "working"
    assert "task_id" not in row  # a join key, not part of the report


def test_a_task_with_no_conversation_reports_no_status(db):
    """"" is "I could not answer", which the server reads as "fall back to recency" —
    never as "idle". A task emdash has not driven yet is exactly that case."""
    _add_task(db, "fresh", task_id="t-2")
    (row,) = list_open_sessions(db)
    assert row["agent_status"] == ""


def test_working_on_any_conversation_wins(db):
    """A task owns several conversations and only one of them is being driven; the
    others sit at awaiting-input. The session is working."""
    _add_task(db, "multi", task_id="t-3")
    _add_conversation(db, "t-3", "working", at="2026-08-12T22:00:00Z")
    _add_conversation(db, "t-3", "awaiting-input", at="2026-08-12T23:00:00Z")
    (row,) = list_open_sessions(db)
    assert row["agent_status"] == "working"


def test_without_working_the_newest_conversation_answers(db):
    _add_task(db, "quiet", task_id="t-4")
    _add_conversation(db, "t-4", "awaiting-input", at="2026-08-12T22:00:00Z")
    _add_conversation(db, "t-4", "compacting", at="2026-08-12T23:00:00Z")
    (row,) = list_open_sessions(db)
    assert row["agent_status"] == "compacting"


def test_a_null_activity_stamp_still_orders_by_updated_at(db):
    """emdash 1.2 dropped `conversations.last_interacted_at` and its migration train
    (0036) copied the values into `last_session_activity_at` — but that successor is
    NULLABLE and sparsely written (10 of 26 rows, fleet laptop 2026-08-28). Ordering
    by it alone is therefore not an ordering: the 16 NULL rows sort together and the
    newest-wins rule silently picks an arbitrary one. The read COALESCEs onto
    `updated_at`, which emdash declares NOT NULL, so a row with no activity stamp
    still sorts where it belongs."""
    _add_task(db, "sparse", task_id="t-6")
    _add_conversation(db, "t-6", "idle", at="2026-08-12T22:00:00Z", column="updated_at")
    _add_conversation(db, "t-6", "awaiting-input", at="2026-08-12T23:00:00Z", column="updated_at")
    (row,) = list_open_sessions(db)
    assert row["agent_status"] == "awaiting-input"


def test_a_drifted_conversations_table_costs_the_status_not_the_report(db):
    """The asymmetry that matters: this read is an ENRICHMENT. An emdash that cannot
    answer must leave us reporting sessions without the flag — raising here would skip
    the whole report, and an empty report clears every RunnerBinding server-side."""
    _add_task(db, "still-listed", task_id="t-5")
    _add_conversation(db, "t-5", "working")
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE conversations RENAME COLUMN agent_status TO state")
    conn.commit()
    conn.close()

    (row,) = list_open_sessions(db)
    assert row["emdash_task"] == "still-listed"
    assert row["agent_status"] == ""
    # ...and verify-emdash is where that degradation becomes visible.
    assert check_read_schema(db)[0] == ["conversations.agent_status missing"]


# --------------------------------------------------------------------------------------
# Soft delete. emdash 1.2 added `deleted_at` to tasks/projects and asks for live tasks as
# `and(isNull(archivedAt), isNull(deletedAt))`. Matching that is what keeps canopy's idea
# of "open" the same as emdash's — filtering on archived_at alone reports sessions that
# emdash no longer shows anywhere, which is a session nobody can click into.
# --------------------------------------------------------------------------------------

def _soft_delete(db, name):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET deleted_at = '2026-08-28T06:00:00Z' WHERE name = ?", (name,))
    conn.commit()
    conn.close()


def test_a_soft_deleted_task_is_not_an_open_session(db):
    _add_task(db, "kept")
    _add_task(db, "binned")
    _soft_delete(db, "binned")
    assert {r["emdash_task"] for r in list_open_sessions(db)} == {"kept"}


def test_a_soft_deleted_task_is_absent_not_live(db):
    """`task_state` drives the REUSE decision. Calling a deleted task "live" sends the
    runner to open a task emdash will not find, and the CDP path then fails at a point
    where it can no longer tell "glitched" from "gone"."""
    _add_task(db, "binned")
    _soft_delete(db, "binned")
    assert task_state(db, "binned") == "absent"


def test_a_soft_deleted_task_is_not_reported_as_archived(db):
    """The archived list is the CLOSING signal — the server un-retires anything absent
    from it. A deleted task is not something the human archived, and reporting it as
    such would resurrect it."""
    from canopy_runner.emdash import list_recently_archived_tasks

    _add_task(db, "really-archived", archived_at="2026-08-27T00:00:00Z")
    _add_task(db, "binned", archived_at="2026-08-27T00:00:00Z")
    _soft_delete(db, "binned")
    assert list_recently_archived_tasks(db) == ["really-archived"]


# --------------------------------------------------------------------------------------
# An emdash OLDER than 1.2. The fleet's two macOS accounts each auto-update their own
# emdash, so it is routinely mixed-version — and a read that names a 1.2 column
# unconditionally does not degrade on the older box, it RAISES. `list_open_sessions`
# raising means the caller skips the whole session report (reporting empty would clear
# every RunnerBinding), so the phone's list silently goes stale and stays stale (#641).
#
# Every fixture above was moved to 1.2's shape when those columns were added, which is
# exactly why nothing caught it. These pin the older shape.
# --------------------------------------------------------------------------------------

_PRE_1_2_SCHEMA = """
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL, status TEXT,
      archived_at TEXT, created_at TEXT, last_interacted_at TEXT,
      type TEXT DEFAULT 'task' NOT NULL
    );
    CREATE TABLE conversations (
      id TEXT PRIMARY KEY, task_id TEXT, agent_status TEXT, last_interacted_at TEXT
    );
"""


@pytest.fixture()
def old_db(tmp_path: Path) -> str:
    path = tmp_path / "emdash4.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_1_2_SCHEMA)
    conn.execute("INSERT INTO projects (id, name) VALUES ('p', 'canopy-web')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, status, type) "
        "VALUES ('t', 'p', 'some-task', 'in_progress', 'task')"
    )
    conn.execute(
        "INSERT INTO conversations (id, task_id, agent_status, last_interacted_at) "
        "VALUES ('c', 't', 'working', '2026-08-20T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_open_sessions_still_work_on_a_pre_1_2_emdash(old_db):
    rows = list_open_sessions(old_db)
    assert [r["emdash_task"] for r in rows] == ["some-task"]


def test_agent_status_survives_on_a_pre_1_2_emdash(old_db):
    """The ordering column was RENAMED, not merely added, so this is the one that has
    to fall back rather than simply omit a predicate — `last_interacted_at` is what the
    older emdash calls it."""
    (row,) = list_open_sessions(old_db)
    assert row["agent_status"] == "working"


def test_task_state_still_answers_on_a_pre_1_2_emdash(old_db):
    """"unknown" here would push every reuse decision onto the CDP verdict, which is
    the false-negative that duplicates live sessions."""
    assert task_state(old_db, "some-task") == "live"


def test_projects_still_list_on_a_pre_1_2_emdash(old_db):
    from canopy_runner.emdash import list_projects

    assert list_projects(old_db) == ["canopy-web"]


def test_a_pre_1_2_emdash_is_not_reported_as_drifted(old_db):
    problems, older = check_read_schema(old_db)
    assert problems == []
    assert any("deleted_at" in o for o in older)
