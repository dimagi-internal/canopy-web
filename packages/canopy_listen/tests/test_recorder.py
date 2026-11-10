"""A whole recording with a fake source and recogniser: what lands on disk must read like a
Superwhisper meeting, live and once ended, and a failure must say why without words."""
import json
import queue
import threading

import numpy as np
import pytest
from canopy_listen import cli, store
from canopy_listen.live import SAMPLE_RATE, Sentence
from canopy_listen.recorder import record


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_LISTEN_HOME", str(tmp_path))


class FakeSource:
    name = "fake"

    def __init__(self, seconds):
        self.chunks, self.done = queue.Queue(), threading.Event()
        self.seconds = seconds

    def start(self):
        step = SAMPLE_RATE // 10
        for _ in range(int(self.seconds * 10)):
            self.chunks.put(np.full(step, 0.1, dtype=np.float32))
        self.done.set()

    def stop(self):
        pass


def recognise(buffer):
    n = len(buffer) / SAMPLE_RATE
    return [Sentence("the budget is due friday", 0.0, min(2.0, n))] if n > 0.5 else []


def test_a_recording_reads_like_a_superwhisper_meeting(capsys):
    meeting = store.new_meeting("standup", "fake")
    assert record(meeting["id"], FakeSource(4.0), recognise=recognise, keep_audio=False) == 0
    assert cli.main(["meetings", "--limit", "1", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == meeting["id"] and rows[0]["state"] == "ended"
    assert "segments" not in rows[0]  # a listing carries no words

    assert cli.main(["meetings", "read", meeting["id"], "--transcript", "--json"]) == 0
    read = json.loads(capsys.readouterr().out)
    assert read["segments"][0]["text"] == "the budget is due friday"
    assert set(read["segments"][0]) >= {"text", "start", "end", "speaker", "words"}
    assert read["speakers"] == [{"name": "Speaker 1", "number": 0}]
    assert read["summary"] and "Speaker 1: the budget" in read["transcript"]
    assert store.active() is None


def test_it_listens_before_the_models_load_and_loses_nothing(monkeypatch):
    """Loading the models can take seconds on a cold disk: audio that arrives meanwhile
    waits in the source's queue, and the meeting already says it is recording."""
    from canopy_listen import config, engines

    meeting = store.new_meeting()
    seen = {}

    class SlowRecogniser:
        timed = True

        def __init__(self, *a):
            seen["state"] = store.load(meeting["id"])["state"]
            seen["queued"] = source.chunks.qsize()

        def __call__(self, buffer):
            return recognise(buffer)

    source = FakeSource(3.0)
    monkeypatch.setattr(config, "load", lambda: {"asr": "x", "speakers": "none", "final": "none"})
    monkeypatch.setattr(engines, "Recogniser", SlowRecogniser)
    monkeypatch.setattr(engines, "make_labeller", lambda *a: None)
    assert record(meeting["id"], source, keep_audio=False) == 0
    assert seen == {"state": "recording", "queued": 30}
    saved = store.load(meeting["id"])
    assert [s["text"] for s in saved["segments"]] == ["the budget is due friday"]
    assert saved["partial"] == ""


def test_a_live_meeting_has_no_transcript_yet(capsys):
    meeting = store.new_meeting()
    meeting["state"] = "recording"
    meeting["segments"] = [{"text": "hi", "start": 0, "end": 1, "speaker": 0, "words": []}]
    store.save(meeting)
    cli.main(["meetings", "read", meeting["id"], "--transcript", "--json"])
    read = json.loads(capsys.readouterr().out)
    assert read["summary"] == "" and read["transcript"] == "" and read["segments"]


def test_a_failure_names_the_error_and_not_the_words():
    meeting = store.new_meeting()

    def broken(buffer):
        raise RuntimeError("the budget is due friday")

    assert record(meeting["id"], FakeSource(3.0), recognise=broken, keep_audio=False) == 1
    saved = store.load(meeting["id"])
    assert saved["state"] == "failed" and saved["error"] == "RuntimeError"
    assert "budget" not in json.dumps(saved)


def test_meeting_ids_cannot_climb_out_of_the_store():
    with pytest.raises(ValueError):
        store.meeting_dir("../etc")
