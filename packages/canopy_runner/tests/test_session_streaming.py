"""Runner live-streaming (spec 2026-07-24): tail a desired session's transcript and
ship each new conversational record (user + assistant) with its RAW record ordinal.
The resume point is the SERVER's `last_index` marker on the stream descriptor —
no in-memory offset state — so a detach, a runner restart, or an account failover
all catch up the same way: ship everything after the marker. Fake client + tmp
transcript."""
import json

from canopy_runner import main as m


class _Cfg:
    runner_id = "r"


class _Client:
    def __init__(self, streams):
        self._streams = streams
        self.posted = []          # (session_id, events)
        self.fail_posts = False

    def sync_streams(self, runner_id):
        return self._streams

    def post_session_stream(self, runner_id, session_id, events):
        if self.fail_posts:
            raise RuntimeError("network")
        self.posted.append((session_id, events))


def _asst(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def _user(text):
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


def _summary():
    return json.dumps({"type": "summary", "summary": "meta"}) + "\n"


def _desc(last_index=None):
    return {"session_id": "s1", "session_key": "echo-1", "project": "echo",
            "last_index": last_index}


def _events(client):
    return [e for _sid, ev in client.posted for e in ev]


def _setup(tmp_path, monkeypatch, content):
    m._stream_readers.clear()
    p = tmp_path / "echo.jsonl"
    p.write_text(content)
    monkeypatch.setattr(m.transcript, "resolve_transcript", lambda _proj, _task, **_k: p)
    return p


def test_first_attach_streams_forward_with_raw_ordinals(tmp_path, monkeypatch):
    """No server marker (last_index=None) => don't replay history; new records ship
    keyed by their raw position in the file, users included."""
    p = _setup(tmp_path, monkeypatch, _summary() + _user("old q") + _asst("old a"))
    c = _Client([_desc()])

    m._sync_session_streams(_Cfg(), c)          # attach: history is not replayed
    assert c.posted == []

    with open(p, "a") as f:
        f.write(_user("live q") + _asst("live a"))   # ordinals 3, 4
    m._sync_session_streams(_Cfg(), c)
    assert _events(c) == [
        {"kind": "user", "seq": 3, "index": 3, "payload": {"text": "live q"}},
        {"kind": "assistant", "seq": 4, "index": 4, "payload": {"text": "live a"}},
    ]


def test_attach_catches_up_from_the_server_marker(tmp_path, monkeypatch):
    """The server holds rows through ordinal 1; everything after ships on attach."""
    _setup(tmp_path, monkeypatch,
           _user("q1") + _asst("a1") + _user("q2") + _asst("a2"))
    c = _Client([_desc(last_index=1)])
    m._sync_session_streams(_Cfg(), c)
    assert _events(c) == [
        {"kind": "user", "seq": 2, "index": 2, "payload": {"text": "q2"}},
        {"kind": "assistant", "seq": 3, "index": 3, "payload": {"text": "a2"}},
    ]


def test_restart_resumes_from_the_server_marker(tmp_path, monkeypatch):
    """The durable version of the #368 fix: attach → stream → the runner RESTARTS
    (all in-memory state gone) while the agent keeps writing → on re-attach the
    server's marker seeds the catch-up, so nothing written while away is lost and
    no ordinal is ever shipped twice."""
    p = _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1"))
    c = _Client([_desc(last_index=None)])
    m._sync_session_streams(_Cfg(), c)          # attach
    with open(p, "a") as f:
        f.write(_asst("while watching"))        # ordinal 2
    m._sync_session_streams(_Cfg(), c)
    assert [e["index"] for e in _events(c)] == [2]

    # RESTART: every in-memory tailer is gone; the agent worked while we were down.
    m._stream_readers.clear()
    with open(p, "a") as f:
        f.write(_user("missed q") + _asst("missed a"))   # ordinals 3, 4
    # The server persisted everything shipped so far, so its marker is 2.
    c._streams = [_desc(last_index=2)]
    m._sync_session_streams(_Cfg(), c)

    assert [(e["kind"], e["index"], e["payload"]["text"]) for e in _events(c)] == [
        ("assistant", 2, "while watching"),
        ("user", 3, "missed q"),
        ("assistant", 4, "missed a"),
    ]
    seqs = [e["seq"] for e in _events(c)]
    assert len(seqs) == len(set(seqs)), f"ordinal-keyed seqs must never collide: {seqs}"


def test_failed_post_is_retried_from_the_marker_next_tick(tmp_path, monkeypatch):
    """A dropped post must not advance the local cursor past unshipped records —
    the tailer resets and the next tick re-attaches from the server marker."""
    p = _setup(tmp_path, monkeypatch, _user("q1"))
    c = _Client([_desc(last_index=None)])
    m._sync_session_streams(_Cfg(), c)          # attach at end of history
    with open(p, "a") as f:
        f.write(_asst("reply"))                 # ordinal 1
    c.fail_posts = True
    m._sync_session_streams(_Cfg(), c)          # post fails silently
    assert c.posted == []
    c.fail_posts = False
    c._streams = [_desc(last_index=0)]          # server still only has ordinal 0
    m._sync_session_streams(_Cfg(), c)          # re-attach + catch-up
    assert [(e["index"], e["payload"]["text"]) for e in _events(c)] == [(1, "reply")]


def test_detached_session_drops_its_tailer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _asst("old"))
    c = _Client([_desc()])
    m._sync_session_streams(_Cfg(), c)
    assert "s1" in m._stream_readers
    c._streams = []
    m._sync_session_streams(_Cfg(), c)
    assert "s1" not in m._stream_readers


def test_sync_streams_survives_client_error(monkeypatch):
    m._stream_readers.clear()

    class _Boom:
        def sync_streams(self, rid):
            raise RuntimeError("network")

    m._sync_session_streams(_Cfg(), _Boom())  # must not raise
    assert m._stream_readers == {}
