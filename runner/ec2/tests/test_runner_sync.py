"""The box reads its work through `GET /runners/{id}/sync` when `v` moves.

`v` (the runner's sync version) rides heartbeat replies, acks and doorbell frames;
`/sync` answers 304 behind its ETag when nothing moved, and a full read every
SYNC_RECONCILE_SECONDS finds work whose doorbell and bump were both missed. A
server without `/sync` is read drain by drain, as before.
"""
from __future__ import annotations


class _Server:
    """A fake canopy-web: `/sync` at a version, with whatever work is pending."""

    def __init__(self, version=1, work=None, has_sync=True):
        self.version = version
        self.work = dict(work or {})
        self.has_sync = has_sync
        self.calls = []

    def api(self, method, path, body=None, *, headers=None, quiet=False, **_kw):
        self.calls.append((method, path, dict(headers or {})))
        if path.endswith("/sync"):
            if not self.has_sync:
                return 404, None
            if (headers or {}).get("If-None-Match") == f'"v{self.version}"':
                return 304, None
            return 200, {"version": self.version, **self.work}
        return 200, {}

    def syncs(self):
        return [c for c in self.calls if c[1].endswith("/sync")]


def _wire(mod, monkeypatch, server):
    monkeypatch.setattr(mod, "_api", server.api)
    mod._reset_sync()


def test_the_first_read_is_whole_and_an_unchanged_v_reads_nothing(cloud_runner, monkeypatch):
    server = _Server(version=4)
    _wire(cloud_runner, monkeypatch, server)
    assert cloud_runner.maybe_sync("r", now=1000.0)
    assert server.syncs()[-1][2] == {}  # whole: no ETag yet
    cloud_runner.note_version(4)
    assert not cloud_runner.maybe_sync("r", now=1001.0)  # same v: not due
    assert len(server.syncs()) == 1


def test_a_moved_v_reads_behind_the_etag(cloud_runner, monkeypatch):
    server = _Server(version=4)
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    server.version = 5
    server.work = {"interrupts": [{"session_id": "s1", "session_key": "k1", "interrupt_id": "i1"}]}
    stopped = []
    monkeypatch.setattr(cloud_runner, "stop_session", lambda key, why: stopped.append(key) or [])
    cloud_runner.note_version(5)
    assert cloud_runner.maybe_sync("r", now=1001.0)
    assert server.syncs()[-1][2] == {"If-None-Match": '"v4"'}
    assert stopped == ["k1"]
    assert any(p.endswith("/session-interrupt-result") for _, p, _ in server.calls)
    assert cloud_runner._SYNC["version"] == 5


def test_a_frame_without_new_work_costs_a_304(cloud_runner, monkeypatch):
    server = _Server(version=7)
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    cloud_runner.note_version(None)  # a frame or reply with no `v` (an older server)
    assert cloud_runner.maybe_sync("r", now=1001.0)
    assert server.syncs()[-1][2] == {"If-None-Match": '"v7"'}
    assert cloud_runner._SYNC["version"] == 7


def test_the_reconcile_finds_work_whose_frame_was_dropped(cloud_runner, monkeypatch):
    """Work recorded with its bump, but the frame never arrived and no beat carried
    `v`: the full read on the reconcile clock still finds it."""
    server = _Server(version=2)
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    server.version = 3
    server.work = {"cancel_turn_ids": ["t-9"]}
    stopped = []
    monkeypatch.setattr(cloud_runner, "request_stop", lambda tid, why: stopped.append(tid) or True)
    assert not cloud_runner.maybe_sync("r", now=1000.0 + cloud_runner.SYNC_RECONCILE_SECONDS - 1)
    assert cloud_runner.maybe_sync("r", now=1000.0 + cloud_runner.SYNC_RECONCILE_SECONDS)
    assert server.syncs()[-1][2] == {}  # whole, not behind the ETag
    assert stopped == ["t-9"]


def test_a_session_this_box_recorded_is_read_whole(cloud_runner, monkeypatch):
    """A new binding moves no version: recording one makes the next read whole."""
    server = _Server(version=2)
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    cloud_runner.sync_refresh()
    assert cloud_runner.maybe_sync("r", now=1001.0)
    assert server.syncs()[-1][2] == {}


def test_a_server_without_sync_is_read_drain_by_drain(cloud_runner, monkeypatch):
    server = _Server(has_sync=False)
    _wire(cloud_runner, monkeypatch, server)
    assert not cloud_runner.maybe_sync("r", now=1000.0)
    assert not cloud_runner.sync_in_use()
    assert cloud_runner._heartbeat_body([])["sync"] == 0
    assert cloud_runner.sync_streams() is None  # the views GET /streams
    assert cloud_runner.mint_wanted()  # the sign-in poll runs every tick
    cloud_runner.note_version(9)
    assert not cloud_runner.maybe_sync("r", now=1001.0)
    assert len(server.syncs()) == 1  # never asked again


def test_the_heartbeat_says_it_reads_sync(cloud_runner, monkeypatch):
    _wire(cloud_runner, monkeypatch, _Server())
    assert cloud_runner._heartbeat_body([])["sync"] == 1


def test_a_beat_or_ack_carries_v(cloud_runner, monkeypatch):
    server = _Server(version=3)
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    cloud_runner._observe_beat({"paused": False, "v": 3})
    assert not cloud_runner._sync_due(1001.0)[0]
    cloud_runner._observe_beat({"paused": False, "sync_version": 4})
    assert cloud_runner._sync_due(1001.0)[0]


def test_the_sign_in_poll_runs_only_when_due(cloud_runner, monkeypatch):
    server = _Server(version=1, work={"mint_due": False})
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    claims = []
    monkeypatch.setattr(cloud_runner, "_drain_mint", lambda rid: claims.append(rid))
    cloud_runner._maybe_drain_mint("r")
    assert claims == []
    cloud_runner._MINT_RUNG.set()  # a `mint` frame
    cloud_runner._maybe_drain_mint("r")
    assert claims == ["r"]
    server.version, server.work = 2, {"mint_due": True}
    cloud_runner.note_version(2)
    cloud_runner.maybe_sync("r", now=1001.0)
    cloud_runner._maybe_drain_mint("r")
    assert claims == ["r", "r"]


def test_stream_list_comes_from_the_last_answer(cloud_runner, monkeypatch):
    server = _Server(version=1, work={"streams": [{"session_id": "s1", "session_key": "k"}]})
    _wire(cloud_runner, monkeypatch, server)
    cloud_runner.maybe_sync("r", now=1000.0)
    assert cloud_runner.sync_streams() == [{"session_id": "s1", "session_key": "k"}]


def test_a_failed_read_stays_due(cloud_runner, monkeypatch):
    calls = []

    def api(method, path, body=None, **kw):
        calls.append(path)
        return 502, None

    monkeypatch.setattr(cloud_runner, "_api", api)
    cloud_runner._reset_sync()
    cloud_runner.maybe_sync("r", now=1000.0)
    assert cloud_runner.sync_in_use()
    assert cloud_runner._sync_due(1001.0)[0]
