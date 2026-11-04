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
    assert [json.loads(x) for x in done.stdout.splitlines()] == [{"type": "stopped", "reason": "no_meeting"}]


def test_as_a_program_it_streams_a_finished_read_as_segments_and_ended(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # The first read must look live, so a counter file makes the later ones finished.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "reads"
    path = bin_dir / "superwhisper"
    live = meeting(seg("hello", 1))
    done_reading = meeting(seg("hello", 1), seg("bye", 2), summary="s")
    path.write_text(
        f"#!{sys.executable}\nimport json, sys, pathlib\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"listing = {json.dumps([{'id': 'abc', 'datetime': now}])!r}\n"
        f"live = {json.dumps(live)!r}\n"
        f"over = {json.dumps(done_reading)!r}\n"
        # Call 0 is the listing and call 1 the attaching read.
        "print((live if n <= 1 else over) if 'read' in sys.argv else listing)\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # Stdin stays open: closing it would read as the host going away.
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(SOURCE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)
    proc.stdin.write(json.dumps({"type": "start", "settings": {"from_start": True}, "run_id": "r"}) + "\n")
    proc.stdin.flush()
    out = [json.loads(line) for line in proc.stdout]
    proc.stdin.close()
    proc.wait(timeout=10)
    assert [i["type"] for i in out] == ["segment", "segment", "ended", "stopped"]
    assert [i.get("text") for i in out[:2]] == ["hello", "bye"]


def test_the_check_asks_for_help_only_and_reports_a_missing_tool(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "argv"
    path = bin_dir / "superwhisper"
    path.write_text(f"#!{sys.executable}\nimport sys, pathlib\npathlib.Path({str(seen)!r}).write_text(' '.join(sys.argv[1:]))\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    assert subprocess.run([sys.executable, str(SOURCE), "--check"], env=env, timeout=30).returncode == 0
    assert seen.read_text() == "meetings --help"
    os.remove(path)
    assert subprocess.run([sys.executable, str(SOURCE), "--check"], env=env, timeout=30).returncode == 1


def test_the_manifest_says_it_provides_a_transcript_and_makes_no_canopy_calls():
    manifest = json.loads((SOURCE.parent / "manifest.json").read_text())
    assert manifest["provides"] == ["transcript"] and manifest["canopy"] == []
