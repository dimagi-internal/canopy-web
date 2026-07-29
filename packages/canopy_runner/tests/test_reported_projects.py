"""The runner reports the repos it can drive on its healthy heartbeat.

`capabilities["projects"]` used to be typed by a human at pairing and never
revisited, so a turn at a repo the box HAS sat QUEUED forever (labs 2026-07-28,
repo `canopy`). Now the box answers for itself.

The property under test everywhere below is the absent-vs-empty distinction:
`None` means "I could not tell" and the server leaves its stored list alone;
`[]` means "I genuinely have none" and empties it. Sending `[]` when emdash is
merely unreadable would blank the list and make every repo turn on this runner
unclaimable — the `replace_reported_sessions` drift with a bigger blast radius.
"""
import sqlite3

from canopy_runner import main as main_mod


def _db(tmp_path, *names) -> str:
    path = tmp_path / "emdash4.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("CREATE TABLE projects (id TEXT, name TEXT, path TEXT);")
    for i, n in enumerate(names):
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (f"p{i}", n, f"/x/{n}"))
    conn.commit()
    conn.close()
    return str(path)


class _Cfg:
    def __init__(self, db):
        self.emdash_db = db


def test_reports_what_emdash_holds(tmp_path):
    cfg = _Cfg(_db(tmp_path, "canopy-web", "canopy"))
    assert main_mod._reported_projects(cfg) == ["canopy", "canopy-web"]


def test_reports_none_when_emdash_cannot_be_read(tmp_path, caplog):
    """NOT []. The whole safety property: an unreadable DB must leave the server's
    stored list alone, not blank it."""
    bad = tmp_path / "bad.db"
    sqlite3.connect(str(bad)).execute("CREATE TABLE projects (id TEXT)")  # no `name`
    assert main_mod._reported_projects(_Cfg(str(bad))) is None


def test_reports_empty_for_a_box_with_no_emdash(tmp_path):
    """A missing DB is a true "no emdash here", and [] is the honest report —
    distinct from the unreadable case above, which is the point."""
    assert main_mod._reported_projects(_Cfg(str(tmp_path / "nope.db"))) == []


def test_an_unreadable_read_is_logged_loudly(tmp_path, caplog):
    """This is the silent-degradation class `verify-emdash` exists for — it must
    not pass in silence, or a fleet-wide routing outage has no breadcrumb."""
    bad = tmp_path / "bad.db"
    sqlite3.connect(str(bad)).execute("CREATE TABLE projects (id TEXT)")
    with caplog.at_level("WARNING"):
        main_mod._reported_projects(_Cfg(str(bad)))
    assert any("verify-emdash" in r.message for r in caplog.records)


# The heartbeat WIRING (that a healthy tick actually carries this) is asserted in
# test_main.py, on the proven run_once scaffolding — duplicating that harness here
# would test the stubs more than the runner.
