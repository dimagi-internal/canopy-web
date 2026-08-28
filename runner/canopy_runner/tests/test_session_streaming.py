"""Runner live-streaming (spec 2026-07-24): tail a desired session's transcript and
ship each new conversational row (user + assistant + tool calls) with its COMPOSITE
transcript ordinal (record * BLOCK_STRIDE + block).
The resume point is the SERVER's `last_index` marker on the stream descriptor —
no in-memory offset state — so a detach, a runner restart, or an account failover
all catch up the same way: ship everything after the marker. Fake client + tmp
transcript."""
import json

from canopy_runner import transcript
from canopy_runner import streams
from canopy_runner.chat_bridge import compose_index as _ix


class _Cfg:
    runner_id = "r"
    emdash_db = ""      # no emdash DB in these tests: resolution falls back to the convention


class _Client:
    def __init__(self, streams):
        self._streams = streams
        self.posted = []          # (session_id, events)
        self.transcript_ids = []  # the transcript id shipped with each post
        self.fail_posts = False

    def sync_streams(self, runner_id):
        return self._streams

    def post_session_stream(self, runner_id, session_id, events, transcript_id=""):
        if self.fail_posts:
            raise RuntimeError("network")
        self.posted.append((session_id, events))
        self.transcript_ids.append(transcript_id)


def _asst(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def _user(text):
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


def _summary():
    return json.dumps({"type": "summary", "summary": "meta"}) + "\n"


def _desc(last_index=None, first_index=..., transcript_id="echo"):
    """A stream descriptor. `first_index` defaults to "the server holds a
    contiguous history up to last_index" (0 when there is a marker, None when
    there isn't), which is what every caller here means.

    `transcript_id` defaults to the stem of the transcript `_setup` writes, i.e.
    "the markers below are markers into the file this runner is about to read".
    Passing a different one is how a caller says the session was swapped out from
    under the binding (issue #615) — see the tests at the bottom."""
    return {"session_id": "s1", "session_key": "echo-1", "project": "echo",
            "last_index": last_index,
            "first_index": (None if last_index is None else 0)
            if first_index is ... else first_index,
            "transcript_id": transcript_id, "live": True}


def _events(client):
    return [e for _sid, ev in client.posted for e in ev]


def _setup(tmp_path, monkeypatch, content):
    streams._stream_readers.clear()
    p = tmp_path / "echo.jsonl"
    p.write_text(content)
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _proj, _task, **_k: p)
    return p


def test_first_attach_ships_the_whole_history(tmp_path, monkeypatch):
    """The server holds nothing => ship EVERYTHING, then stream forward.

    Deliberately replaces "don't replay history". Forward-only on first sight is
    what left the server holding 983 of 6119 transcript rows across 12 live
    sessions (labs, 2026-07-31), 8 of them at exactly zero — and it is why "Load
    full session" had to go and ask the runner at read time, which measured 14.6s
    against a client that waited 1.2s. The transcript is the durable source, so
    first sight of a session is precisely when to capture it.
    """
    p = _setup(tmp_path, monkeypatch, _summary() + _user("old q") + _asst("old a"))
    c = _Client([_desc()])

    streams.sync_session_streams(_Cfg(), c)          # attach: history IS captured
    assert [(e["kind"], e["index"], e["payload"]["text"]) for e in _events(c)] == [
        ("user", _ix(1), "old q"),                   # record 0 is the summary
        ("assistant", _ix(2), "old a"),
    ]

    with open(p, "a") as f:
        f.write(_user("live q") + _asst("live a"))   # ordinals 3, 4
    streams.sync_session_streams(_Cfg(), c)
    assert _events(c)[-2:] == [
        {"kind": "user", "seq": _ix(3), "index": _ix(3), "payload": {"text": "live q"}},
        {"kind": "assistant", "seq": _ix(4), "index": _ix(4), "payload": {"text": "live a"}},
    ]


