"""The source decides which conversation gets read at all, so these pin what it refuses,
when it says a meeting is over, and that each word is emitted exactly once."""
import datetime as dt
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "source.py"


def _load():
    spec = importlib.util.spec_from_file_location("superwhisper_source", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()
NOW = dt.datetime(2026, 11, 3, 21, 40, tzinfo=dt.timezone.utc)


def seg(text, start, speaker=0):
    return {"text": text, "start": start, "end": start + 0.5, "speaker": speaker, "words": []}


def meeting(*segments, **extra):
    return {"segments": list(segments), "speakers": [{"name": "Priya", "number": 0}],
            "summary": "", "transcript": "", **extra}


class Harness:
    def __init__(self, start_after=0.0):
        self.state = meeting()
        self.items = []
        self.follower = m.Follower(lambda: self.state, self.items.append, start_after)

    def texts(self):
        return [i["text"] for i in self.items if i["type"] == "segment"]

    def kinds(self):
        return [i["type"] for i in self.items]


def test_speaker_index_missing_from_the_list_still_gets_the_number_the_app_shows():
    speakers = [{"name": "Priya", "number": 0}]
    assert m.speaker_label(0, speakers) == "Priya"
    assert m.speaker_label(2, speakers) == "Speaker 3"
    assert m.speaker_label(None, speakers) == "Unknown speaker"


def test_starts_after_what_was_already_said_and_emits_each_segment_once():
    h = Harness(start_after=10.0)
    h.state = meeting(seg("before", 5), seg("new", 20), seg("words", 21))
    h.follower.tick()
    assert h.texts() == ["new", "words"]
    h.state["segments"].extend([seg("later", 30)])
    h.follower.tick()
    h.follower.tick()
    assert h.texts() == ["new", "words", "later"]


def test_emits_raw_segments_and_leaves_joining_to_the_app():
    h = Harness()
    h.state = meeting(seg(" pine", 1), seg("apple", 2))
    h.follower.tick()
    assert h.texts() == [" pine", "apple"]
    assert h.items[0] == {"type": "segment", "start": 1, "end": 1.5, "speaker": "Priya", "text": " pine"}


def test_a_rename_reaches_only_later_segments():
    h = Harness()
    h.state = meeting(seg("one", 1))
    h.follower.tick()
    h.state = meeting(seg("one", 1), seg("two", 2))
    h.state["speakers"] = [{"name": "Sam", "number": 0}]
    h.follower.tick()
    assert [i["speaker"] for i in h.items] == ["Priya", "Sam"]


def test_a_deleted_meeting_is_gone_after_three_failed_reads():
    h = Harness()
    h.state = None
    h.follower.tick()
    h.follower.tick()
    assert h.kinds() == [] and not h.follower.finished
    h.follower.tick()
    assert h.kinds() == ["gone"] and h.follower.finished


def test_one_failed_read_is_not_a_deleted_meeting():
    h = Harness()
    h.state = None
    h.follower.tick()
    h.state = meeting()
    h.follower.tick()
    assert h.follower.read_failures == 0 and not h.follower.finished


@pytest.mark.parametrize("extra", [{"summary": "s"}, {"transcript": "t"}])
def test_a_summary_or_transcript_means_ended_after_the_last_segments(extra):
    h = Harness()
    h.state = meeting(seg("bye", 1), **extra)
    h.follower.tick()
    assert h.kinds() == ["segment", "ended"] and h.follower.finished


def test_refuses_a_stale_or_missing_meeting_and_accepts_a_recent_one():
    recent = [{"id": "new", "datetime": "2026-11-03 21:33:06.000"}]
    assert m.pick_meeting(recent, now=NOW, max_age_minutes=15) == "new"
    with pytest.raises(m.Refused, match="stale_meeting"):
        m.pick_meeting(recent, now=NOW, max_age_minutes=5)
    with pytest.raises(m.Refused, match="no_meeting"):
        m.pick_meeting([], now=NOW)
    with pytest.raises(m.Refused, match="no_meeting"):
        m.pick_meeting(None, now=NOW)


def _attach(first, settings=None):
    recent = [{"id": "new", "datetime": "2026-11-03 21:33:06.000"}]
    return m.attach(settings or {}, list_meetings=lambda: recent, read_meeting=lambda _id: first, now=NOW)


def test_attach_refuses_a_finished_or_unreadable_meeting():
    with pytest.raises(m.Refused, match="meeting_already_ended"):
        _attach(meeting(seg("old", 1), summary="s"))
    with pytest.raises(m.Refused, match="unreadable_meeting"):
        _attach(None)


def test_attach_starts_after_what_was_said_unless_asked_for_the_start():
    first = meeting(seg("earlier", 8))
    assert _attach(first)[1] == 8.5
    assert _attach(first, {"from_start": True})[1] == 0.0


def _fake_cli(tmp_path, listing, reading):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "superwhisper"
    path.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({json.dumps(json.dumps(reading))} if 'read' in sys.argv else {json.dumps(json.dumps(listing))})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _run_source(tmp_path, bin_dir, lines):
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, str(SOURCE)], input="".join(json.dumps(x) + "\n" for x in lines),
                          capture_output=True, text=True, env=env, timeout=30)


