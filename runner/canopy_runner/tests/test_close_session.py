"""Closing, from the runner's side: delete the emdash task, then TELL the server.

Telling matters. The server wrote nothing when it relayed the close, and absence
alone takes SESSION_LIVE_WINDOW (3 min) to retire a row — the `archived:` closing
signal says it in one report."""
from canopy_runner import close, sessions


# Same lightweight Config stand-in as test_session_report_live.py's `_Cfg` — only
# the fields `maybe_report_sessions` actually reads.
class _Cfg:
    session_tail_count = 30
    session_tail_limit = 8
    session_report_seconds = 10
    session_report_limit = 100
    emdash_db = "/nonexistent"
    runner_id = "r"


def _cfg(monkeypatch):
    return _Cfg()


def test_a_close_queues_the_task_for_the_closing_signal(monkeypatch):
    sessions._PENDING_CLOSED.clear()
    monkeypatch.setattr(close.cdp_control, "close_task",
                        lambda t, port=9222: {"ok": True, "action": "deleted"})
    assert close.close_session("ddd") == "deleted"
    assert sessions._PENDING_CLOSED == {"ddd"}


def test_an_already_absent_task_still_queues_the_signal(monkeypatch):
    """The task is gone but the server may not know: a human deleted it in emdash
    between the phone rendering the list and the tap landing."""
    sessions._PENDING_CLOSED.clear()
    monkeypatch.setattr(close.cdp_control, "close_task",
                        lambda t, port=9222: {"ok": True, "action": "absent"})
    assert close.close_session("gone") == "absent"
    assert sessions._PENDING_CLOSED == {"gone"}


def test_a_failed_delete_queues_nothing(monkeypatch):
    """The row must stay where it is. Nothing was written server-side, so doing
    nothing here is already the correct outcome — reporting the close would be the
    only way to get it wrong."""
    sessions._PENDING_CLOSED.clear()

    def boom(task, port=9222):
        raise close.cdp_control.CDPError("no delete control")

    monkeypatch.setattr(close.cdp_control, "close_task", boom)
    try:
        close.close_session("ddd")
    except close.cdp_control.CDPError:
        pass
    else:
        raise AssertionError("expected the CDP error to propagate to the caller")
    assert sessions._PENDING_CLOSED == set()


def test_a_pending_close_forces_a_report_even_when_nothing_changed(monkeypatch):
    """The report is change-driven plus a heartbeat. A close must not wait out the
    heartbeat — that is the latency the whole relay design is trying to avoid."""
    sessions._PENDING_CLOSED.clear()
    sessions._tail_readers.clear()
    sessions._last_session_report = 0.0
    sent = {}

    class _Client:
        def report_sessions(self, runner_id, payload, archived=None):
            sent["archived"] = archived
            sent["sessions"] = payload

    cfg = _cfg(monkeypatch)
    monkeypatch.setattr(sessions.emdash, "list_open_sessions", lambda *a, **k: [])
    monkeypatch.setattr(sessions.emdash, "list_recently_archived_tasks", lambda *a, **k: [])
    monkeypatch.setattr(sessions, "session_changed", lambda *a, **k: False)
    sessions.request_close_report("ddd")
    sessions.maybe_report_sessions(cfg, _Client(), now_fn=lambda: 0.0)
    assert sent["archived"] == ["ddd"]
    assert sessions._PENDING_CLOSED == set()