def test_attach_ships_history_when_the_server_is_missing_the_head(tmp_path, monkeypatch):
    """A marker alone is not enough to decide what to send.

    Rows can only be appended above the high-water mark, so a server holding
    448..1024 of a transcript that starts at 0 can never repair itself by
    streaming — labs had a session pinned at 8.6% for exactly this reason.
    `first_index` is what makes the hole visible, and the ordinal-keyed write
    makes re-shipping the rows it already has free.
    """
    _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1") + _user("q2"))
    c = _Client([_desc(last_index=_ix(2), first_index=_ix(1))])   # missing record 0
    streams.sync_session_streams(_Cfg(), c)
    assert [e["index"] for e in _events(c)] == [_ix(0), _ix(1), _ix(2)]


def test_attach_catches_up_from_the_server_marker(tmp_path, monkeypatch):
    """The server holds rows through ordinal 1; everything after ships on attach."""
    _setup(tmp_path, monkeypatch,
           _user("q1") + _asst("a1") + _user("q2") + _asst("a2"))
    c = _Client([_desc(last_index=_ix(1))])
    streams.sync_session_streams(_Cfg(), c)
    assert _events(c) == [
        {"kind": "user", "seq": _ix(2), "index": _ix(2), "payload": {"text": "q2"}},
        {"kind": "assistant", "seq": _ix(3), "index": _ix(3), "payload": {"text": "a2"}},
    ]


def test_restart_resumes_from_the_server_marker(tmp_path, monkeypatch):
    """The durable version of the #368 fix: attach → stream → the runner RESTARTS
    (all in-memory state gone) while the agent keeps writing → on re-attach the
    server's marker seeds the catch-up, so nothing written while away is lost and
    no ordinal is ever shipped twice."""
    p = _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1"))
    # The server already holds the history, so attach has only forward work to do
    # — this test is about the restart, not the first-sight capture.
    c = _Client([_desc(last_index=_ix(1))])
    streams.sync_session_streams(_Cfg(), c)          # attach
    with open(p, "a") as f:
        f.write(_asst("while watching"))        # ordinal 2
    streams.sync_session_streams(_Cfg(), c)
    assert [e["index"] for e in _events(c)] == [_ix(2)]

    # RESTART: every in-memory tailer is gone; the agent worked while we were down.
    streams._stream_readers.clear()
    with open(p, "a") as f:
        f.write(_user("missed q") + _asst("missed a"))   # ordinals 3, 4
    # The server persisted everything shipped so far, so its marker is 2.
    c._streams = [_desc(last_index=_ix(2))]
    streams.sync_session_streams(_Cfg(), c)

    assert [(e["kind"], e["index"], e["payload"]["text"]) for e in _events(c)] == [
        ("assistant", _ix(2), "while watching"),
        ("user", _ix(3), "missed q"),
        ("assistant", _ix(4), "missed a"),
    ]
    seqs = [e["seq"] for e in _events(c)]
    assert len(seqs) == len(set(seqs)), f"ordinal-keyed seqs must never collide: {seqs}"


def test_failed_post_is_retried_from_the_marker_next_tick(tmp_path, monkeypatch):
    """A dropped post must not advance the local cursor past unshipped records —
    the tailer resets and the next tick re-attaches from the server marker."""
    p = _setup(tmp_path, monkeypatch, _user("q1"))
    # Server already holds record 0, so attach ships nothing and this test stays
    # about the retry rather than the first-sight capture.
    c = _Client([_desc(last_index=_ix(0))])
    streams.sync_session_streams(_Cfg(), c)          # attach: nothing new to ship
    assert c.posted == []
    with open(p, "a") as f:
        f.write(_asst("reply"))                 # ordinal 1
    c.fail_posts = True
    streams.sync_session_streams(_Cfg(), c)          # post fails silently
    assert c.posted == []
    c.fail_posts = False
    c._streams = [_desc(last_index=_ix(0))]     # server still only has record 0
    streams.sync_session_streams(_Cfg(), c)          # re-attach + catch-up
    assert [(e["index"], e["payload"]["text"]) for e in _events(c)] == [(_ix(1), "reply")]


