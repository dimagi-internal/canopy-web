#!/usr/bin/env python3
"""A transcript source: follow one live Superwhisper meeting and emit its segments.

Everything specific to Superwhisper lives here, so a meeting app doesn't know or care
where its words come from.

  * ONE MEETING, AND A LIVE ONE. A Superwhisper meeting runs until someone ends it, so an
    old one may still be open, and attaching to it would pass off a past conversation as
    live. The newest meeting is pinned at the start, and a stale or finished one is refused.
  * DELETING MEANS GONE. If the meeting can't be read three times running, the person may
    have just decided it should not exist, and `gone` tells the app to drop what it holds.
  * NOTHING IS PRINTED BUT THE PROTOCOL. The CLI's output holds the words, so it is never
    surfaced, and neither is an error that might quote it.
  * START MEANS RECORD. If no meeting is recording, this starts one: Superwhisper's Meeting
    mode, then record. What it started it also ends, and the mode the person had goes back,
    so their next dictation isn't a meeting. A meeting they started themselves is theirs, and
    is left recording.

The text is in `segments`: on a live meeting `transcript` stays empty until the end. There
is no streaming API or webhook, so this polls. Speaker names are re-read on every poll, so
a rename in the app reaches later segments.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

POLL_SECONDS = 3.0
MAX_AGE_MINUTES = 15.0
READ_FAILURES_MEANS_GONE = 3
CLI_TIMEOUT_SECONDS = 20
#: Superwhisper's built-in meeting mode, as `superwhisper switch` lists it.
MEETING_MODE = "system-meeting"
#: How long a recording it started has to show up as a meeting.
RECORDING_WAIT_SECONDS = 20.0
CONTROL_TIMEOUT_SECONDS = 5
APP_DOMAIN = "com.superduper.superwhisper"


class Refused(Exception):
    """Why this source can't attach. Its argument is a short reason code, never content."""


def cli_path() -> str:
    # Started from the menu bar, PATH is launchd's and usually lacks ~/.local/bin.
    return shutil.which("superwhisper") or str(Path.home() / ".local/bin/superwhisper")


