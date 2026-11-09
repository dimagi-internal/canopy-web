#!/usr/bin/env python3
"""A transcript source: follow one live meeting and emit its segments.

Two recorders, chosen by the `recorder` setting, and everything specific to them lives
here, so the meeting app doesn't know or care where its words come from:

  * canopy-listen (the default) records and transcribes on this Mac with open models.
    Start starts it, and this always stops it on the way out, so the microphone is never
    left on. `--end` ends its meeting, which the run then saves.
  * superwhisper, the rules below.

  * ONE MEETING, AND A LIVE ONE. A Superwhisper meeting runs until someone ends it, so an
    old one may still be open, and attaching to it would pass off a past conversation as
    live. The newest meeting is pinned at the start, and a stale or finished one is refused.
  * DELETING MEANS GONE. If the meeting can't be read three times running, the person may
    have just decided it should not exist, and `gone` tells the app to drop what it holds.
  * NOTHING IS PRINTED BUT THE PROTOCOL. The CLI's output holds the words, so it is never
    surfaced, and neither is an error that might quote it.
  * START WAITS FOR THE PERSON. Superwhisper's command-line tool can't start a meeting (it
    reports switching to Meeting mode and doesn't, so a recording it starts is a dictation,
    which types what it heard into the front window). So this never records: if no meeting
    is recording yet, it waits for the person to start one in Superwhisper, saying so.
  * EVERY STEP IS NAMED. Events carry a step name and nothing else, so the menu bar can say
    what is happening without any words from the meeting passing through it.

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
#: How long Start waits for the person to start a meeting in Superwhisper.
WAIT_FOR_MEETING_SECONDS = 540.0  # under the app's 10-minute silence stop, so the reason shown is this one
CONTROL_TIMEOUT_SECONDS = 5
#: The other recorder: canopy-listen, which records and transcribes on this Mac.
LISTEN = "canopy-listen"
LISTEN_START_SECONDS = 240  # its first start downloads the models

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


def tool_is_set_up() -> bool:
    """Asks for help text only, so the check reads no meeting and starts nothing."""
    try:
        return subprocess.run([cli_path(), "meetings", "--help"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=CLI_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def listen_path() -> str:
    return shutil.which(LISTEN) or str(Path.home() / ".local/bin" / LISTEN)


def run_listen(*args: str, timeout: float = CLI_TIMEOUT_SECONDS):
    """Parsed JSON from canopy-listen, or None. Its output holds words too, so it is never shown."""
    try:
        res = subprocess.run([listen_path(), *args, "--json"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
        return json.loads(res.stdout) if res.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def listen_is_set_up() -> bool:
    try:
        return subprocess.run([listen_path(), "status", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=CLI_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_listening(*, step=lambda name: None, start=None) -> tuple:
    """canopy-listen records on this Mac, so Start starts it: ((read, cursor), meeting id), or Refused."""
    step("starting_recording")
    got = (start or (lambda: run_listen("start", "--title", "Meeting", timeout=LISTEN_START_SECONDS)))()
    if not isinstance(got, dict) or got.get("state") != "recording" or not got.get("id"):
        raise Refused("recording_did_not_start")
    meeting_id = str(got["id"])
    step("recording")
    return (lambda: run_listen("meetings", "read", meeting_id, "--transcript"), 0.0), meeting_id


def recorder_of(settings: dict) -> str:
    return str(settings.get("recorder") or LISTEN).strip().lower()


def app_is_open() -> bool:
    """Recording needs the app itself running, not just its command-line tool."""
    try:
        return subprocess.run([shutil.which("pgrep") or "/usr/bin/pgrep", "-x", "superwhisper"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=CONTROL_TIMEOUT_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def attach(settings: dict, *, list_meetings, read_meeting, now: dt.datetime, wait: float = 0.0,
           sleep=time.sleep, clock=None, step=lambda name: None) -> tuple:
    """(read function, cursor to start after), or Refused.

    A meeting recording now is joined. Otherwise, given `wait`, one the person starts within
    that time is joined from its first word. `sleep` returning true means stop waiting."""
    try:
        joined = _attach_to(list_meetings(), settings, read_meeting, now)
    except Refused:
        if wait <= 0:
            raise
    else:
        step("joined_your_meeting")
        return joined
    step("waiting_for_your_meeting")
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    waited = 0.0
    while waited < wait:
        if sleep(POLL_SECONDS):
            raise Refused("stopped_while_waiting")
        waited += POLL_SECONDS
        try:
            joined = _attach_to(list_meetings(), {"from_start": True}, read_meeting, clock())
        except Refused:
            continue
        step("joined_your_meeting")
        return joined
    raise Refused("no_meeting_started")


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


def watch_for_stop(stop: threading.Event, end: threading.Event | None = None) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "end" and end is not None:
            end.set()  # the person ended the meeting: finish it, and keep what was said
        if isinstance(message, dict) and message.get("type") == "stop":
            break
    # Stdin closing means the host is gone, and nobody is left to tell us to stop.
    stop.set()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Checks get no settings, so each passes if either recorder is ready.
    if argv == ["--check"]:
        return 0 if listen_is_set_up() or tool_is_set_up() else 1
    if argv == ["--check-open"]:
        return 0 if listen_is_set_up() or app_is_open() else 1
    if argv == ["--end"]:  # end the meeting canopy-listen is recording; the run then saves it
        return 0 if run_listen("stop", timeout=90) is not None else 1
    settings = wait_for_start()
    stop, end = threading.Event(), threading.Event()
    threading.Thread(target=watch_for_stop, args=(stop, end), daemon=True).start()
    step = lambda name: emit_line({"type": "event", "name": name, "fields": {}})  # noqa: E731
    recorder = recorder_of(settings)
    follower, recording = None, None
    try:
        if recorder == LISTEN:
            (read, start_after), recording = start_listening(step=step)
        elif recorder == "superwhisper":
            read, start_after = attach(
                settings,
                list_meetings=lambda: run_cli("meetings", "--limit", "1"),
                read_meeting=lambda meeting_id: run_cli("meetings", "read", meeting_id, "--transcript"),
                now=dt.datetime.now(dt.timezone.utc), wait=WAIT_FOR_MEETING_SECONDS,
                sleep=lambda seconds: stop.wait(seconds), step=step)
        else:
            raise Refused("unknown_recorder")
        follower = Follower(read, emit_line, start_after)
        ending = False
        while not follower.finished and not stop.is_set():
            if end.is_set() and recording and not ending:
                ending = True
                step("ending")
                run_listen("stop", timeout=90)  # its last words and its end arrive on the next read
            follower.tick()
            if not follower.finished:
                (end if recording and not ending else stop).wait(POLL_SECONDS)
    except Refused as refusal:
        emit_line({"type": "stopped", "reason": str(refusal)})
        return 0
    except BrokenPipeError:
        return 0
    finally:
        # Never leave the microphone on: a recording this started ends when this does.
        if recording and not (follower and follower.ended):
            run_listen("stop", timeout=90)
    emit_line({"type": "stopped", "reason": "finished" if follower.finished else "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
