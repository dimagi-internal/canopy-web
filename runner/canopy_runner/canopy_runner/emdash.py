"""Emdash sqlite adapter — READ-ONLY.

The CDP executor drives emdash's real UI (create/reuse sessions); it never writes
to emdash's DB. But the DOM cannot answer "does this task still exist?" — emdash
virtualizes the sidebar, so a scrolled-out task is absent from the page — so the
reuse decision asks sqlite instead (`task_state`), and the phone's session list is
read the same way (`list_open_sessions`).

`task_state` is deliberately **fail-soft**: any sqlite error degrades to "unknown"
rather than raising, because a read failure must never be mistaken for "session gone"
(that false negative duplicated a live session — see its docstring). `list_open_sessions`
is fail-soft only for a MISSING db ("no emdash here"); a real read failure raises
`EmdashReadError` instead, because the caller (the phone's session report) must not
mistake "could not read" for "zero open sessions" — reporting an empty list clears
every RunnerBinding server-side. The risk both reads share is a *silent* schema drift —
emdash renaming a column these queries name — which is why `check_read_schema`
(surfaced as `canopy_runner verify-emdash`) exists: run it after an emdash update to
confirm the columns these two functions depend on still exist. Keep `READ_SCHEMA` in
lockstep with the SQL below — it IS the list of columns the SQL names.

**A row is live only if it is neither archived NOR soft-deleted.** emdash 1.2 added
`deleted_at` to `tasks` and `projects` and its own queries pair the two —
`and(isNull(tasks.archivedAt), isNull(tasks.deletedAt))` is how emdash itself asks for
live tasks. Matching that is not defensive tidiness: filtering on `archived_at` alone
means canopy reports as OPEN a task emdash no longer shows anywhere, so the supervisor
grows sessions nobody can click into and `task_state` calls a deleted task "live" and
hands it to the reuse path. The columns were empty on the fleet laptop when this was
written (2026-08-28), so the divergence is latent rather than observed — which is the
point of fixing it now: it starts costing the moment anyone deletes a task.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# The exact columns the two reads below depend on. verify-emdash asserts these
# still exist after an emdash update. Update this the moment you change the SQL.
READ_SCHEMA: dict[str, list[str]] = {
    "tasks": [
        "name",
        "archived_at",
        "created_at",
        "status",
        "last_interacted_at",
        "type",
        "project_id",
        "deleted_at",
    ],
    "projects": ["id", "name", "deleted_at"],
    # Emdash's OWN liveness flag, per conversation. Read fail-soft (see
    # `_agent_statuses`) but listed here anyway: verify-emdash is where you find
    # out a read drifted, and a degradation that nothing reports is exactly the
    # class this file exists to prevent.
    #
    # `last_interacted_at` lived here until emdash 1.2. Its migration train copied
    # the values into `last_session_activity_at` (0036) and then dropped the legacy
    # column (0037) — so the successor is emdash's own choice, not our guess. It is
    # NULLABLE and sparsely populated (10 of 26 rows on the fleet laptop, 2026-08-28),
    # hence the COALESCE onto `updated_at` in the ORDER BY below rather than a
    # straight swap: ordering by a mostly-NULL column is not an ordering.
    "conversations": [
        "task_id",
        "agent_status",
        "last_session_activity_at",
        "updated_at",
    ],
}

# `conversations.agent_status` when emdash is actively driving the agent. The
# other value seen in practice is 'awaiting-input' (the agent stopped and is
# waiting on a human) — both are reported through verbatim; only this one is
# named here because it is the one with a meaning the server acts on.
WORKING = "working"


class SchemaCheckError(Exception):
    """The emdash DB itself couldn't be opened/read — distinct from a column drift,
    so 'the DB isn't there' isn't mistaken for 'the schema changed'."""


class EmdashReadError(Exception):
    """The emdash DB exists but could not be READ (locked, corrupt, or a column the
    SQL names has been renamed).

    Distinct from a missing file, which is a legitimate "no emdash here" and stays
    fail-soft. The distinction is load-bearing: the caller must never mistake a read
    failure for "zero open sessions", because reporting an empty list clears every
    RunnerBinding server-side (`replace_reported_sessions`). Swallowing the error is
    what let a schema drift blank the supervisor with nothing in the log."""


@contextmanager
def _db(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a connection and guarantee it is closed.

    ``sqlite3.Connection`` used as a context manager only commits/rolls back the
    transaction on `__exit__` — it does NOT close the connection. Every caller here
    must go through this helper so the underlying file handle is always released.
    """
    conn = sqlite3.connect(db_path, timeout=3.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def check_read_schema(db_path: str) -> list[str]:
    """Verify every column the CDP-path reads depend on still exists.

    Returns a list of human-readable problems (``"tasks.foo missing"``,
    ``"table 'projects' missing"``); an EMPTY list means the read surface is intact.
    Raises ``SchemaCheckError`` if the DB itself can't be opened, so "can't find the
    DB" stays distinct from "the schema drifted".
    """
    if not Path(db_path).exists():  # don't let sqlite3.connect() create an empty file
        raise SchemaCheckError(f"emdash DB not found at {db_path}")
    problems: list[str] = []
    try:
        with _db(db_path) as conn:
            for table, cols in READ_SCHEMA.items():
                # PRAGMA can't be parameter-bound; the table name is our own constant,
                # never user input, so the f-string is safe here.
                present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not present:
                    problems.append(f"table {table!r} missing (or has no columns)")
                    continue
                problems.extend(f"{table}.{col} missing" for col in cols if col not in present)
    except sqlite3.Error as exc:
        raise SchemaCheckError(f"cannot read emdash DB {db_path}: {exc}") from exc
    return problems


def task_state(db_path: str, name: str) -> str:
    """READ-ONLY: is the emdash task `name` live, archived, or absent in THIS account's
    emdash? Returns "live" | "archived" | "absent" | "unknown".

    This is the source of truth for the session-reuse decision, because the DOM is not:
    emdash VIRTUALIZES the sidebar, so a task scrolled out of view isn't in the page at
    all — indistinguishable, to a DOM query, from a task that never existed. That false
    negative made the runner spawn a duplicate session and orphan the live one's context
    (observed 2026-07-15: eva's org-research thread, task provably present and
    un-archived, reported TASK_NOT_FOUND). sqlite always knows, in one query.

    "unknown" (missing/unreadable/drifted DB) is deliberately distinct from "absent": a
    read failure must never be mistaken for "gone", or we're back to duplicating live
    sessions. Callers degrade to the CDP verdict on "unknown" — see execute.execute_turn.

    Names aren't unique in emdash's schema, so the newest row wins: an old archived
    namesake must not report a live task as gone.
    """
    if not Path(db_path).exists():          # don't let sqlite3.connect() create one
        return "unknown"
    try:
        with _db(db_path) as conn:
            row = conn.execute(
                "SELECT archived_at FROM tasks WHERE name=? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (name,),
            ).fetchone()
    except sqlite3.Error:
        return "unknown"
    if row is None:
        return "absent"
    return "archived" if row["archived_at"] else "live"


def _agent_statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """READ-ONLY: {task_id: agent_status} — emdash's own answer to "is this session
    working right now", which nothing else on this box can give.

    Every other liveness signal here is an INFERENCE from writes: the server used to
    call a session "running" when its transcript had grown in the last 120s. That
    misreads both directions of the same silence — a session inside a long tool call
    writes nothing for minutes and reads as finished (the reported symptom: "it says
    finished, but it's still working when I click in"), while one that just stopped
    keeps reading as running until the window expires. Emdash sets this flag when it
    starts and stops driving the agent, so it answers directly.

    FAIL-SOFT to `{}` on any read error, unlike its caller: this is an ENRICHMENT of
    the session report, not the report. An older emdash without the column must cost
    us the flag (the server then falls back to its recency heuristic), never the whole
    report — raising here would clear every RunnerBinding server-side.

    A task can own several conversations. `working` on ANY of them wins; otherwise the
    newest one answers (rows arrive oldest-first, so a later row overwrites).
    """
    try:
        rows = conn.execute(
            """
            SELECT task_id, COALESCE(agent_status, '') AS agent_status
            FROM conversations
            ORDER BY COALESCE(last_session_activity_at, updated_at)
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for r in rows:
        status = r["agent_status"]
        if not status or out.get(r["task_id"]) == WORKING:
            continue
        out[r["task_id"]] = status
    return out


def list_open_sessions(db_path: str, limit: int = 30) -> list[dict]:
    """READ-ONLY: the un-archived emdash tasks, newest-first, capped. Returns
    [{emdash_task, project, status, agent_status, last_interacted_at}]. The task NAME is the identity
    open_and_send targets; project is joined from `projects` for display + the continue
    turn's target.

    A MISSING db degrades to [] so the runner loop survives ("no emdash here"). A real
    READ failure raises EmdashReadError — the caller must be able to tell "I read zero
    open tasks" from "I could not read", because the two have opposite server-side
    consequences (nothing changes vs every binding is cleared).

    Only `type='task'` rows — the real sessions emdash shows in its project list.
    `type='automation-run'` rows are un-promoted automation triggers that emdash hides
    under "Automations", not sessions a human opened; including them leaked phantom rows
    into the supervisor (e.g. an 8-day-old `plain-keys-rescue`) that don't appear in the
    emdash UI. `status` is the TASK's status, always 'in_progress' in practice (emdash
    never updates it); `agent_status` is the per-conversation one that does move — see
    `_agent_statuses`, and note it is "" whenever this emdash could not answer."""
    if not Path(db_path).exists():
        return []
    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT t.id AS task_id,
                       t.name AS emdash_task,
                       COALESCE(p.name, '') AS project,
                       COALESCE(t.status, '') AS status,
                       t.last_interacted_at AS last_interacted_at
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.archived_at IS NULL AND t.deleted_at IS NULL AND t.type = 'task'
                ORDER BY t.last_interacted_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            statuses = _agent_statuses(conn)
    except sqlite3.Error as exc:
        raise EmdashReadError(f"emdash open-session read failed: {exc}") from exc
    out = []
    for r in rows:
        s = dict(r)
        # task_id is a join key, not part of the report: the server keys sessions on
        # the task NAME, and a field nothing reads is a field that goes stale.
        s["agent_status"] = statuses.get(s.pop("task_id"), "")
        out.append(s)
    return out


def list_projects(db_path: str) -> list[str]:
    """READ-ONLY: the NAMES of every project emdash holds — the repos this box can
    actually drive.

    This is what `capabilities["projects"]` should have been all along. Declared by
    hand at pairing, that list drifted the only way that matters: silently, and
    toward "cannot run". A turn at `canopy` sat QUEUED forever because nobody had
    typed `canopy` into it, while the repo sat right here in this table (labs,
    2026-07-28). An agent's slug and a repo's name are the same thing to the runner
    — both name an emdash project (`execute.py`: `agent_slug or project`) — so this
    one read answers both halves of "what can this box drive".

    Sorted so the reported list is stable: it is compared against the stored one on
    every heartbeat, and sqlite's insertion order would otherwise look like a change
    on every restart.

    Empty names are dropped here rather than left for the server to strip: a session
    turn has project="", so a stray "" in capabilities would make this runner match
    EVERY session turn via `project__in`.

    Same contract as the reads above, and it matters more here than anywhere: a
    MISSING db returns [] ("no emdash on this box" is a true answer), but a real
    read failure RAISES, so the caller omits the field instead of asserting "I can
    drive nothing". Reporting [] on a drifted schema would blank the stored list and
    make every repo turn on this runner unclaimable — the same shape as the drift
    that once blanked the supervisor via `replace_reported_sessions`, one notch
    worse.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM projects WHERE deleted_at IS NULL ORDER BY name"
            ).fetchall()
    except sqlite3.Error as exc:
        raise EmdashReadError(f"emdash project read failed: {exc}") from exc
    return [r["name"] for r in rows if r["name"]]


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
                WHERE t.archived_at IS NOT NULL AND t.deleted_at IS NULL AND t.type = 'task'
                ORDER BY t.archived_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [r["emdash_task"] for r in rows]
    except sqlite3.Error as exc:
        raise EmdashReadError(f"emdash archived-task read failed: {exc}") from exc


