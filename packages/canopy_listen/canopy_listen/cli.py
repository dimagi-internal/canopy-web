"""canopy-listen: record a meeting on this Mac and transcribe it here, live.

  canopy-listen start [--title T] [--file AUDIO [--speed N]] [--json]
  canopy-listen stop [--json]
  canopy-listen status [--json]
  canopy-listen meetings [--limit N] [--json]
  canopy-listen meetings read ID [--transcript] [--json]
  canopy-listen delete ID
  canopy-listen models          download both models now
  canopy-listen queue-file AUDIO [--speed N]   for testing: the next start plays AUDIO

`meetings` and `meetings read` print what Superwhisper's command-line tool prints, so
anything that follows a Superwhisper meeting can follow one of these.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time

from . import store
from .recorder import FileSource, MicSource, pid_of, record

START_TIMEOUT_SECONDS = 180  # the first start downloads the models


def _print(args, data, human: str) -> None:
    print(json.dumps(data) if getattr(args, "json", False) else human)


def cmd_start(args) -> int:
    current = store.active()
    if current and pid_of(current):
        _print(args, {"id": current, "state": "recording", "already": True}, f"Already recording {current}")
        return 0
    queued = store.home() / "next-input.json"
    if not args.file and queued.exists():
        # For testing a whole app end to end: the next start plays this file instead of the
        # microphone, once. `canopy-listen queue-file` writes it.
        try:
            spec = json.loads(queued.read_text())
            args.file, args.speed = spec["file"], float(spec.get("speed", 1.0))
        finally:
            queued.unlink(missing_ok=True)
    if args.file and not os.path.exists(args.file):
        print("no such audio file", file=sys.stderr)
        return 2
    meeting = store.new_meeting(args.title or "", "file" if args.file else "microphone")
    cmd = [sys.executable, "-m", "canopy_listen", "_record", meeting["id"]]
    if args.file:
        cmd += ["--file", os.path.abspath(args.file), "--speed", str(args.speed)]
    log = open(store.meeting_dir(meeting["id"]) / "recorder.log", "ab")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + (args.wait if args.wait is not None else START_TIMEOUT_SECONDS)
    state = "starting"
    while time.monotonic() < deadline:
        m = store.load(meeting["id"]) or {}
        state = m.get("state", "starting")
        if state in ("recording", "ended", "failed"):
            break
        time.sleep(0.2)
    ok = state in ("recording", "ended")
    _print(args, {"id": meeting["id"], "state": state}, f"{state}: {meeting['id']}")
    return 0 if ok else 1


def cmd_stop(args) -> int:
    current = store.active()
    pid = pid_of(current) if current else None
    if not pid:
        _print(args, {"stopped": False}, "Not recording")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and pid_of(current):
        time.sleep(0.2)
    m = store.load(current) or {}
    _print(args, {"stopped": True, "id": current, "state": m.get("state")}, f"Stopped {current}")
    return 0


def cmd_status(args) -> int:
    current = store.active()
    if current and pid_of(current):
        m = store.load(current) or {}
        _print(args, store.summary_row(m), f"{m.get('state')}: {current} ({m.get('heard_seconds', 0):.0f}s)")
    else:
        _print(args, {"state": "idle"}, "Not recording")
    return 0


def cmd_meetings(args) -> int:
    if args.action == "read":
        m = store.load(args.id) if args.id else None
        if m is None:
            print("no such meeting", file=sys.stderr)
            return 1
        out = {k: m[k] for k in ("id", "datetime", "title", "state", "segments", "speakers", "summary")}
        out["transcript"] = m.get("transcript", "") if args.transcript else ""
        print(json.dumps(out) if args.json else (m.get("transcript") or "\n".join(s["text"] for s in m["segments"])))
        return 0
    rows = [store.summary_row(m) for m in store.listing(args.limit)]
    print(json.dumps(rows) if args.json else "\n".join(f"{r['datetime']}  {r['id']}  {r['state']}" for r in rows))
    return 0


def cmd_delete(args) -> int:
    if pid_of(args.id):
        print("still recording; stop it first", file=sys.stderr)
        return 1
    return 0 if store.delete(args.id) else 1


def cmd_queue_file(args) -> int:
    if not os.path.exists(args.file):
        print("no such audio file", file=sys.stderr)
        return 2
    store.home().mkdir(parents=True, exist_ok=True)
    (store.home() / "next-input.json").write_text(json.dumps({"file": os.path.abspath(args.file), "speed": args.speed}))
    print("The next start plays this file instead of the microphone.")
    return 0


def cmd_models(args) -> int:  # noqa: ARG001
    from .models import download

    download()
    print("models ready")
    return 0


def cmd_record(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source = FileSource(args.file, args.speed) if args.file else MicSource()
    return record(args.id, source)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="canopy-listen", description="Record and transcribe meetings on this Mac.")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="start recording a meeting")
    s.add_argument("--title", default="")
    s.add_argument("--file", help="play this audio file in instead of the microphone (for testing)")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--wait", type=float, default=None, help="seconds to wait for recording to begin")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_start)
    for name, func, text in (("stop", cmd_stop, "stop the meeting being recorded"),
                             ("status", cmd_status, "what is being recorded now")):
        x = sub.add_parser(name, help=text)
        x.add_argument("--json", action="store_true")
        x.set_defaults(func=func)
    m = sub.add_parser("meetings", help="list or read meetings")
    m.add_argument("action", nargs="?", choices=["read"])
    m.add_argument("id", nargs="?")
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--transcript", action="store_true")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_meetings)
    d = sub.add_parser("delete", help="delete a meeting and its recording")
    d.add_argument("id")
    d.set_defaults(func=cmd_delete)
    sub.add_parser("models", help="download the models now").set_defaults(func=cmd_models)
    q = sub.add_parser("queue-file", help="for testing: the next start plays this audio file instead of the microphone")
    q.add_argument("file")
    q.add_argument("--speed", type=float, default=1.0)
    q.set_defaults(func=cmd_queue_file)
    r = sub.add_parser("_record")
    r.add_argument("id")
    r.add_argument("--file")
    r.add_argument("--speed", type=float, default=1.0)
    r.set_defaults(func=cmd_record)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
