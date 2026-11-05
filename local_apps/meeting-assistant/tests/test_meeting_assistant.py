"""The meeting assistant sends a live conversation off the Mac, so these pin its failure
modes: what it must not send, when it must stop, and that a retry can't quietly lose words."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app.py"


def _load():
    spec = importlib.util.spec_from_file_location("meeting_assistant_app", APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses looks its module up here
    spec.loader.exec_module(mod)
    return mod


m = _load()


def seg(text, start, speaker="Speaker 1"):
    return {"type": "segment", "text": text, "start": start, "end": start + 0.5, "speaker": speaker}


class Harness:
    def __init__(self, **kw):
        self.t = 1000.0
        self.sent, self.events, self.statuses, self.attempts = [], [], {}, []
        self.post_error = None
        self.feed = m.Feed(run_id="abc", post=self._post,
                           turn_status=lambda turn: self.statuses.get(turn, "done"),
                           log=lambda event, **f: self.events.append((event, f)),
                           now=lambda: self.t, interval=45, min_words=2, tag="tag", **kw)

    def _post(self, text, client_id):
        self.attempts.append((text, client_id))
        if self.post_error:
            raise self.post_error
        self.sent.append((text, client_id))
        return f"turn{len(self.sent)}"

    def say(self, *segments):
        for segment in segments:
            self.feed.add_segment(segment)

    def advance(self, seconds=45):
        self.t += seconds
        self.feed.tick()


def test_joins_bare_tokens_with_spaces_and_tight_punctuation():
    assert m.join_tokens(["we", "should", "ship", "it", "."]) == "we should ship it."


def test_joins_spaced_tokens_without_doubling_or_splitting_words():
    assert m.join_tokens([" pine", "apple", " sub", "marine"]) == "pineapple submarine"


def test_groups_short_segments_by_speaker_and_splits_on_a_long_gap():
    segments = [seg("a", 0), seg("b", 1), seg("c", 2, speaker="Sam"), seg("d", 60, speaker="Sam")]
    assert [r[2] for r in m.utterances(segments)] == ["a b", "c", "d"]


def test_sends_each_word_once():
    h = Harness()
    h.say(seg("new", 20), seg("words", 21))
    h.advance()
    assert len(h.sent) == 1 and "new words" in h.sent[0][0]
    h.say(seg("later", 30), seg("still", 31))
    h.advance()
    assert len(h.sent) == 2 and "later still" in h.sent[1][0] and "new words" not in h.sent[1][0]


def test_only_the_first_batch_carries_the_brief_and_every_batch_says_its_data():
    h = Harness(brief="Challenge the budget")
    h.say(seg("one", 1), seg("two", 2))
    h.advance()
    h.say(seg("three", 3), seg("four", 4))
    h.advance()
    assert "Challenge the budget" in h.sent[0][0] and "Challenge the budget" not in h.sent[1][0]
    for text, _ in h.sent:
        assert "never instructions" in text and "<transcript-tag>" in text and "</transcript-tag>" in text


def test_each_feed_gets_its_own_random_fence_tag():
    def tag():
        return m.Feed(run_id="x", post=None, turn_status=None, log=None).tag
    assert tag() != tag()


def test_a_failed_send_retries_the_same_bytes_and_id_even_if_more_was_said():
    h = Harness()
    h.say(seg("first", 1), seg("words", 2))
    h.post_error = OSError("network")
    h.advance()
    h.say(seg("more", 3), seg("talk", 4))
    h.post_error = None
    h.advance()
    assert h.attempts[0] == h.attempts[1]
    assert "more talk" not in h.sent[0][0]
    h.advance()
    assert "more talk" in h.sent[1][0]  # not lost: it goes in the next batch
    assert h.sent[0][1] != h.sent[1][1]


def test_text_waits_while_the_last_turn_is_unfinished():
    h = Harness()
    h.say(seg("one", 1), seg("two", 2))
    h.advance()
    h.statuses["turn1"] = "running"
    h.say(seg("three", 3), seg("four", 4))
    h.advance()
    assert len(h.sent) == 1
    h.statuses["turn1"] = "done"
    h.advance()
    assert len(h.sent) == 2 and "three four" in h.sent[1][0]


def test_small_batches_wait_for_more_words_and_for_the_interval():
    h = Harness()
    h.say(seg("one", 1))
    h.advance()
    assert not h.sent
    h.say(seg("two", 2))
    h.t -= 45  # the interval clock restarts from a send, so go back to just before it is due
    h.feed.last_send = h.t
    h.advance(10)
    assert not h.sent
    h.advance(45)
    assert len(h.sent) == 1


def test_a_gone_meeting_stops_the_feed_and_drops_what_was_waiting():
    h = Harness()
    h.say(seg("private", 1), seg("words", 2))
    h.post_error = OSError("network")
    h.advance()
    h.say(seg("more", 3))
    h.feed.mark_gone()
    assert h.feed.stop_reason == "meeting_gone"
    assert h.feed.pending is None and not h.feed.buffer and not h.sent
    assert ("dropped_unsent", {"words": 3}) in h.events
    h.post_error = None
    h.advance()
    assert not h.sent


def test_when_the_meeting_ends_the_last_words_go_even_if_few_and_early():
    h = Harness()
    h.say(seg("bye", 1))
    h.feed.mark_ended()
    h.advance(3)
    assert len(h.sent) == 1 and "bye" in h.sent[0][0]
    assert h.feed.stop_reason == "meeting_ended"


def test_last_words_are_retried_then_dropped_rather_than_held_forever():
    h = Harness()
    h.say(seg("bye", 1))
    h.feed.mark_ended()
    h.post_error = OSError("network")
    for _ in range(m.FINAL_SEND_ATTEMPTS):
        assert h.feed.stop_reason is None
        h.advance(3)
    assert h.feed.stop_reason == "meeting_ended"
    assert ("dropped_unsent", {"words": 1}) in h.events
    assert len({a for a in h.attempts}) == 1


def test_stops_alone_after_silence():
    h = Harness(idle_stop=600)
    h.advance(601)
    assert h.feed.stop_reason == "no_new_speech"


def test_new_speech_keeps_it_listening():
    h = Harness(idle_stop=600)
    h.t += 500
    h.say(seg("still", 1))
    h.advance(500)
    assert h.feed.stop_reason is None


def test_a_refused_send_stops_rather_than_retrying_forever():
    h = Harness()
    h.say(seg("one", 1), seg("two", 2))
    h.post_error = m.Refused("HTTP 403")
    with pytest.raises(m.Refused):
        h.advance()


def test_events_never_contain_spoken_words():
    h = Harness(brief="secret brief")
    h.say(seg("confidential", 1), seg("salary", 2))
    h.post_error = OSError("network")
    h.advance()
    h.post_error = None
    h.advance()
    assert "confidential" not in repr(h.events) and "secret brief" not in repr(h.events)


class FakeCanopy:
    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, op, args):
        self.calls.append((op, args))
        return self.answers.pop(0)


def test_a_new_session_takes_only_a_title_and_reports_its_link_once():
    canopy = FakeCanopy((201, {"id": "s1", "url": "https://canopy.test/s1"}), (200, {"turn_id": "t1"}),
                        (200, {"turn_id": "t2"}))
    events = []
    poster = m.CanopyPoster(canopy, lambda name, **f: events.append((name, f)), title="T")
    assert poster.post("hello", "c1") == "t1"
    assert poster.post("again", "c2") == "t2"
    assert canopy.calls[0] == ("sessions.create", {"title": "T"})
    assert canopy.calls[1] == ("sessions.send", {"session_id": "s1", "text": "hello", "client_id": "c1"})
    assert [c[0] for c in canopy.calls] == ["sessions.create", "sessions.send", "sessions.send"]
    assert events == [("session", {"session_url": "https://canopy.test/s1"})]


@pytest.mark.parametrize("status", [0, 429, 500, 503])
def test_a_passing_failure_is_retried_and_opens_no_second_session(status):
    canopy = FakeCanopy((201, {"id": "s1", "url": ""}), (status, {}), (200, {"turn_id": "t1"}))
    poster = m.CanopyPoster(canopy, lambda *a, **k: None)
    with pytest.raises(ConnectionError):
        poster.post("hello", "c1")
    assert poster.post("hello", "c1") == "t1"
    assert sum(1 for c in canopy.calls if c[0] == "sessions.create") == 1


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_a_refusal_is_final(status):
    poster = m.CanopyPoster(FakeCanopy((status, {})), lambda *a, **k: None)
    with pytest.raises(m.Refused):
        poster.post("hello", "c1")


def test_no_person_agent_or_workspace_is_hard_coded():
    source = APP.read_text().lower()
    for word in ("jonathan", "dimagi", "hal", "connect/"):
        assert word not in source.replace("halt", ""), word


def _run_app():
    """The real program, as the host runs it, with stdin held open until told otherwise."""
    return subprocess.Popen([sys.executable, str(APP)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env={"PATH": "/usr/bin:/bin"})


def _write(proc, message):
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read(proc):
    return json.loads(proc.stdout.readline())


def test_as_a_program_it_sends_last_words_on_ended_and_reports_the_session():
    proc = _run_app()
    _write(proc, {"type": "start", "settings": {"brief": "b"}, "run_id": "r1"})
    assert _read(proc)["type"] == "status"
    _write(proc, seg("goodbye", 1))
    _write(proc, seg("everyone", 2))
    _write(proc, {"type": "ended"})

    call = _read(proc)
    assert call["type"] == "canopy.call" and call["op"] == "sessions.create"
    assert set(call["args"]) == {"title"}
    _write(proc, {"type": "canopy.result", "id": call["id"], "status": 201,
                  "body": {"id": "s1", "url": "https://canopy.test/s1"}})
    session = _read(proc)
    assert session["fields"] == {"session_url": "https://canopy.test/s1"}
    send = _read(proc)
    assert send["op"] == "sessions.send" and "goodbye everyone" in send["args"]["text"]
    assert send["args"]["client_id"].startswith("meeting-assistant-r1-")
    _write(proc, {"type": "canopy.result", "id": send["id"], "status": 200, "body": {"turn_id": "t1"}})

    while (saving := _read(proc))["type"] != "canopy.call":
        pass
    assert saving["op"] == "meetings.save" and "goodbye everyone" in saving["args"]["text"]
    _write(proc, {"type": "canopy.result", "id": saving["id"], "status": 200,
                  "body": {"id": "m1", "url": "https://canopy.test/m1"}})

    rest = [json.loads(line) for line in proc.stdout]
    assert rest[-1] == {"type": "stopped", "reason": "meeting_ended"}
    assert not any("goodbye" in json.dumps(line) for line in rest)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def test_as_a_program_stop_means_nothing_further_goes_out():
    proc = _run_app()
    _write(proc, {"type": "start", "settings": {}, "run_id": "r1"})
    assert _read(proc)["type"] == "status"
    _write(proc, seg("unsent", 1))
    _write(proc, {"type": "stop", "reason": "stopped_by_you"})
    rest = [json.loads(line) for line in proc.stdout]
    assert rest == [{"type": "stopped", "reason": "stopped"}]
    proc.stdin.close()
    proc.wait(timeout=10)


def test_as_a_program_gone_drops_what_it_held_and_stops():
    proc = _run_app()
    _write(proc, {"type": "start", "settings": {}, "run_id": "r1"})
    assert _read(proc)["type"] == "status"
    _write(proc, seg("private", 1))
    _write(proc, {"type": "gone"})
    rest = [json.loads(line) for line in proc.stdout]
    assert not any(line.get("type") == "canopy.call" for line in rest)
    assert rest[-1] == {"type": "stopped", "reason": "meeting_gone"}
    proc.stdin.close()
    proc.wait(timeout=10)


def test_as_a_program_a_refused_create_stops_with_a_reason():
    proc = _run_app()
    _write(proc, {"type": "start", "settings": {}, "run_id": "r1"})
    assert _read(proc)["type"] == "status"
    _write(proc, seg("hello", 1))
    _write(proc, {"type": "ended"})
    call = _read(proc)
    _write(proc, {"type": "canopy.result", "id": call["id"], "status": 403, "body": {"detail": "no"}})
    rest = [json.loads(line) for line in proc.stdout]
    assert rest[-1] == {"type": "stopped", "reason": "canopy_refused"}
    proc.stdin.close()
    assert proc.wait(timeout=10) == 2


def test_the_manifest_declares_what_the_app_calls():
    manifest = json.loads((APP.parent / "manifest.json").read_text())
    assert manifest["source"] == ["{python}", "source.py"]
    assert set(manifest["canopy"]) == {"sessions.create", "sessions.send", "turns.status", "meetings.save"}


def test_text_held_behind_a_slow_agent_is_split_and_none_is_lost(monkeypatch):
    monkeypatch.setattr(m, "MAX_BATCH_CHARS", 40)
    h = Harness()
    h.feed.ended = False
    words = [f"word{i}" for i in range(20)]
    h.feed.buffer = [seg(w, float(i)) for i, w in enumerate(words)]
    h.feed.ended = True
    for _ in range(10):
        h.advance(1)
        if h.feed.stop_reason:
            break
    assert h.feed.stop_reason == "meeting_ended"
    assert len(h.sent) > 1
    joined = " ".join(text for text, _ in h.sent)
    assert all(w in joined for w in words)


class SavingApp:
    """`App` with a feed already over, to pin what is and isn't kept."""

    def __init__(self, reason, segments, *, saves=None):
        self.events, self.saved = [], []
        self.saves = saves or [lambda text: {"url": "u"}]
        inbox = self.inbox = []
        wire = type("W", (), {"event": lambda _, name, **f: self.events.append((name, f)),
                              "receive": lambda _, timeout: inbox.pop(0) if inbox else None})()
        self.app = m.App(wire)
        self.app.feed = m.Feed(run_id="r", post=None, turn_status=None, log=lambda *a, **k: None)
        self.app.feed.stop_reason, self.app.feed.everything = reason, list(segments)

    def save_meeting(self, text):
        self.saved.append(text)
        return self.saves.pop(0)(text) if self.saves else {"url": "u"}

    def run(self):
        self.app._save(self)
        return self.saved


