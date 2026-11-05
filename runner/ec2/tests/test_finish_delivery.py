"""A turn's finish is delivered, not merely sent.

A finish canopy-web never heard leaves the turn EXECUTING until the lease sweep
marks finished work LOST (and starts a debugger on it). Two holes made that
happen: `_api` let a raw read error (a reset, a remote disconnect, a read
timeout, a short body) escape instead of answering like "unreachable", and the
finish was sent once. These pin both shut.
"""
from __future__ import annotations

import http.client
import socket

import pytest


@pytest.fixture
def cr(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_bearer", lambda: "")
    monkeypatch.setattr(cloud_runner, "BASE_URL", "http://canopy.invalid")
    return cloud_runner


# ── _api answers a dead connection like an unreachable one ───────────────────

@pytest.mark.parametrize("exc", [
    ConnectionResetError(104, "Connection reset by peer"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    socket.timeout("The read operation timed out"),
    TimeoutError("timed out"),
])
def test_a_raw_connection_error_is_status_zero(cr, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(cr.urllib.request, "urlopen", boom)
    assert cr._api("POST", "/turns/t/finish", {"status": "done"}) == (0, None)


def test_a_short_body_after_the_headers_is_status_zero(cr, monkeypatch):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"{", 10)
    monkeypatch.setattr(cr.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert cr._api("POST", "/turns/t/finish", {"status": "done"}) == (0, None)


# ── a finish that did not land is kept and re-sent ───────────────────────────

class _Server:
    """A fake `_api` answering the finish route from a script of statuses."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        return (self.statuses.pop(0) if self.statuses else 200), None


def test_a_finish_that_landed_is_not_kept(cr, monkeypatch):
    monkeypatch.setattr(cr, "_api", _Server(200))
    assert cr._post_finish("t1", {"status": "done"})[0] == 200
    assert cr._pending_finish_ids() == []


@pytest.mark.parametrize("status", [0, 401, 429, 500, 502, 503])
def test_a_finish_that_may_still_land_is_kept_and_delivered_later(cr, monkeypatch, status):
    server = _Server(status, 200)
    monkeypatch.setattr(cr, "_api", server)
    body = {"status": "done", "result_note": "all good", "session_key": "s1"}
    assert cr._post_finish("t1", body)[0] == status
    assert cr._pending_finish_ids() == ["t1"]

    cr._retry_pending_finishes(now=cr.time.time() + cr.FINISH_RETRY_BASE_SECONDS + 1)
    assert cr._pending_finish_ids() == []
    assert server.calls[-1] == ("POST", "/turns/t1/finish", body)   # the same finish, again


@pytest.mark.parametrize("status", [404, 409, 422])
def test_an_answer_that_cannot_change_is_not_retried(cr, monkeypatch, status):
    monkeypatch.setattr(cr, "_api", _Server(status))
    cr._post_finish("t1", {"status": "done"})
    assert cr._pending_finish_ids() == []


def test_retries_back_off_and_wait_their_turn(cr, monkeypatch):
    server = _Server(503, 503, 503)
    monkeypatch.setattr(cr, "_api", server)
    t0 = cr.time.time()
    cr._post_finish("t1", {"status": "done"})
    cr._retry_pending_finishes(now=t0 + 1)              # not due yet
    assert len(server.calls) == 1

    cr._retry_pending_finishes(now=t0 + cr.FINISH_RETRY_BASE_SECONDS + 1)
    assert len(server.calls) == 2
    rec = cr._read_pending_finishes()["t1"]
    assert rec["attempts"] == 2 and rec["last"] == 503
    first_wait = rec["next_at"] - (t0 + cr.FINISH_RETRY_BASE_SECONDS + 1)

    cr._retry_pending_finishes(now=rec["next_at"])
    rec2 = cr._read_pending_finishes()["t1"]
    assert rec2["attempts"] == 3
    assert rec2["next_at"] - rec["next_at"] > first_wait   # backed off further


def test_a_terminal_answer_on_retry_drops_it(cr, monkeypatch):
    monkeypatch.setattr(cr, "_api", _Server(0, 404))
    cr._post_finish("t1", {"status": "done"})
    cr._retry_pending_finishes(now=cr.time.time() + 3600)
    assert cr._pending_finish_ids() == []


def test_it_gives_up_eventually(cr, monkeypatch):
    monkeypatch.setattr(cr, "_api", _Server(0, 0))
    cr._post_finish("t1", {"status": "done"})
    cr._retry_pending_finishes(now=cr.time.time() + cr.FINISH_GIVE_UP_SECONDS + 1)
    assert cr._pending_finish_ids() == []


def test_the_kept_finish_survives_a_restart(cr, monkeypatch, load_cloud_runner):
    monkeypatch.setattr(cr, "_api", _Server(0))
    cr._post_finish("t1", {"status": "failed", "result_note": "x"})
    fresh = load_cloud_runner()
    fresh.PENDING_FINISH_FILE = cr.PENDING_FINISH_FILE
    server = _Server(200)
    monkeypatch.setattr(fresh, "_api", server)
    fresh._retry_pending_finishes(now=fresh.time.time() + 3600)
    assert server.calls == [("POST", "/turns/t1/finish", {"status": "failed", "result_note": "x"})]
    assert fresh._pending_finish_ids() == []


def test_a_waiting_finish_keeps_its_turns_lease(cr, monkeypatch):
    # Until canopy-web hears the finish, the turn must ride the heartbeat or the
    # sweep marks it LOST — the very thing the retry exists to prevent.
    monkeypatch.setattr(cr, "_api", _Server(0))
    monkeypatch.setattr(cr, "_mark_in_flight", lambda n: None)
    cr._post_finish("t1", {"status": "done"})
    assert cr._heartbeat_body(["t2"])["active_turn_ids"] == ["t2", "t1"]


def test_the_retry_runs_off_the_polling_thread(cr, monkeypatch):
    monkeypatch.setattr(cr, "_api", _Server(0, 200))
    assert cr.maybe_retry_pending_finishes() is False   # nothing waiting
    cr._post_finish("t1", {"status": "done"})
    monkeypatch.setattr(cr, "FINISH_RETRY_BASE_SECONDS", 0.0)
    rec = cr._read_pending_finishes()
    rec["t1"]["next_at"] = 0
    cr._write_pending_finishes(rec)
    assert cr.maybe_retry_pending_finishes() is True
    cr._FINISH_RETRIER["thread"].join(5)
    assert cr._pending_finish_ids() == []


def test_every_finish_goes_through_the_kept_path():
    # A finish POSTed straight through `_api` would be sent once again.
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "cloud_runner.py").read_text()
    sites = [line for line in src.splitlines() if "/finish\"" in line and "_api(" in line]
    assert len(sites) == 2, sites   # _post_finish's first send and its retry
