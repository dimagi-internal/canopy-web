"""A failed close is reported to canopy, from every close path.

2026-10-12: "Close sessions" from the menu bar left three sessions open, and the
only place the reason existed was ~/.canopy/runner.log in another macOS user's
home folder. Each path now turns the failure into a sentence a person can act on
and sends it (POST /runners/{id}/close-results); Supervisor shows it."""
import types

from canopy_runner import close, close_all, session_target, streams


class _Client:
    def __init__(self, closes=()):
        self._closes = list(closes)
        self.posted: list[list[dict]] = []

    def sync_closes(self, runner_id):
        return self._closes

    def post_close_results(self, runner_id, results):
        self.posted.append(results)


def _cfg():
    return types.SimpleNamespace(cdp_port=9222, runner_id="r1", emdash_db="/nonexistent")


# ── the reason ────────────────────────────────────────────────────────────────

def test_a_refusal_says_it_could_not_tell_which_project():
    exc = close.CloseRefused("x: ambiguous", reason=session_target.AMBIGUOUS)
    assert close.failure_reason(exc) == (
        "couldn't tell which project's task this is "
        "(several projects have a task by this name)")
    assert close.failure_reason(close.CloseRefused("x")) == close.NO_PROJECT


def test_a_cdp_failure_with_emdash_down_says_emdash_is_unreachable(monkeypatch):
    monkeypatch.setattr(close.cdp_control, "cdp_healthy", lambda **k: False)
    exc = close.cdp_control.CDPError("connect ECONNREFUSED 127.0.0.1:9222")
    assert close.failure_reason(exc, cdp_port=9222) == "emdash unreachable over CDP (port 9222)"


def test_a_cdp_failure_with_emdash_up_carries_emdashs_own_words(monkeypatch):
    monkeypatch.setattr(close.cdp_control, "cdp_healthy", lambda **k: True)
    exc = close.cdp_control.CDPError("task still in emdash after delete")
    assert close.failure_reason(exc) == "emdash: task still in emdash after delete"


def test_any_other_failure_is_still_a_short_sentence():
    assert close.failure_reason(RuntimeError("x" * 500)) == "x" * 200


# ── the menu bar's close-sessions ─────────────────────────────────────────────

ROWS = [
    {"emdash_task": "a1", "project": "ace"},
    {"emdash_task": "a2", "project": "ace"},
    {"emdash_task": "orphan", "project": ""},
]


def test_close_all_reports_failures_skips_and_successes(monkeypatch):
    def fake_close(task, *, project="", **k):
        if task == "a1":
            raise close.CloseRefused("a1: ambiguous", reason=session_target.AMBIGUOUS)
        return "deleted"

    monkeypatch.setattr(close_all.close, "close_session", fake_close)
    result = close_all.close_all(_cfg(), rows=ROWS)
    client = _Client()
    close_all.report_outcomes(_cfg(), client, result)
    assert client.posted == [[
        {"session_key": "a1", "project": "ace", "ok": False,
         "error": "couldn't tell which project's task this is "
                  "(several projects have a task by this name)"},
        {"session_key": "a2", "project": "ace", "ok": True, "error": ""},
        # Never closed by name alone — and no longer dropped silently either.
        {"session_key": "orphan", "project": "", "ok": False, "error": close.NO_PROJECT},
    ]]
    assert result["failed"][0]["reason"].startswith("couldn't tell")


def test_a_report_that_cannot_be_sent_never_raises():
    class Down(_Client):
        def post_close_results(self, runner_id, results):
            raise ConnectionError("canopy down")

    close.report_results(_cfg(), Down(), [{"session_key": "a", "project": "p",
                                           "ok": False, "error": "x"}])


# ── the phone's close, drained on the poll tick ───────────────────────────────

def test_a_failed_drain_is_reported_once_per_reason(monkeypatch):
    def boom(*a, **k):
        raise close.CloseRefused("t: unknown", reason=session_target.UNKNOWN)

    monkeypatch.setattr(close, "close_session", boom)
    client = _Client([{"session_key": "t", "project": "hal", "close_error": ""}])
    streams.drain_closes(_cfg(), client)
    assert client.posted == [[{
        "session_key": "t", "project": "hal", "ok": False,
        "error": "couldn't tell which project's task this is "
                 "(emdash's database could not be read)"}]]

    # Next tick: the server already holds that reason — nothing new to say.
    client = _Client([{"session_key": "t", "project": "hal",
                       "close_error": client.posted[0][0]["error"]}])
    streams.drain_closes(_cfg(), client)
    assert client.posted == []


def test_a_drain_that_succeeds_clears_a_held_failure(monkeypatch):
    monkeypatch.setattr(close, "close_session", lambda *a, **k: "deleted")
    client = _Client([{"session_key": "t", "project": "hal", "close_error": "old"},
                      {"session_key": "u", "project": "hal", "close_error": ""}])
    streams.drain_closes(_cfg(), client)
    assert client.posted == [[{"session_key": "t", "project": "hal", "ok": True, "error": ""}]]
