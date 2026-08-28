"""Runner backfill: read a session's full transcript and ship it as {role,text}
messages when the server asks. Fake client + tmp transcript."""
import json

from canopy_runner import transcript
from canopy_runner import streams
from canopy_runner.chat_bridge import compose_index as _ix


class _Cfg:
    runner_id = "r"
    emdash_db = ""      # no emdash DB here: resolution falls back to the convention


class _Client:
    def __init__(self, backfills):
        self._backfills = backfills
        self.shipped = []  # (session_id, messages)
        self.finals = []   # the `final` flag per POST
        self.transcript_ids = []  # which transcript each POST named (issue #615)

    def sync_backfills(self, runner_id):
        return self._backfills

    def post_session_backfill(self, runner_id, session_id, messages, final=True,
                              transcript_id=""):
        self.shipped.append((session_id, messages))
        self.finals.append(final)
        self.transcript_ids.append(transcript_id)


def _asst(t):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n"


def _user(t):
    return json.dumps({"type": "user", "message": {"content": t}}) + "\n"


def _summary():
    return json.dumps({"type": "summary", "summary": "meta"}) + "\n"


def test_drains_backfill_and_ships_full_transcript_with_ordinals(tmp_path, monkeypatch):
    """Every message carries its composite transcript ordinal (the leading summary
    record shifts them), so the server keys backfilled rows on exactly the ordinals
    the live stream uses — that identity is what makes the two idempotent."""
    p = tmp_path / "echo.jsonl"
    p.write_text(_summary() + _user("q1") + _asst("a1") + _user("q2") + _asst("a2"))
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _proj, _task, **_k: p)

    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    streams.drain_backfills(_Cfg(), c)

    assert len(c.shipped) == 1
    sid, messages = c.shipped[0]
    assert sid == "s1"
    assert [(x["index"], x["role"], x["text"]) for x in messages] == [
        (_ix(1), "user", "q1"), (_ix(2), "assistant", "a1"),
        (_ix(3), "user", "q2"), (_ix(4), "assistant", "a2"),
    ]


def test_drain_skips_when_transcript_missing(monkeypatch):
    monkeypatch.setattr(transcript, "resolve_transcript", lambda *a, **k: None)
    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    streams.drain_backfills(_Cfg(), c)
    assert c.shipped == []  # nothing to ship; runner-offline path stays server-side


# --- chunking: one POST cannot carry a long session ------------------------
# (chunk_rows itself is unit-tested in packages/canopy_transcript/tests/test_batching.py)


def test_long_transcript_ships_in_chunks_and_only_the_last_is_final(tmp_path, monkeypatch):
    """The whole payload used to go in ONE request. Against Django's
    DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) that is an unhandled 500 raised BEFORE the
    view runs — no 4xx to read, no partial write, and the runner retries forever.
    Measured over 193 local transcripts, one full-history payload is already
    2.57 MB. `final` is what stops an early chunk from retiring the request while
    the rest is still in flight, which would strand a permanently partial history.
    """
    p = tmp_path / "echo.jsonl"
    p.write_text("".join(_asst("y" * 4000) for _ in range(400)))
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _proj, _task, **_k: p)

    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    streams.drain_backfills(_Cfg(), c)

    assert len(c.shipped) > 1, "a 1.6 MB transcript must not go in one request"
    assert c.finals == [False] * (len(c.shipped) - 1) + [True]
    for _sid, batch in c.shipped:
        assert len(json.dumps(batch).encode()) < 2_621_440
    shipped = [m["index"] for _sid, batch in c.shipped for m in batch]
    assert shipped == sorted(shipped) and len(shipped) == 400


def test_empty_transcript_still_posts_once_so_the_request_is_retired(tmp_path, monkeypatch):
    """Nothing to ship is not nothing to say: only a `final` chunk clears
    `backfill_requested`, so a silent skip would leave the ask set forever."""
    p = tmp_path / "echo.jsonl"
    p.write_text(_summary())
    monkeypatch.setattr(transcript, "resolve_transcript", lambda _proj, _task, **_k: p)
    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    streams.drain_backfills(_Cfg(), c)
    assert c.shipped == [("s1", [])] and c.finals == [True]