def run_cli(*args: str):
    """Parsed JSON from the CLI, or None."""
    try:
        res = subprocess.run([cli_path(), *args, "--json"], capture_output=True, text=True,
                             timeout=CLI_TIMEOUT_SECONDS)
        return json.loads(res.stdout) if res.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_control(*args: str) -> bool:
    """A command to the running app (switch, start, stop). Its output is never read."""
    try:
        return subprocess.run([cli_path(), *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=CONTROL_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tool_is_set_up() -> bool:
    """Asks for help text only, so the check reads no meeting and starts nothing."""
    try:
        return subprocess.run([cli_path(), "meetings", "--help"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=CLI_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def app_is_open() -> bool:
    """Recording needs the app itself running, not just its command-line tool."""
    try:
        return subprocess.run(["/usr/bin/pgrep", "-x", "superwhisper"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=CONTROL_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def active_mode():
    """The mode the person has selected, so it can be put back. None if it can't be read."""
    try:
        res = subprocess.run(["/usr/bin/defaults", "read", APP_DOMAIN, "activeModeKey"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=CONTROL_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    mode = res.stdout.strip()
    return mode if res.returncode == 0 and mode and len(mode) < 100 else None


class Recorder:
    """Starts a meeting recording, and later undoes exactly what it did."""

    def __init__(self, control, read_mode):
        self.control, self.read_mode = control, read_mode
        self.previous_mode = None
        self.recording = False

    def start(self) -> bool:
        self.previous_mode = self.read_mode()
        if self.previous_mode != MEETING_MODE and not self.control("switch", MEETING_MODE):
            self.previous_mode = None
            return False
        self.recording = self.control("start")
        return self.recording

    def finish(self, *, still_recording: bool) -> None:
        """Ends the recording it started (unless the meeting already ended) and puts the mode back."""
        if self.recording and still_recording:
            self.control("stop")
        self.recording = False
        if self.previous_mode and self.previous_mode != MEETING_MODE:
            self.control("switch", self.previous_mode)
        self.previous_mode = None


def pick_meeting(listing, *, now: dt.datetime, max_age_minutes: float = MAX_AGE_MINUTES) -> str:
    """The newest meeting, if it started recently."""
    if not listing:
        raise Refused("no_meeting")
    newest = listing[0]
    try:
        started = dt.datetime.fromisoformat(str(newest["datetime"])).replace(tzinfo=dt.timezone.utc)
        meeting_id = newest["id"]
    except (KeyError, TypeError, ValueError):
        raise Refused("unreadable_meeting") from None
    if (now - started).total_seconds() / 60 > max_age_minutes:
        raise Refused("stale_meeting")
    return meeting_id


def speaker_label(index, speakers) -> str:
    """The app's current name for a speaker, or the 1-based number it shows for an unnamed
    one. A segment can carry an index the `speakers` list doesn't have yet."""
    if index is None:
        return "Unknown speaker"
    for speaker in speakers or []:
        if speaker.get("number") == index and speaker.get("name"):
            return speaker["name"]
    return f"Speaker {index + 1}"


def has_ended(meeting: dict) -> bool:
    return bool(meeting.get("summary") or meeting.get("transcript"))


def newest_end(meeting: dict) -> float:
    return max((s.get("end", 0) for s in meeting.get("segments") or []), default=0.0)


class Follower:
    """Turns repeated reads of one meeting into a stream where each segment appears once."""

    def __init__(self, read, emit, start_after: float = 0.0):
        self.read, self.emit = read, emit
        self.cursor = start_after
        self.read_failures = 0
        self.finished = False
        self.ended = False

    def tick(self) -> None:
        meeting = self.read()
        if meeting is None:
            self.read_failures += 1
            if self.read_failures >= READ_FAILURES_MEANS_GONE:
                self.emit({"type": "gone"})
                self.finished = True
            return
        self.read_failures = 0
        speakers = meeting.get("speakers") or []
        fresh = sorted((s for s in meeting.get("segments") or [] if s.get("end", 0) > self.cursor),
                       key=lambda s: s.get("start", 0))
        for segment in fresh:
            self.emit({"type": "segment", "start": segment.get("start", 0), "end": segment.get("end", 0),
                       "speaker": speaker_label(segment.get("speaker"), speakers),
                       "text": segment.get("text") or ""})
        if fresh:
            self.cursor = max(s.get("end", 0) for s in fresh)
        if has_ended(meeting):
            self.emit({"type": "ended"})
            self.finished = self.ended = True


def _attach_to(listing, settings: dict, read_meeting, now: dt.datetime) -> tuple:
    meeting_id = pick_meeting(listing, now=now)
    read = lambda: read_meeting(meeting_id)  # noqa: E731
    first = read()
    if first is None:
        raise Refused("unreadable_meeting")
    if has_ended(first):
        raise Refused("meeting_already_ended")
    return read, 0.0 if settings.get("from_start") else newest_end(first)


def _newest_id(listing):
    return listing[0].get("id") if listing and isinstance(listing[0], dict) else None


def attach(settings: dict, *, list_meetings, read_meeting, now: dt.datetime, recorder=None,
           wait: float = RECORDING_WAIT_SECONDS, sleep=time.sleep, clock=None) -> tuple:
    """(read function, cursor to start after), or Refused.

    A meeting already recording is joined. Otherwise, given a recorder, one is started and
    joined from its first word; without one (or if it never shows up) this refuses."""
    listing = list_meetings()
    try:
        return _attach_to(listing, settings, read_meeting, now)
    except Refused:
        if recorder is None:
            raise
    before = _newest_id(listing)
    if not recorder.start():
        recorder.finish(still_recording=True)
        raise Refused("could_not_start_recording")
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    waited = 0.0
    while waited < wait:
        if sleep(1.0):  # told to stop while waiting
            break
        waited += 1.0
        listing = list_meetings()
        if _newest_id(listing) in (None, before):
            continue
        try:
            return _attach_to(listing, {"from_start": True}, read_meeting, clock())
        except Refused:
            continue
    recorder.finish(still_recording=True)
    raise Refused("recording_not_found")


def emit_line(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def wait_for_start() -> dict:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "start":
            settings = message.get("settings")
            return settings if isinstance(settings, dict) else {}
        if isinstance(message, dict) and message.get("type") == "stop":
            break
    raise SystemExit(0)


def watch_for_stop(stop: threading.Event) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "stop":
            break
    # Stdin closing means the host is gone, and nobody is left to tell us to stop.
    stop.set()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--check"]:
        return 0 if tool_is_set_up() else 1
    if argv == ["--check-open"]:
        return 0 if app_is_open() else 1
    settings = wait_for_start()
    stop = threading.Event()
    threading.Thread(target=watch_for_stop, args=(stop,), daemon=True).start()
    recorder = Recorder(run_control, active_mode)
    follower = None
    try:
        read, start_after = attach(
            settings,
            list_meetings=lambda: run_cli("meetings", "--limit", "1"),
            read_meeting=lambda meeting_id: run_cli("meetings", "read", meeting_id, "--transcript"),
            now=dt.datetime.now(dt.timezone.utc), recorder=recorder,
            sleep=lambda seconds: stop.wait(seconds))
        follower = Follower(read, emit_line, start_after)
        while not follower.finished and not stop.is_set():
            follower.tick()
            if not follower.finished:
                stop.wait(POLL_SECONDS)
    except Refused as refusal:
        emit_line({"type": "stopped", "reason": str(refusal)})
        return 0
    except BrokenPipeError:
        return 0
    finally:
        # Stop, silence, the time cap or a crash: a recording this started doesn't outlive it.
        recorder.finish(still_recording=not (follower and follower.ended))
    emit_line({"type": "stopped", "reason": "finished" if follower.finished else "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
