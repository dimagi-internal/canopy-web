"""Coaching: the owner's own machine ships their sessions in full (`ship_full`).

The server lists a coached owner's sessions even once retired; the runner ships
each one's whole history once, stops when the server stops listing it (the last
grant revoked), and never ships one from a repo the owner has not opted in to."""
import json

from canopy_runner import server_status, streams, transcript


class _Cfg:
    runner_id = "r"
    emdash_db = ""


class _Client:
    def __init__(self, listed):
        self.listed = listed
        self.posted = []

    def sync_streams(self, runner_id):
        return self.listed

    def post_session_stream(self, runner_id, session_id, events, transcript_id=""):
        self.posted.append((session_id, events))


def _user(text):
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


def _asst(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def _coached(*, archived=True, project="echo"):
    return {"session_id": "s1", "session_key": "echo-1", "project": project,
            "last_index": None, "first_index": None, "transcript_id": "echo",
            "live": False, "ship_full": True, "archived": archived}


def _texts(client):
    return [e["payload"]["text"] for _sid, ev in client.posted for e in ev]


def _setup(tmp_path, monkeypatch, content, *, opted_in=None):
    streams._stream_readers.clear()
    p = tmp_path / "echo.jsonl"
    p.write_text(content)
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _proj, _task, **_k: p)
    monkeypatch.setattr(server_status, "report_projects", lambda: opted_in)
    return p


def test_a_coached_retired_session_ships_its_whole_history_once(tmp_path, monkeypatch):
    """The server holds none of it, so the whole history goes up on first sight —
    then it is settled: a file nothing writes to is not re-read every tick."""
    _setup(tmp_path, monkeypatch, _user("q1") + _asst("a1"))
    c = _Client([_coached()])
    streams.sync_session_streams(_Cfg(), c)
    assert _texts(c) == ["q1", "a1"]

    resolved = []
    monkeypatch.setattr(transcript, "resolve_transcript", lambda *_a, **_k: resolved.append(1))
    streams.sync_session_streams(_Cfg(), c)
    assert resolved == [] and len(c.posted) == 1


def test_a_coached_live_session_keeps_shipping_as_it_grows(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch, _user("q1"))
    c = _Client([_coached(archived=False)])
    streams.sync_session_streams(_Cfg(), c)
    with open(p, "a") as f:
        f.write(_asst("a1"))
    streams.sync_session_streams(_Cfg(), c)
    assert _texts(c) == ["q1", "a1"]


def test_unflagged_it_stops_shipping(tmp_path, monkeypatch):
    """Revoking the last grant takes the session off the server's list: its tailer
    is dropped and nothing more of it ships."""
    p = _setup(tmp_path, monkeypatch, _user("q1"))
    c = _Client([_coached(archived=False)])
    streams.sync_session_streams(_Cfg(), c)
    assert len(c.posted) == 1
    c.listed = []                                     # coaching turned off
    with open(p, "a") as f:
        f.write(_asst("after"))
    streams.sync_session_streams(_Cfg(), c)
    assert len(c.posted) == 1 and "s1" not in streams._stream_readers


def test_a_repo_not_opted_in_never_ships(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _user("private") + _asst("diary"),
           opted_in=frozenset({"canopy-web"}))
    c = _Client([_coached(project="diary")])
    streams.sync_session_streams(_Cfg(), c)
    assert c.posted == []

    c.listed = [_coached(project="canopy-web")]
    streams.sync_session_streams(_Cfg(), c)
    assert _texts(c) == ["private", "diary"]
