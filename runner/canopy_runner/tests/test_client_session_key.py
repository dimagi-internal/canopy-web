"""The server renamed the turn/session key `emdash_task_id` -> `session_key`
(it is engine-agnostic: a cloud runner's is a Claude session id). On a laptop it
is always an emdash task, so the client translates at the boundary and the rest
of this package keeps calling it that."""
from __future__ import annotations

from canopy_runner.client import Client, _emdash_plan


def _client(monkeypatch, response=None):
    sent = []
    c = Client.__new__(Client)
    monkeypatch.setattr(c, "_call", lambda method, path, body=None, **k:
                        sent.append((path, body)) or (200, response))
    return c, sent


def test_finish_sends_the_new_name(monkeypatch):
    c, sent = _client(monkeypatch)
    c.finish("t-1", note="ok", emdash_task_id="c-daily-turn-ad53")
    assert sent == [("/turns/t-1/finish",
                     {"status": "done", "result_note": "ok", "session_key": "c-daily-turn-ad53"})]


def test_record_session_sends_the_new_name(monkeypatch):
    c, sent = _client(monkeypatch, {"reuse": True, "session_key": "c-x-1234"})
    plan = c.record_session("r-1", "echo", "echo:t-1", emdash_task_id="c-x-1234")
    assert sent[0][1]["session_key"] == "c-x-1234"
    assert "emdash_task_id" not in sent[0][1]
    assert plan["emdash_task_id"] == "c-x-1234"


def test_a_plan_reads_either_spelling():
    assert _emdash_plan({"session_key": "new"})["emdash_task_id"] == "new"
    assert _emdash_plan({"emdash_task_id": "old"})["emdash_task_id"] == "old"
    assert _emdash_plan(None)["emdash_task_id"] == ""
