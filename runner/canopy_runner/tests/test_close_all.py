"""Closing every session on the box, or one project's — the menu bar's "Close
sessions". Each close is the phone's close (`close.close_session`), so these pin
only what is new: which sessions are picked, and that one failure stops nothing."""
from canopy_runner import close_all


class _Cfg:
    cdp_port = 9222
    emdash_db = "/nonexistent"


ROWS = [
    {"emdash_task": "a1", "project": "ace"},
    {"emdash_task": "a2", "project": "ace"},
    {"emdash_task": "editing", "project": "eva"},
    {"emdash_task": "orphan", "project": ""},
]


def _record(monkeypatch, fail=()):
    calls = []

    def fake_close(task, *, project="", cdp_port=9222, emdash_db=None, cfg=None):
        calls.append((project, task))
        if task in fail:
            raise close_all.close.CloseRefused(task)
        return "absent" if task == "a2" else "deleted"

    monkeypatch.setattr(close_all.close, "close_session", fake_close)
    return calls


def test_a_project_closes_only_its_own_sessions_aimed_by_project(monkeypatch):
    """ada's and eva's "editing" are two sessions: closing ace's must touch
    neither, and every close carries the project, never the name alone."""
    calls = _record(monkeypatch)
    result = close_all.close_all(_Cfg(), project="ace", rows=ROWS)
    assert calls == [("ace", "a1"), ("ace", "a2")]
    assert result["closed"] == ["a1"] and result["absent"] == ["a2"]


def test_all_closes_everything_but_skips_a_session_with_no_project(monkeypatch):
    calls = _record(monkeypatch)
    result = close_all.close_all(_Cfg(), rows=ROWS)
    assert calls == [("ace", "a1"), ("ace", "a2"), ("eva", "editing")]
    assert result["skipped"] == ["orphan"]


def test_one_failure_does_not_stop_the_rest(monkeypatch):
    calls = _record(monkeypatch, fail={"a1"})
    result = close_all.close_all(_Cfg(), project="ace", rows=ROWS)
    assert calls == [("ace", "a1"), ("ace", "a2")]
    assert [f["task"] for f in result["failed"]] == ["a1"]
    assert result["absent"] == ["a2"]


def test_counts_by_project_puts_the_busiest_first():
    assert list(close_all.counts_by_project(ROWS).items()) == [
        ("ace", 2), ("", 1), ("eva", 1)]