def test_detached_session_drops_its_tailer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _asst("old"))
    c = _Client([_desc()])
    streams.sync_session_streams(_Cfg(), c)
    assert "s1" in streams._stream_readers
    c._streams = []
    streams.sync_session_streams(_Cfg(), c)
    assert "s1" not in streams._stream_readers


def test_sync_streams_survives_client_error(monkeypatch):
    streams._stream_readers.clear()

    class _Boom:
        def sync_streams(self, rid):
            raise RuntimeError("network")

    streams.sync_session_streams(_Cfg(), _Boom())  # must not raise
    assert streams._stream_readers == {}


# ---------------------------------------------------------------------------
# Transcript identity (issue #615) — an emdash task NAME is not a conversation
# ---------------------------------------------------------------------------


def test_markers_from_another_transcript_are_discarded(tmp_path, monkeypatch):
    """The failure that pinned a live session to a day-old one for 23 hours.

    A binding is keyed on the emdash task NAME, so closing "bednet" and opening
    another one re-points it at a NEW conversation while the old one's rows stay.
    `last_index` is then a marker into the PREDECESSOR's file, and a shorter
    successor sits entirely below it — measured on prod: last_index 37,696 from a
    593-record predecessor against a live 384-record transcript whose highest
    possible ordinal was 24,575. Resuming from that marker ships nothing, forever.

    A marker only licenses skipping when it names the file we are reading.
    """
    _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1"))
    # The server's rows came from some other conversation, and its high-water mark
    # is far above anything this transcript can produce.
    c = _Client([_desc(last_index=_ix(999), first_index=0, transcript_id="a-previous-session")])
    streams.sync_session_streams(_Cfg(), c)
    assert [e["index"] for e in _events(c)] == [_ix(0), _ix(1)], (
        "a marker from a different transcript must not suppress this one"
    )
    assert c.transcript_ids == ["echo"], "the ship must name the transcript it came from"


def test_absent_server_transcript_id_ships_the_whole_history(tmp_path, monkeypatch):
    """A session whose rows predate the provenance field has rows of UNKNOWN
    origin — possibly a previous task's. Ship everything so the server can replace
    them wholesale; that is what makes the fix self-healing on deploy rather than
    needing a hand-run reset per session."""
    _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1") + _user("q2"))
    c = _Client([_desc(last_index=_ix(1), first_index=0, transcript_id="")])
    streams.sync_session_streams(_Cfg(), c)
    assert [e["index"] for e in _events(c)] == [_ix(0), _ix(1), _ix(2)]


def test_a_swapped_session_restarts_on_the_new_transcript(tmp_path, monkeypatch):
    """The task behind a live reader is replaced. Nothing about the binding
    changes — same session id, same name — so without re-resolving every tick the
    runner would tail a file nobody writes to for the rest of its life."""
    old = _setup(tmp_path, monkeypatch, _user("old q") + _asst("old a"))
    c = _Client([_desc()])
    streams.sync_session_streams(_Cfg(), c)
    assert [e["payload"]["text"] for e in _events(c)] == ["old q", "old a"]

    # emdash task "echo-1" is closed and a new one opens under the same name.
    new = tmp_path / "successor.jsonl"
    new.write_text(_user("new q") + _asst("new a"))
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _p, _t, **_k: new)
    # The server still advertises the OLD transcript and its marker.
    c._streams = [_desc(last_index=_ix(1), transcript_id=old.stem)]
    streams.sync_session_streams(_Cfg(), c)

    assert [e["payload"]["text"] for e in _events(c)][-2:] == ["new q", "new a"]
    assert c.transcript_ids[-1] == "successor"
    assert streams._stream_readers["s1"]["transcript_id"] == "successor"
