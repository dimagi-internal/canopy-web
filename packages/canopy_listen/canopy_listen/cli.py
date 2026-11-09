"""canopy-listen: record a meeting on this Mac and transcribe it here, live.

  canopy-listen start [--title T] [--file AUDIO [--speed N]] [--json]
  canopy-listen stop [--json]
  canopy-listen status [--json]
  canopy-listen meetings [--limit N] [--json]
  canopy-listen meetings read ID [--transcript] [--json]
  canopy-listen delete ID
  canopy-listen models [--json]                   every model it can use, and which are downloaded
  canopy-listen models download [NAME ...]        the configured ones, or those named
  canopy-listen config [KEY VALUE]                show, or set asr / speakers / final / language / same_voice
  canopy-listen bench DIR [--asr a,b|all] [--speakers x,y|all] [--final x,y|all]
  canopy-listen bench-data DIR                    build a test set from published recordings
  canopy-listen queue-file AUDIO [--speed N]   for testing: the next start plays AUDIO
  canopy-listen serve [--detach] [--port 7351]    the local page (http://127.0.0.1:7351) and the relay
                                                  for a browser tab or a paired phone

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
from pathlib import Path

from . import store
from .recorder import FileSource, MicSource, RelaySource, pid_of, record

START_TIMEOUT_SECONDS = 180  # the first start downloads the models
RELAY_SOURCES = ("phone", "browser-tab", "browser-mic")


def _print(args, data, human: str) -> None:
    print(json.dumps(data) if getattr(args, "json", False) else human)


def cmd_start(args) -> int:
    current = store.active()
    if current and pid_of(current):
        _print(args, {"id": current, "state": "recording", "already": True}, f"Already recording {current}")
        return 0
    queued = store.home() / "next-input.json"
    relay, source = None, "file" if args.file else "microphone"
    if not args.file and queued.exists():
        # The next start, once, records something other than the microphone:
        #   {"relay": id, "source": ...}: audio `canopy-listen serve` relays (a browser tab, the
        #     browser's microphone, a paired phone), queued by serve before it starts the app;
        #   {"file": path, "speed": n}: a file played in, for testing (`canopy-listen queue-file`).
        try:
            spec = json.loads(queued.read_text())
            if spec.get("relay"):
                relay = str(spec["relay"])
                source = spec.get("source") if spec.get("source") in RELAY_SOURCES else "relay"
            else:
                args.file, args.speed = spec["file"], float(spec.get("speed", 1.0))
                source = "file"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            queued.unlink(missing_ok=True)
    if args.file and not os.path.exists(args.file):
        print("no such audio file", file=sys.stderr)
        return 2
    meeting = store.new_meeting(args.title or "", source)
    if relay:
        meeting["input"] = relay
        store.save(meeting)
    cmd = [sys.executable, "-m", "canopy_listen", "_record", meeting["id"]]
    if relay:
        cmd += ["--relay", relay, "--source", source]
    elif args.file:
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


def _downloaded(repo: str) -> bool:
    cache = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"
    snapshots = cache / ("models--" + repo.replace("/", "--")) / "snapshots"
    # A refused or broken download leaves a folder with no config: not downloaded.
    return any(snapshots.glob("*/config.json")) if snapshots.exists() else False


def cmd_models(args) -> int:
    from . import config, engines

    chosen = config.load()
    if args.action == "download":
        names = args.names or [chosen["asr"], chosen["speakers"], chosen["final"]]
        for name in names:
            if name in engines.SPEECH:
                engines.Recogniser(name, chosen["language"])
            elif name == "titanet":
                engines.voice_model_path()
            elif name == "sortformer":
                engines.SortformerStream()
            elif name == "pyannote":
                engines.segmentation_model_path()
                engines.voice_model_path()
            elif name != "none":
                print(f"unknown model {name!r}", file=sys.stderr)
                return 2
            print(f"{name}: ready")
        return 0
    rows = [{"kind": "asr", "name": s.name, "repo": s.repo, "by": "sentence" if s.timed else "pause",
             "languages": s.languages, "downloaded": _downloaded(s.repo), "chosen": s.name == chosen["asr"],
             "note": s.note} for s in engines.SPEECH.values()]
    rows += [{"kind": "speakers", "name": k, "chosen": k == chosen["speakers"], "note": v}
             for k, v in engines.SPEAKERS.items()]
    rows += [{"kind": "final", "name": k, "chosen": k == chosen["final"], "note": v} for k, v in engines.FINAL.items()]
    if args.json:
        print(json.dumps(rows))
        return 0
    for kind in ("asr", "speakers", "final"):
        print({"asr": "Speech", "speakers": "Speakers, live", "final": "Speakers, after the meeting"}[kind])
        for r in (r for r in rows if r["kind"] == kind):
            mark = "*" if r["chosen"] else " "
            extra = ""
            if kind == "asr":
                extra = f"  [{r['by']}, {r['languages']}{', downloaded' if r['downloaded'] else ''}]"
            print(f" {mark} {r['name']:18} {r['note']}{extra}")
    return 0


def cmd_config(args) -> int:
    from . import config

    if args.key:
        if args.value is None:
            print("usage: canopy-listen config KEY VALUE", file=sys.stderr)
            return 2
        try:
            current = config.save({args.key: args.value})
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        current = config.load()
    print(json.dumps(current, indent=2))
    return 0


def _choice(value: str, every: dict, default: list) -> list:
    if not value:
        return default
    if value == "none" and "none" not in every:
        return []
    return list(every) if value == "all" else [v.strip() for v in value.split(",") if v.strip()]


def cmd_bench(args) -> int:
    from . import bench, config, engines

    chosen = config.load()
    results = bench.run(Path(args.directory),
                        asr=_choice(args.asr, engines.SPEECH, [chosen["asr"]]),
                        speakers=_choice(args.speakers, engines.SPEAKERS, [chosen["speakers"]]),
                        final=_choice(args.final, engines.FINAL, []),
                        base_asr=chosen["asr"], language=chosen["language"], same_voice=chosen["same_voice"])
    out = store.home() / "bench" / time.strftime("%Y%m%d-%H%M%S.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"saved: {out}")
    return 0


def cmd_bench_data(args) -> int:
    from . import benchdata

    benchdata.build(Path(args.directory))
    return 0


def cmd_turns(args) -> int:
    from . import engines
    from .recorder import read_audio

    print(json.dumps(engines.turns_here(args.name, read_audio(args.wav))))
    return 0


def cmd_record(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.relay:
        source = RelaySource(args.relay, args.source or "relay")
    else:
        source = FileSource(args.file, args.speed) if args.file else MicSource()
    return record(args.id, source)


def cmd_serve(args) -> int:
    from . import serve

    if args.detach:
        return serve.detach(args.port)
    return serve.run(args.port)


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
    mo = sub.add_parser("models", help="every model it can use; `models download` fetches them")
    mo.add_argument("action", nargs="?", choices=["download"])
    mo.add_argument("names", nargs="*")
    mo.add_argument("--json", action="store_true")
    mo.set_defaults(func=cmd_models)
    co = sub.add_parser("config", help="show, or set, which models a recording uses")
    co.add_argument("key", nargs="?")
    co.add_argument("value", nargs="?")
    co.set_defaults(func=cmd_config)
    be = sub.add_parser("bench", help="compare models on a folder of recordings with known answers")
    be.add_argument("directory")
    be.add_argument("--asr", default="", help="comma-separated, or all (default: the configured one)")
    be.add_argument("--speakers", default="", help="comma-separated, or all")
    be.add_argument("--final", default="", help="comma-separated, or all (default: none)")
    be.set_defaults(func=cmd_bench)
    bd = sub.add_parser("bench-data", help="build a test set from published recordings (about 360 MB, once)")
    bd.add_argument("directory")
    bd.set_defaults(func=cmd_bench_data)
    q = sub.add_parser("queue-file", help="for testing: the next start plays this audio file instead of the microphone")
    q.add_argument("file")
    q.add_argument("--speed", type=float, default=1.0)
    q.set_defaults(func=cmd_queue_file)
    tu = sub.add_parser("_turns")
    tu.add_argument("name")
    tu.add_argument("wav")
    tu.set_defaults(func=cmd_turns)
    r = sub.add_parser("_record")
    r.add_argument("id")
    r.add_argument("--file")
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--relay")
    r.add_argument("--source")
    r.set_defaults(func=cmd_record)
    sv = sub.add_parser("serve", help="the local page, and the relay for a browser tab or a paired phone")
    sv.add_argument("--port", type=int, default=7351)
    sv.add_argument("--detach", action="store_true", help="start it in the background if it isn't running")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