def session_transcript_ref(db_path: str, project: str, task: str) -> tuple[str, str] | None:
    """READ-ONLY: `(cwd, provider_session_id)` for an emdash (project, task), or None.

    The two halves of "where is this session's transcript", answered by emdash rather
    than reconstructed from a path convention emdash owns and has now changed twice.
    `cwd` is the real worktree; `provider_session_id` is Claude Code's OWN session id,
    which NAMES the .jsonl — so together they address the file exactly, with nothing
    left to guess (see `canopy_transcript.resolve_cli_transcript`, which already does
    this lookup for the cloud runner).

    Returns None — never raises, and never a partial answer — when emdash cannot
    answer: no DB, a pre-1.2 emdash without the columns, no such task, or a session
    Claude Code has not reported an id for yet (`provider_session_id` is NULL until it
    does). Every one of those is a legitimate "ask the convention instead", and the
    caller falls back. Raising here would turn "emdash is older than I expected" into
    a broken runner.

    A task can own several conversations; the newest that HAS an id wins, for the same
    reason `_agent_statuses` prefers the newest — an earlier conversation under a
    reused task name must not answer for the live one.
    """
    if not project or not task or not Path(db_path).exists():
        return None
    try:
        with _db(db_path) as conn:
            row = conn.execute(
                """
                SELECT cv.cwd AS cwd, cv.provider_session_id AS sid
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                JOIN conversations cv ON cv.task_id = t.id
                WHERE t.name = ? AND p.name = ? AND t.deleted_at IS NULL
                  AND cv.cwd IS NOT NULL AND cv.provider_session_id IS NOT NULL
                ORDER BY COALESCE(cv.last_session_activity_at, cv.updated_at) DESC
                LIMIT 1
                """,
                (task, project),
            ).fetchone()
    except sqlite3.Error:
        return None            # older emdash, locked db, drifted column — all "ask the convention"
    if row is None or not row["cwd"] or not row["sid"]:
        return None
    return row["cwd"], row["sid"]
