"""The session report carries only the repos canopy works in for this box's owner
(`report_projects` on the heartbeat reply; canopy-web `ProjectOptIn`). Filtered
before any transcript tail is read, so nothing from a plain-Claude-Code repo
leaves the box."""
from canopy_runner import server_status, sessions


class _Cfg:
    session_tail_count = 30
    session_tail_limit = 8
    session_report_seconds = 10
    session_report_limit = 100
    emdash_db = "/nonexistent"
    runner_id = "r"


OPEN = [{"emdash_task": "fix-login", "project": "canopy-web"},
        {"emdash_task": "blog-post", "project": "personal-site"}]


def _report(monkeypatch):
    sessions._PENDING_CLOSED.clear()
    sessions._tail_readers.clear()
    sessions._last_session_report = 0.0
    tailed, sent = [], {}

    class _Client:
        def report_sessions(self, runner_id, payload, archived=None, complete=False):
            sent["tasks"] = [s["emdash_task"] for s in payload]

    monkeypatch.setattr(sessions.emdash, "list_open_sessions", lambda *a, **k: [dict(s) for s in OPEN])
    monkeypatch.setattr(sessions.emdash, "list_recently_archived_tasks", lambda *a, **k: [])
    monkeypatch.setattr(sessions, "session_changed", lambda *a, **k: True)
    monkeypatch.setattr(sessions.transcript, "attach_recent_tail",
                        lambda ss, **k: tailed.extend(s["emdash_task"] for s in ss))
    monkeypatch.setattr(sessions.transcript, "attach_pending_questions", lambda *a, **k: None)
    monkeypatch.setattr(sessions.startup_watch, "attach_stall_markers", lambda *a, **k: None)
    sessions.maybe_report_sessions(_Cfg(), _Client(), now_fn=lambda: 100.0)
    return sent.get("tasks"), tailed


def test_only_opted_in_repos_are_read_or_reported(monkeypatch):
    server_status.accepted({"report_projects": ["canopy-web"]})
    try:
        sent, tailed = _report(monkeypatch)
    finally:
        server_status.accepted({})
    assert sent == ["fix-login"]
    assert tailed == ["fix-login"]


def test_no_answer_from_the_server_reports_everything(monkeypatch):
    server_status.accepted({})  # an older server, or "every repo"
    sent, _ = _report(monkeypatch)
    assert sent == ["fix-login", "blog-post"]


def test_an_empty_list_reports_nothing(monkeypatch):
    server_status.accepted({"report_projects": []})
    try:
        sent, tailed = _report(monkeypatch)
    finally:
        server_status.accepted({})
    assert sent == [] and tailed == []