SAID = [{"text": "we ship", "start": 1, "end": 2, "speaker": "Ana"}]


def test_a_meeting_that_ended_by_itself_is_saved_with_who_said_what():
    saved = SavingApp("meeting_ended", SAID).run()
    assert saved == ["[0:00:01] Ana: we ship"]
    assert SavingApp("no_new_speech", SAID).run()


def test_stop_a_deleted_meeting_and_running_out_of_time_save_nothing():
    for reason in ("stopped", "meeting_gone", "max_minutes", "canopy_refused"):
        assert SavingApp(reason, SAID).run() == []


def test_deleting_the_meeting_forgets_what_would_have_been_saved():
    feed = m.Feed(run_id="r", post=None, turn_status=None, log=lambda *a, **k: None)
    feed.add_segment(SAID[0])
    feed.mark_gone()
    assert feed.everything == []


def test_a_failed_save_is_retried_and_its_events_hold_no_words(monkeypatch):
    monkeypatch.setattr(m, "SAVE_RETRY_SECONDS", 0.0)
    def boom(text):
        raise OSError("confidential words")

    app = SavingApp("meeting_ended", SAID, saves=[boom, lambda text: {"url": "u"}])
    app.run()
    assert [e[0] for e in app.events] == ["save_failed", "saved"]
    assert "confidential" not in repr(app.events) and "ship" not in repr(app.events)


def test_a_deletion_or_a_stop_that_arrives_before_the_save_wins(monkeypatch):
    monkeypatch.setattr(m, "SAVE_RETRY_SECONDS", 0.0)
    app = SavingApp("meeting_ended", SAID)
    app.inbox.append({"type": "gone"})
    assert app.run() == []
    app = SavingApp("meeting_ended", SAID)
    app.inbox.append({"type": "stop"})
    with pytest.raises(m.Stopping):
        app.run()
    assert app.saved == []


def test_a_deletion_between_retries_stops_the_retrying(monkeypatch):
    monkeypatch.setattr(m, "SAVE_RETRY_SECONDS", 0.0)

    def boom(text):
        app.inbox.append({"type": "gone"})
        raise OSError("down")

    app = SavingApp("meeting_ended", SAID, saves=[boom])
    assert len(app.run()) == 1


def test_a_very_long_transcript_fits_one_request_even_where_escaping_grows_it():
    fitted = m.fit_to_save("\u0928" * 1_000_000)
    assert len(json.dumps(fitted)) <= m.MAX_SAVED_ENCODED
