"""Change-driven session reporting: report the instant a shown session's transcript
grows (live), skip when idle, heartbeat periodically."""
import json

from canopy_runner import main as m


class _Cfg:
    session_tail_count = 30
    session_tail_limit = 8
    session_report_seconds = 10
    session_report_limit = 100
    emdash_db = "/nonexistent"
    runner_id = "r"


class _Client:
    def __init__(self):
        self.reports = 0
        self.sessions_seen = []
        self.archived_seen = []

    def report_sessions(self, runner_id, sessions, archived=None):
        self.reports += 1
        self.sessions_seen.append(sessions)
        self.archived_seen.append(archived)


def _asst(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def test_reports_on_growth_skips_idle_and_heartbeats(tmp_path, monkeypatch):
    m._tail_readers.clear()
    m._last_session_report = 0.0
    p = tmp_path / "echo.jsonl"
    p.write_text(_asst("history"))
    sessions = [{"emdash_task": "echo-1", "project": "echo"}]
    monkeypatch.setattr(m.emdash, "list_open_sessions", lambda _db, limit=30: sessions)
    monkeypatch.setattr(m.transcript, "resolve_transcript", lambda _proj, _task, **_k: p)
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)

    c = _Client()
    clock = [100.0]
    now = lambda: clock[0]

    # first sight of the session -> report
    m._maybe_report_sessions(_Cfg(), c, now_fn=now)
    assert c.reports == 1

    # no transcript growth, within the heartbeat window -> skip
    clock[0] = 102.0
    m._maybe_report_sessions(_Cfg(), c, now_fn=now)
    assert c.reports == 1

    # the transcript grows (assistant streaming, no user input) -> report LIVE
    with open(p, "a") as f:
        f.write(_asst("live reply"))
    clock[0] = 103.0
    m._maybe_report_sessions(_Cfg(), c, now_fn=now)
    assert c.reports == 2

    # idle but the heartbeat window elapsed -> report
    clock[0] = 120.0
    m._maybe_report_sessions(_Cfg(), c, now_fn=now)
    assert c.reports == 3


def test_a_failed_emdash_read_skips_the_report_entirely(tmp_path, monkeypatch):
    """An empty report CLEARS every binding server-side. When we could not read, we
    must say nothing at all rather than assert emptiness."""
    m._tail_readers.clear()
    m._last_session_report = 0.0

    def _boom(_db, *_a, **_k):
        raise m.emdash.EmdashReadError("schema drift")

    monkeypatch.setattr(m.emdash, "list_open_sessions", _boom)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.reports == 0            # nothing posted
    assert c.sessions_seen == []     # and certainly not an empty list


def test_report_honours_the_configured_limit(tmp_path, monkeypatch):
    m._tail_readers.clear()
    m._last_session_report = 0.0
    seen = {}

    def _list(db, limit=30):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(m.emdash, "list_open_sessions", _list)
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    m._maybe_report_sessions(_Cfg(), _Client(), now_fn=lambda: 100.0)
    assert seen["limit"] == 100


def test_report_carries_archived_task_names(tmp_path, monkeypatch):
    m._tail_readers.clear()
    m._last_session_report = 0.0
    monkeypatch.setattr(m.emdash, "list_open_sessions", lambda _db, _l=100: [])
    monkeypatch.setattr(m.emdash, "list_recently_archived_tasks", lambda _db, _l=100: ["gone"])
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.archived_seen[-1] == ["gone"]


def test_a_failed_archived_read_still_reports_open_sessions(tmp_path, monkeypatch):
    """Fail-soft in the other direction: losing the closing signal must not cost us
    the open-session report."""
    m._tail_readers.clear()
    m._last_session_report = 0.0

    def _boom(_db, _l=100):
        raise m.emdash.EmdashReadError("drift")

    monkeypatch.setattr(m.emdash, "list_open_sessions", lambda _db, _l=100: [])
    monkeypatch.setattr(m.emdash, "list_recently_archived_tasks", _boom)
    monkeypatch.setattr(m.transcript, "attach_recent_tail", lambda _s, **_k: None)
    c = _Client()
    m._maybe_report_sessions(_Cfg(), c, now_fn=lambda: 100.0)
    assert c.reports == 1
    assert c.archived_seen[-1] == []