def test_as_a_program_it_refuses_with_a_code_and_prints_only_protocol(tmp_path):
    bin_dir = _fake_cli(tmp_path, [], {})
    done = _run_source(tmp_path, bin_dir, [{"type": "start", "settings": {}, "run_id": "r"}])
    assert done.returncode == 0
    # Stdin closing (the host gone) ends the wait at once, with a code and no words.
    assert [json.loads(x) for x in done.stdout.splitlines()] == [
        {"type": "event", "name": "waiting_for_your_meeting", "fields": {}},
        {"type": "stopped", "reason": "stopped_while_waiting"}]


class Meetings:
    """Superwhisper's newest meeting as seen over successive polls."""

    def __init__(self, *listings):
        self.listings = list(listings)

    def __call__(self):
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]


LIVE = [{"id": "theirs", "datetime": NOW.strftime("%Y-%m-%d %H:%M:%S")}]
OLD = [{"id": "old", "datetime": "2026-11-01 10:00:00"}]


def _wait(listing, *, wait=30, stop_after=None):
    steps, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        return stop_after is not None and len(sleeps) >= stop_after

    got = m.attach({}, list_meetings=listing, read_meeting=lambda _id: meeting(seg("hi", 1)), now=NOW,
                   wait=wait, sleep=sleep, clock=lambda: NOW, step=steps.append)
    return got, steps


def test_a_meeting_already_recording_is_joined_at_once():
    _, steps = _wait(Meetings(LIVE))
    assert steps == ["joined_your_meeting"]


def test_with_no_meeting_yet_it_waits_for_the_person_and_joins_from_the_first_word():
    (read, start_after), steps = _wait(Meetings(OLD, OLD, LIVE))
    assert steps == ["waiting_for_your_meeting", "joined_your_meeting"]
    assert start_after == 0.0 and read()["segments"][0]["text"] == "hi"


def test_waiting_ends_with_a_reason_when_nobody_starts_a_meeting_or_stop_is_pressed():
    with pytest.raises(m.Refused, match="no_meeting_started"):
        _wait(Meetings(OLD), wait=9)
    with pytest.raises(m.Refused, match="stopped_while_waiting"):
        _wait(Meetings(OLD), stop_after=2)


def test_it_never_tells_superwhisper_to_do_anything():
    """Its tool can't start a meeting; what it does start is a dictation that types into the front window."""
    import re
    text = SOURCE.read_text()
    commands = set(re.findall(r'run_cli\("(\w+)"', text)) | set(re.findall(r'cli_path\(\), "(\w+)"', text))
    assert commands == {"meetings"}


def test_without_a_wait_it_still_refuses_a_missing_meeting():
    with pytest.raises(m.Refused, match="no_meeting"):
        m.attach({}, list_meetings=lambda: [], read_meeting=lambda _id: None, now=NOW)
