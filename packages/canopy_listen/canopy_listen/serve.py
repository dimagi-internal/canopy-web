"""canopy-listen serve: the one long-lived process behind the local page and the phone relay.

  * A unix socket, ~/.canopy-listen/serve.sock (0600). Its first line is a JSON op:
      session  from the runner's listen pipe: ciphertext frames both ways (see relaycrypto);
      audio    from the recorder's RelaySource: serve sends that input's int16 PCM;
      ping     answers {"ok": true, "name": ...}.
    After the op line, frames are length-prefixed (4 bytes, big-endian). A zero-length audio
    frame means the input ended.
  * http://127.0.0.1:7351 (web.py, when aiohttp is installed): the page, its JSON API and the
    socket a browser tab's audio arrives on. Every request must name this host, and every
    change must come from this page's own origin, so another website can't start a recording.
  * A meeting, from the page or a phone: queue the input (next-input.json), run
    `canopy-runner app start meeting-assistant --accept-notice`, follow meeting.json and the
    app's run.json, and push state and words back. End runs `app end`, Discard `app stop`.

It logs states and counts only: never a word, a frame, or a key.

The logic here needs only numpy and cryptography, so it is tested without a network or a
web library; web.py is the thin aiohttp layer over the `api_*` methods.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import hmac
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import numpy as np

from . import relaycrypto as rc
from . import store
from .recorder import _name_speakers, _transcript, pid_of

log = logging.getLogger("canopy_listen.serve")

APP = "meeting-assistant"
DEFAULT_PORT = 7351
PAIR_SECONDS = 600
PAIR_FORGET_SECONDS = 3600  # an expired secret is kept a while, so its phone hears "expired"
SAMPLE_RATE = 16000
MAX_BUFFER_BYTES = SAMPLE_RATE * 2 * 300  # five minutes waiting for the recorder to load its models
MAX_AUDIO_MESSAGE = 64 * 1024
SOURCES = ("microphone", "browser-tab", "browser-mic", "phone")
FOLLOW_SECONDS = 0.25
NAME_LIMIT = 60
DEFAULT_NOTICE = "What's said in this meeting leaves your Mac. Tell the people in it."


# --- where things are ---------------------------------------------------------------


def canopy_home() -> Path:
    """The runner's folder (~/.canopy): runner.json, and each local app's run.json."""
    return Path(os.environ.get("CANOPY_LISTEN_CANOPY_HOME") or Path.home() / ".canopy")


def runner_info() -> dict:
    """The runner id and Canopy address, for the pairing link. Never the runner's token."""
    try:
        raw = json.loads((canopy_home() / "runner.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {"runner_id": str(raw.get("runner_id") or ""), "base_url": str(raw.get("base_url") or "").rstrip("/")}
    return out if out["runner_id"] and out["base_url"].startswith("https://") else {}


def laptop_name() -> str:
    try:
        name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                              timeout=3, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return (name or socket.gethostname().removesuffix(".local") or "This Mac")[:NAME_LIMIT]


def runner_argv() -> list:
    """canopy-runner, as the menu bar would run it. CANOPY_LISTEN_RUNNER replaces it (tests)."""
    override = os.environ.get("CANOPY_LISTEN_RUNNER")
    if override:
        return shlex.split(override)
    return [shutil.which("canopy-runner") or str(Path.home() / ".local/bin/canopy-runner")]


def run_command(argv: list, timeout: float = 300) -> tuple[int, str]:
    """(exit code, the end of what it printed). The runner's own messages, never a meeting's words."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return res.returncode, (res.stdout + res.stderr)[-400:]
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _utc_epoch(text: str) -> float:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.UTC).timestamp()
    except (TypeError, ValueError):
        return 0.0


# --- keys, phones, pairing ----------------------------------------------------------


class LaptopKey:
    """This Mac's static relay key: made once, in relay-key.json (0600)."""

    def __init__(self, path: Path | None = None):
        self.path = path or store.home() / "relay-key.json"
        self._key = None

    def private(self):
        if self._key is None:
            try:
                self._key = rc.private_from_pem(json.loads(self.path.read_text())["private_pem"])
            except (OSError, ValueError, KeyError) as exc:
                if not isinstance(exc, FileNotFoundError):
                    # There, but unreadable. A new key unpairs every phone, so keep the old
                    # file for whoever looks, and say so: phones are told to pair again.
                    aside = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
                    with contextlib.suppress(OSError):
                        self.path.rename(aside)
                    log.warning("relay key was unreadable (kept as %s); made a new one: phones must pair again",
                                aside.name)
                self._key = rc.new_private()
                write_private(self.path, json.dumps({"private_pem": rc.private_to_pem(self._key),
                                                     "public": rc.public_b64(self._key), "created": time.time()}))
        return self._key

    def public(self) -> str:
        return rc.public_b64(self.private())


class Phones:
    """Paired phones: phones.json, [{id, key, label, paired_at}]. Public keys only."""

    def __init__(self, path: Path | None = None):
        self.path = path or store.home() / "phones.json"

    def all(self) -> list:
        try:
            phones = json.loads(self.path.read_text()).get("phones") or []
            return [p for p in phones if isinstance(p, dict) and p.get("key")]
        except (OSError, ValueError, AttributeError):
            return []

    def _write(self, phones: list) -> None:
        write_private(self.path, json.dumps({"phones": phones}, indent=2))

    def add(self, key: str, label: str) -> dict:
        phones = [p for p in self.all() if p["key"] != key]
        phone = {"id": uuid.uuid4().hex[:12], "key": key, "label": label, "paired_at": time.time()}
        self._write(phones + [phone])
        return phone

    def remove(self, phone_id: str) -> bool:
        phones = self.all()
        kept = [p for p in phones if p.get("id") != phone_id]
        self._write(kept)
        return len(kept) != len(phones)

    def known(self, key) -> dict | None:
        return next((p for p in self.all() if isinstance(key, str) and p["key"] == key), None)


class Pairings:
    """One-time pairing secrets, made by the page's "Pair a phone": each works once, for 10 minutes."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.entries: dict = {}

    def new(self) -> dict:
        now = self.clock()
        self.entries = {k: e for k, e in self.entries.items() if e["expires"] + PAIR_FORGET_SECONDS > now}
        entry = {"id": uuid.uuid4().hex[:12], "secret": rc.new_pairing_secret(), "expires": now + PAIR_SECONDS,
                 "label": None}
        self.entries[entry["id"]] = entry
        return entry

    def live(self) -> bool:
        now = self.clock()
        return any(e["label"] is None and e["expires"] > now for e in self.entries.values())

    def known(self) -> bool:
        return bool(self.entries)

    def open(self, session_id: str, frame: bytes):
        """(entry, message, sealer to answer with) for the secret this first frame was made
        with, or None. A secret that has been used or has expired still opens its frame, so
        the phone can be told why it was refused."""
        for entry in list(self.entries.values()):
            up, down = rc.pair_keys(entry["secret"], session_id)
            try:
                kind, body = rc.Opener(up, session_id).open(frame)
            except rc.BadFrame:
                continue
            if kind != rc.CONTROL:
                return None
            try:
                message = json.loads(body)
            except ValueError:
                return None
            return entry, message, rc.Sealer(down, session_id)
        return None

    def status(self, pairing_id: str) -> dict | None:
        entry = self.entries.get(pairing_id)
        if entry is None:
            return None
        if entry["label"] is not None:
            return {"state": "paired", "label": entry["label"]}
        if entry["expires"] <= self.clock():
            return {"state": "expired"}
        return {"state": "waiting", "expires": entry["expires"]}


def pairing_url(base_url: str, runner_id: str, laptop_pub: str, secret: bytes, name: str, expires: float) -> str:
    blob = json.dumps({"r": runner_id, "k": laptop_pub, "s": rc.b64u(secret), "n": name, "x": int(expires)},
                      separators=(",", ":"))
    return f"{base_url}/listen#pair={rc.b64u(blob.encode())}"


# --- audio inputs -------------------------------------------------------------------


class Input:
    """Audio arriving from a browser or a phone, held until the recorder's RelaySource reads it."""

    def __init__(self, input_id: str, source: str):
        self.id, self.source = input_id, source
        self.buffer: deque = deque()
        self.buffered = 0
        self.ended = False
        self.ended_at = None
        self.level = 0.0
        self.seconds = 0.0
        self.producer = False
        self.consumer = False
        self.wake = asyncio.Event()

    def push(self, pcm: bytes) -> None:
        if self.ended:
            return
        pcm = bytes(pcm[:len(pcm) - len(pcm) % 2])
        if not pcm:
            return
        self.buffer.append(pcm)
        self.buffered += len(pcm)
        while self.buffered > MAX_BUFFER_BYTES and len(self.buffer) > 1:
            self.buffered -= len(self.buffer.popleft())
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        self.level = float(min(1.0, np.sqrt(np.mean(samples * samples)) / 32768.0 * 4))
        self.seconds += len(samples) / SAMPLE_RATE
        self.wake.set()

    def take(self) -> list:
        out = list(self.buffer)
        self.buffer.clear()
        self.buffered = 0
        return out

    def end(self) -> None:
        if not self.ended:
            self.ended, self.ended_at = True, time.monotonic()
            self.level = 0.0
            log.info("input %s (%s) ended after %.0fs", self.id[:8], self.source, self.seconds)
        self.wake.set()


class Hub:
    def __init__(self):
        self.inputs: dict = {}

    def create(self, source: str) -> Input:
        now = time.monotonic()
        self.inputs = {k: i for k, i in self.inputs.items() if not (i.ended and now - i.ended_at > 600)}
        inp = Input(uuid.uuid4().hex, source)
        self.inputs[inp.id] = inp
        return inp

    def get(self, input_id) -> Input | None:
        return self.inputs.get(input_id) if isinstance(input_id, str) else None

    def end(self, input_id) -> None:
        inp = self.get(input_id)
        if inp is not None:
            inp.end()


# --- the meeting --------------------------------------------------------------------


def app_state(since: float | None = None) -> dict:
    """Meeting assistant's run on this Mac, from ~/.canopy/apps/meeting-assistant: running or
    not, its chat link, and whether it saved the meeting (an event in its log). Read only."""
    folder = canopy_home() / "apps" / APP
    try:
        record = json.loads((folder / "run.json").read_text())
        if not isinstance(record, dict):
            record = {}
    except (OSError, ValueError):
        record = {}
    alive = False
    try:
        fd = os.open(folder / "host.lock", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            alive = True  # the supervisor holds it
        finally:
            os.close(fd)
    except OSError:
        pass
    fresh = bool(record) and (since is None or float(record.get("started_at") or 0) >= since - 5)
    running = alive and not record.get("stopped_at") and fresh
    url = str(record.get("session_url") or "") if fresh else ""
    return {"running": running, "state": str(record.get("state") or "") if fresh else "",
            "session_url": url if url.startswith("https://") else "",
            "stop_reason": str(record.get("stop_reason") or "") if fresh else "",
            "saved": fresh and since is not None and _saved_since(folder / "log.jsonl", since)}


def _saved_since(path: Path, since: float) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(tail):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "event" and row.get("name") == "saved" \
                and float(row.get("t") or 0) >= since:
            return True
    return False


def phase_of(current: dict | None, meeting: dict | None, app: dict) -> tuple[str, str]:
    """What the page and the phone show: idle, starting, recording, listening, ending,
    saved, ended, failed or discarded, and a short reason code."""
    if meeting is None:
        if current:
            return current["phase"], current.get("detail", "")
        return "idle", ""
    state = meeting.get("state")
    discarding = bool(current and current.get("discarding"))
    if state in ("starting", "loading"):
        return ("ending" if discarding else "starting"), ""
    if state == "recording":
        if current and (current.get("ending") or discarding):
            return "ending", ""
        return ("listening" if meeting.get("segments") else "recording"), ""
    if state == "finishing":
        return "ending", "finishing"
    if state == "failed":
        return "failed", str(meeting.get("error") or "")
    if discarding:
        return "ending", ""
    if app.get("saved"):
        return "saved", ""
    if app.get("running"):
        return "ending", "saving"
    return "ended", ""


PHONE_STATE = {"idle": "starting", "starting": "starting", "recording": "recording", "listening": "recording",
               "ending": "ending", "saved": "ended", "ended": "ended", "failed": "failed", "discarded": "ended"}
#: How each finished phase reads on the phone (the `detail` of its `ended` state): only
#: `saved` may be shown as saved.
PHONE_END = {"saved": "saved", "ended": "not_saved", "discarded": "discarded"}
#: A meeting in one of these states has a recorder running (it writes its pid first).
RECORDER_STATES = ("loading", "recording", "finishing")
#: A phone meeting whose pipe went away (its runner restarted) is kept this long for the
#: runner to rejoin; longer than the server's PEER_GONE, which ends it first.
DETACHED_GRACE = 200.0
#: Segments go to the phone in batches, so a step that commits many (the first after the
#: models load) can't trip the relay's message rate.
SEGMENT_BATCH = 20
SEGMENT_BATCH_CHARS = 20_000
TERMINAL = ("saved", "ended", "failed", "discarded")


#: A start that hasn't become a recording by now never will (the first start downloads
#: models, within the recorder's own 180 s).
BIND_DEADLINE = 240


def _unqueue(input_id: str) -> None:
    """Remove next-input.json if it is still this input's."""
    queued = store.home() / "next-input.json"
    try:
        if json.loads(queued.read_text()).get("relay") == input_id:
            queued.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass


class Controller:
    """One meeting at a time, started from the page or a phone. `run` runs a command
    (argv -> (code, output)); tests pass a fake."""

    def __init__(self, hub: Hub, run=None, clock=time.time):
        self.hub = hub
        self.run = run or run_command
        self.clock = clock
        self.current: dict | None = None
        self.tasks: set = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def active_meeting(self) -> dict | None:
        meeting_id = store.active()
        return store.load(meeting_id) if meeting_id and pid_of(meeting_id) else None

    def busy(self) -> bool:
        if self.active_meeting():
            return True
        c = self.current
        return bool(c and c["phase"] == "starting" and not c.get("meeting_id"))

    def start(self, source: str, input_id: str | None = None) -> dict:
        """Queue the input and start Meeting assistant in the background: the person already
        accepted its notice, on the page or the phone."""
        if source not in SOURCES:
            return {"ok": False, "reason": "unknown_input"}
        if self.busy():
            return {"ok": False, "reason": "busy"}
        queued = store.home() / "next-input.json"
        if input_id:
            store.home().mkdir(parents=True, exist_ok=True)
            queued.write_text(json.dumps({"relay": input_id, "source": source, "queued_at": self.clock()}))
        else:
            queued.unlink(missing_ok=True)  # the microphone: nothing queued, and nothing stale either
        self.current = {"source": source, "input": input_id, "started_at": self.clock(), "phase": "starting",
                        "detail": "", "meeting_id": None, "ending": False, "discarding": False}
        log.info("starting a meeting from %s", source)
        self._spawn(self._launch(self.current))
        return {"ok": True, "input": input_id}

    async def _launch(self, current: dict) -> None:
        code, output = await asyncio.to_thread(self.run, runner_argv() + ["app", "start", APP, "--accept-notice"])
        if current["discarding"]:
            # Discarded while it was starting: a recording that began meanwhile goes too.
            self.bind()
            if current.get("meeting_id"):
                self._stop_recorder(current)
            return
        if code != 0 and self.current is current and not current.get("meeting_id"):
            if current["input"]:
                (store.home() / "next-input.json").unlink(missing_ok=True)
                self.hub.end(current["input"])
            current["phase"], current["detail"] = "failed", _first_line(output) or "app_did_not_start"
            log.info("Meeting assistant didn't start (exit %d)", code)

    def bind(self) -> None:
        """Find the meeting the current start made: the one recording this input, or the
        microphone meeting that began after Start."""
        c = self.current
        if not c or c.get("meeting_id"):
            return
        ids = [store.active()] if store.active() else []
        if c["input"]:
            ids += [m["id"] for m in store.listing(5)]
        for meeting_id in ids:
            meeting = store.load(meeting_id)
            if not meeting:
                continue
            if c["input"]:
                match = meeting.get("input") == c["input"]
            else:
                match = meeting.get("source") == "microphone" and _utc_epoch(meeting["datetime"]) >= c["started_at"] - 2
            if match:
                c["meeting_id"] = meeting_id
                return

    def view(self) -> dict:
        self.bind()
        c = self.current
        if c and c["phase"] == "starting" and not c.get("meeting_id") and not c["discarding"] \
                and self.clock() - c["started_at"] > BIND_DEADLINE:
            # The app started (or said it had) but no recording ever took this input: give
            # up rather than stay busy, and leave nothing queued for the next start.
            c["phase"], c["detail"] = "failed", "did_not_start"
            if c["input"]:
                self.hub.end(c["input"])
                _unqueue(c["input"])
            log.info("a start never began recording")
        meeting = store.load(c["meeting_id"]) if c and c.get("meeting_id") else None
        if c and c.get("meeting_id") and meeting is None and c["phase"] != "discarded":
            c["phase"], c["detail"] = "ended", "deleted"
        if meeting is None and (c is None or c["phase"] in TERMINAL):
            other = self.active_meeting()  # started elsewhere: the menu bar, or the command line
            if other is not None:
                meeting, c = other, None
        if meeting and meeting.get("state") in RECORDER_STATES and not pid_of(meeting["id"]):
            # Its recorder died (killed, out of memory) without saying so: the file still
            # says it's recording, and the audio still arriving would go nowhere.
            meeting = {**meeting, "state": "failed", "error": meeting.get("error") or "recorder_stopped"}
        app = app_state(c["started_at"] if c else None)
        phase, detail = phase_of(c, meeting, app)
        return {"phase": phase, "detail": detail, "meeting": meeting, "app": app, "current": c}

    async def end(self, current: dict | None = None) -> None:
        """End and keep: Meeting assistant finishes and saves, as when a meeting ends itself."""
        c = current or self.current
        if c is not None:
            c["ending"] = True
        log.info("ending the meeting")
        code, _ = await asyncio.to_thread(self.run, runner_argv() + ["app", "end", APP])
        if code != 0:
            self._stop_recorder(c)  # no app run to end: end the recording itself
        if c and c["input"]:
            self.hub.end(c["input"])

    async def discard(self, current: dict | None = None) -> None:
        """Stop everything, send nothing more, and delete the recording on this Mac."""
        c = current or self.current
        if c is None:
            return
        c["discarding"] = True
        log.info("discarding the meeting")
        await asyncio.to_thread(self.run, runner_argv() + ["app", "stop", APP])
        if c["input"]:
            self.hub.end(c["input"])
        self.bind()
        self._stop_recorder(c)
        meeting_id = c.get("meeting_id")
        for _ in range(120):
            if not (meeting_id and pid_of(meeting_id)):
                break
            await asyncio.sleep(0.5)
        if meeting_id:
            store.delete(meeting_id)
        c["phase"], c["detail"], c["meeting_id"] = "discarded", "", None
        if c["input"]:
            (store.home() / "next-input.json").unlink(missing_ok=True)

    def _stop_recorder(self, c: dict | None) -> None:
        meeting_id = (c or {}).get("meeting_id") or store.active()
        pid = pid_of(meeting_id) if meeting_id else None
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    def dismiss(self) -> None:
        """The page's "Start another": forget a finished meeting. A live one stays."""
        if self.current and self.view()["phase"] in TERMINAL:
            self.current = None


def rename(meeting_id: str, number, name) -> dict | None:
    """Name a speaker. While recording, the recorder merges names.json at its next step;
    otherwise meeting.json (and an ended meeting's transcript) is updated here."""
    meeting = store.load(meeting_id)
    if (meeting is None or not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < 100
            or not isinstance(name, str)):
        return None
    store.set_name(meeting_id, number, name)
    _name_speakers(meeting)
    if not pid_of(meeting_id):
        if meeting.get("state") == "ended":
            meeting["transcript"] = _transcript(meeting)
        store.save(meeting)
    return meeting


def _first_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[0][:160] if lines else ""


# --- the phone, through the relay ---------------------------------------------------


class RelaySession:
    """One relay session from the runner's pipe. `send` writes a frame back (bytes ->
    awaitable). Every inbound frame goes to `receive`; False means end the session.

    A meeting outlives the pipe that brought it: when the pipe closes without the server
    ending the session (the runner restarted), the session waits DETACHED_GRACE for the
    runner to bring a new pipe (`attach`), keeping its keys, counters, input and recorder.
    What it would have sent meanwhile is dropped, and resent from where the phone says it
    is (`resume`). A frame that doesn't open (forged, or replayed by a pipe that resent
    it) is dropped on its own; only a reason code is logged."""

    def __init__(self, serve: Serve, session_id: str, kind: str, hello, send):
        self.serve, self.sid, self.kind, self.send_raw = serve, session_id, kind, send
        self.conn = 1  # which pipe is attached: a newer one replaces an older
        self.attached = True
        self.finished = False
        self.grace: asyncio.Task | None = None
        self.bad_frames = 0
        self.sent = {"segments": 0, "state": None, "speakers": None, "chat": False, "partial": ""}
        if isinstance(hello, str):
            try:
                hello = json.loads(hello)
            except ValueError:
                hello = {}
        self.hello = hello if isinstance(hello, dict) else {}
        self.opener = self.sealer = None
        self.start_nonce = ""  # set by open(); `start` must carry it back
        self.laptop_nonce: bytes | None = None  # set when pairing by code
        self.input: Input | None = None
        self.current: dict | None = None
        self.follow: asyncio.Task | None = None

    async def open(self) -> bool:
        if not isinstance(self.sid, str) or not self.sid:
            return False
        if self.kind == "pair" and self.hello.get("commit"):
            return await self._code_hello()
        if self.kind == "pair":
            return self.serve.pairings.known()
        if self.kind != "meeting":
            return False
        phone = self.serve.phones.known(self.hello.get("phone_key"))
        if phone is None:
            # In the clear (the phone can't be answered under keys this Mac doesn't share
            # with it), so the phone can say "pair again" rather than waiting it out.
            log.info("refused a meeting session from a phone that isn't paired")
            await self.send_raw(rc.handshake_frame({"type": "refused", "reason": "unpaired"}))
            return False
        try:
            up, down = rc.session_keys_laptop(self.serve.key.private(), str(self.hello.get("phone_eph") or ""),
                                              phone["key"], self.sid)
        except (ValueError, TypeError):
            log.info("refused a meeting session: bad keys")
            return False
        self.opener, self.sealer = rc.Opener(up, self.sid), rc.Sealer(down, self.sid)
        # Fresh from this Mac, and `start` must carry it back sealed. The keys come from the
        # phone's side and the session id alone, so a server that kept an old session's
        # frames could otherwise replay them here and start a meeting nobody is having.
        self.start_nonce = rc.b64u(os.urandom(16))
        await self.control({"type": "ready", "name": self.serve.name, "nonce": self.start_nonce})
        log.info("phone session open")
        return True

    async def control(self, message: dict) -> None:
        frame = self.sealer.control(message)  # sealed and written together: counters stay in order
        if not self.attached:
            return  # no pipe: dropped, and resent from where the phone says it is
        try:
            await self.send_raw(frame)
        except (ConnectionError, OSError):
            self.attached = False

    def attach(self, send) -> int:
        """A new pipe for this meeting (the runner rejoined): carry on through it."""
        self.send_raw, self.conn, self.attached = send, self.conn + 1, True
        if self.grace is not None:
            self.grace.cancel()
            self.grace = None
        self.resync(None)
        log.info("phone session rejoined")
        return self.conn

    def detach(self, conn: int) -> None:
        """Pipe `conn` closed without an end: wait for the runner to bring another."""
        if conn != self.conn or self.finished:
            return
        self.attached = False
        log.info("phone session waiting for its pipe")
        self.grace = asyncio.get_running_loop().create_task(self._expire(conn))

    async def _expire(self, conn: int) -> None:
        await asyncio.sleep(DETACHED_GRACE)
        if conn == self.conn and not self.attached:
            log.info("phone session's pipe never came back")
            await self.close()

    def resync(self, have) -> None:
        """Send again what the phone may have missed: the state, speakers, chat link and
        partial words, and every segment after the first `have` (None: keep the count)."""
        if isinstance(have, int) and not isinstance(have, bool) and have >= 0:
            self.sent["segments"] = min(have, self.sent["segments"])
        self.sent.update(state=None, speakers=None, chat=False, partial=None)

    async def receive(self, frame: bytes) -> bool:
        if self.kind == "pair":
            return await (self._code_reveal(frame) if self.laptop_nonce is not None else self._pair(frame))
        try:
            kind, body = self.opener.open(frame)
        except rc.BadFrame as bad:
            self.bad_frames += 1
            log.info("dropped a %s frame from the phone", bad.args[0])
            return self.bad_frames <= 200  # a stream of them is something else: end it
        if kind == rc.AUDIO:
            if self.input is not None:
                self.input.push(body)
            return True
        if kind != rc.CONTROL:
            log.info("dropped a phone session: unknown kind")
            return False
        try:
            message = json.loads(body)
        except ValueError:
            return False
        if not isinstance(message, dict):
            return False
        return await self._on_control(message)

    async def _on_control(self, message: dict) -> bool:
        what = message.get("type")
        if what == "start":
            if self.input is not None:
                return True
            if message.get("notice_accepted") is not True:
                await self.control({"type": "state", "state": "failed", "detail": "notice_not_accepted"})
                return True
            nonce = message.get("nonce")
            if not isinstance(nonce, str):
                await self.control({"type": "state", "state": "failed", "detail": "update_page"})
                return True
            if not hmac.compare_digest(nonce, self.start_nonce):
                log.info("dropped a start that doesn't answer this session's ready")
                return True
            self.input = self.serve.hub.create("phone")
            self.input.producer = True
            started = self.serve.controller.start("phone", self.input.id)
            if not started["ok"]:
                self.input.end()
                self.input = None
                await self.control({"type": "state", "state": "failed", "detail": started["reason"]})
                return True
            self.current = self.serve.controller.current
            await self.control({"type": "state", "state": "starting", "detail": ""})
            self.follow = asyncio.get_running_loop().create_task(self._follow())
        elif what in ("end", "discard"):
            if self._owns():
                await (self.serve.controller.end(self.current) if what == "end"
                       else self.serve.controller.discard(self.current))
        elif what == "resume":
            self.resync(message.get("have"))
            if self.follow is not None and self.follow.done() and self._owns():
                self.follow = asyncio.get_running_loop().create_task(self._follow())  # tell it the end again
        elif what == "rename":
            if self._owns():
                self.serve.controller.bind()
            if self._owns() and self.current.get("meeting_id"):
                name = message.get("name")
                if isinstance(name, str) and len(name) <= 200:
                    rename(self.current["meeting_id"], message.get("speaker"), name)
        return True

    def _owns(self) -> bool:
        return self.current is not None and self.serve.controller.current is self.current

    async def _follow(self) -> None:
        sent = self.sent
        while True:
            if not self._owns():
                if sent["state"] not in ("ended", "failed"):
                    await self.control({"type": "state", "state": "ended", "detail": "replaced"})
                return
            view = self.serve.controller.view()
            phase, detail = view["phase"], view["detail"]
            meeting = view["meeting"] or {}
            segments = meeting.get("segments") or []
            if self.attached:
                await self._send_segments(segments)
            partial = meeting.get("partial") or ""
            if not isinstance(partial, str) or phase in TERMINAL:
                partial = ""
            if self.attached and partial != sent["partial"]:
                await self.control({"type": "partial", "text": partial[:2000]})
                sent["partial"] = partial
            speakers = meeting.get("speakers") or []
            if self.attached and speakers and speakers != sent["speakers"]:
                await self.control({"type": "speakers", "speakers": speakers})
                sent["speakers"] = speakers
            url = view["app"].get("session_url")
            if self.attached and url and not sent["chat"]:
                await self.control({"type": "chat", "url": url})
                sent["chat"] = True
            state = PHONE_STATE.get(phase, "starting")
            if phase in PHONE_END:
                detail = PHONE_END[phase]
            if self.attached and state != sent["state"]:
                await self.control({"type": "state", "state": state, "detail": detail})
                sent["state"] = state
            if phase in TERMINAL and self.attached:
                if phase == "saved":
                    await self.control({"type": "saved", "url": self.serve.meetings_url()})
                log.info("phone meeting finished: %s, %d segments", phase, sent["segments"])
                return
            await asyncio.sleep(FOLLOW_SECONDS)

    async def _send_segments(self, segments: list) -> None:
        """The segments the phone doesn't have yet, in batches."""
        while self.attached and self.sent["segments"] < len(segments):
            batch, chars = [], 0
            for seg in segments[self.sent["segments"]:]:
                text = str(seg.get("text", ""))[:4000]
                if batch and (len(batch) >= SEGMENT_BATCH or chars + len(text) > SEGMENT_BATCH_CHARS):
                    break
                batch.append({"start": seg.get("start", 0), "end": seg.get("end", 0),
                              "speaker": seg.get("speaker", 0), "text": text})
                chars += len(text)
            await self.control({"type": "segments", "from": self.sent["segments"], "items": batch})
            if self.attached:
                self.sent["segments"] += len(batch)

    async def _pair(self, frame: bytes) -> bool:
        """The first and only frame of a pair session. Answers, then the session closes."""
        found = self.serve.pairings.open(self.sid, frame)
        if found is None:
            log.info("dropped a pair session: no secret opens it")
            return False
        entry, message, sealer = found
        key, label = message.get("phone_key"), message.get("label")
        try:
            if message.get("type") != "pair" or not isinstance(label, str):
                raise ValueError
            rc.load_public(key)
        except (ValueError, TypeError, AttributeError):
            log.info("dropped a pair session: malformed")
            return False
        if entry["label"] is not None or entry["expires"] <= self.serve.pairings.clock():
            reason = "used" if entry["label"] is not None else "expired"
            await self.send_raw(sealer.control({"type": "refused", "reason": reason}))
            log.info("refused a pairing: %s", reason)
            return False
        label = " ".join(label.split())[:NAME_LIMIT] or "Phone"
        self.serve.phones.add(key, label)
        entry["label"] = label
        await self.send_raw(sealer.control({"type": "paired", "name": self.serve.name}))
        log.info("paired a phone")
        return False

    async def _refuse_in_clear(self, reason: str) -> bool:
        await self.send_raw(rc.handshake_frame({"type": "refused", "reason": reason}))
        log.info("refused a code pairing: %s", reason)
        return False

    async def _code_hello(self) -> bool:
        """Pairing by code, step 1: the phone has committed to its key; show ours."""
        if self.serve.confirming:
            return await self._refuse_in_clear("busy")
        if self.serve.pairings.clock() - self.serve.declined_at < DECLINE_COOLDOWN:
            return await self._refuse_in_clear("wait")  # each attempt is a dialog on this Mac
        self.laptop_nonce = os.urandom(rc.NONCE_BYTES)
        await self.send_raw(rc.handshake_frame({
            "type": "hello", "laptop_key": self.serve.key.public(), "nonce": rc.b64u(self.laptop_nonce),
            "name": self.serve.name}))
        log.info("code pairing: hello sent")
        return True

    async def _code_reveal(self, frame: bytes) -> bool:
        """Step 2: the phone reveals its key. If it matches its commitment, the person here
        compares the code; yes keeps the key. The session closes either way."""
        try:
            message = rc.read_handshake(frame)
            key, label = message.get("phone_key"), message.get("label")
            nonce = rc.unb64u(message.get("nonce"))
            if message.get("type") != "reveal" or not isinstance(label, str) or len(nonce) != rc.NONCE_BYTES:
                raise ValueError
            rc.load_public(key)
        except (rc.BadFrame, ValueError, TypeError, AttributeError):
            log.info("dropped a code pairing: malformed")
            return False
        if not hmac.compare_digest(rc.pair_commit(key, nonce), str(self.hello.get("commit") or "")):
            log.info("dropped a code pairing: the key doesn't match its commitment")
            return False
        if self.serve.confirming:
            return await self._refuse_in_clear("busy")
        code = rc.pair_code(self.sid, key, self.serve.key.public(), nonce, self.laptop_nonce)
        label = " ".join(label.split())[:NAME_LIMIT] or "A browser"
        self.serve.confirming = True
        try:
            allowed = await self.serve.confirm(label, code, self.serve.name)
        finally:
            self.serve.confirming = False
        if not allowed:
            self.serve.declined_at = self.serve.pairings.clock()
            return await self._refuse_in_clear("declined")
        try:
            _, down = rc.session_keys_laptop(self.serve.key.private(), str(self.hello.get("phone_eph") or ""),
                                             key, self.sid)
        except (ValueError, TypeError):
            log.info("dropped a code pairing: bad keys")
            return False
        self.serve.phones.add(key, label)
        await self.send_raw(rc.Sealer(down, self.sid).control({"type": "paired", "name": self.serve.name}))
        log.info("paired a phone by code")
        return False

    async def close(self) -> None:
        """The end of the session: the recorder hears its input end and finishes."""
        if self.finished:
            return
        self.finished, self.attached = True, False
        if self.grace is not None and self.grace is not asyncio.current_task():
            self.grace.cancel()
        if self.serve.relays.get(self.sid) is self:
            del self.serve.relays[self.sid]
        if self.follow is not None:
            self.follow.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.follow
        if self.input is not None:
            self.input.end()  # the recorder hears the end of its input and finishes the meeting


def _hello(hello) -> dict:
    if isinstance(hello, str):
        try:
            hello = json.loads(hello)
        except ValueError:
            return {}
    return hello if isinstance(hello, dict) else {}


# --- the whole service --------------------------------------------------------------


def allowed(host: str | None, origin: str | None, port: int, *, needs_origin: bool) -> bool:
    """Only this page may use the local server: the Host must be this server (so a DNS
    rebinding page can't reach it), and anything that changes something must come from this
    page's own origin (so another website can't start a recording)."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in hosts:
        return False
    if origin is None:
        return not needs_origin
    return origin in {f"http://{h}" for h in hosts}


CONFIRM_SECONDS = 120
#: After a pairing is declined (or nobody answered), new ones wait this long.
DECLINE_COOLDOWN = 30
DIALOG = """on run argv
  activate
  set answer to display dialog (item 1 of argv) with title "Canopy Listen" buttons {"Don't Allow", "Allow"} ¬
    default button "Don't Allow" cancel button "Don't Allow" giving up after ((item 2 of argv) as integer) with icon caution
  if gave up of answer then return "no"
  if button returned of answer is "Allow" then return "yes"
  return "no"
end run"""


async def ask_on_screen(label: str, code: str, name: str, seconds: int = CONFIRM_SECONDS) -> bool:
    """Ask the person at this Mac whether the page that wants to pair shows `code`. The
    label comes from the browser, so it goes in as an argument, never into the script."""
    if not shutil.which("osascript"):
        log.info("code pairing: no osascript, so no one can be asked")
        return False
    text = (f"“{label}” wants to pair with {name} to record meetings through Canopy.\n\n"
            f"Allow only if the Canopy page shows this code:\n\n{code[:3]} {code[3:]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", DIALOG, text, str(seconds),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), seconds + 15)
    except (OSError, asyncio.TimeoutError):
        log.info("code pairing: the dialog didn't answer")
        return False
    return out.decode().strip() == "yes"


class Serve:
    def __init__(self, *, run=None, clock=time.time, name: str | None = None, confirm=None):
        self.name = name or laptop_name()
        self.key = LaptopKey()
        self.phones = Phones()
        self.pairings = Pairings(clock)
        self.confirm = confirm or ask_on_screen
        self.confirming = False
        self.declined_at = float("-inf")
        self.hub = Hub()
        self.controller = Controller(self.hub, run=run, clock=clock)
        self.relays: dict[str, RelaySession] = {}  # meetings by session id, while they last
        self.app_info: dict = {}
        self._app_info_at = 0.0

    def meetings_url(self) -> str:
        base = runner_info().get("base_url")
        return f"{base}/meetings" if base else ""

    # --- the unix socket ---

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            op = json.loads(line or b"{}")
            if not isinstance(op, dict):
                raise ValueError
        except (TimeoutError, ValueError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
        try:
            if op.get("op") == "ping":
                writer.write(json.dumps({"ok": True, "name": self.name}).encode() + b"\n")
                await writer.drain()
            elif op.get("op") == "audio":
                await self._audio(op, writer)
            elif op.get("op") == "session":
                await self._session(op, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _audio(self, op: dict, writer: asyncio.StreamWriter) -> None:
        inp = self.hub.get(op.get("input"))
        if inp is None or inp.consumer:
            writer.write(json.dumps({"ok": False, "reason": "unknown_input" if inp is None else "taken"}).encode()
                         + b"\n")
            await writer.drain()
            return
        inp.consumer = True
        writer.write(b'{"ok": true}\n')
        log.info("recorder reading input %s", inp.id[:8])
        try:
            while True:
                inp.wake.clear()
                chunks = inp.take()
                for chunk in chunks:
                    writer.write(struct.pack(">I", len(chunk)) + chunk)
                await writer.drain()
                if inp.ended and not inp.buffer:
                    writer.write(struct.pack(">I", 0))
                    await writer.drain()
                    return
                if not inp.buffer:
                    await inp.wake.wait()
        finally:
            inp.consumer = False

    async def _session(self, op: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(frame: bytes) -> None:
            writer.write(struct.pack(">I", len(frame)) + frame)
            await writer.drain()

        sid, kind = op.get("session_id"), op.get("kind")
        session = self.relays.get(sid) if kind == "meeting" and isinstance(sid, str) else None
        if session is not None and session.hello.get("phone_eph") != _hello(op.get("hello")).get("phone_eph"):
            log.info("refused a rejoin: not the same phone session")
            return
        if session is not None:
            conn = session.attach(send)
        else:
            session = RelaySession(self, sid, kind, op.get("hello"), send)
            if not await session.open():
                return
            if kind == "meeting":
                self.relays[sid] = session
            conn = session.conn
        over = False  # the session is over, not just this pipe
        try:
            while session.conn == conn:
                (size,) = struct.unpack(">I", await reader.readexactly(4))
                if size == 0:  # the runner's explicit end: the server ended the session
                    over = True
                    break
                if size > rc.MAX_FRAME:
                    log.info("dropped a session: frame size")
                    over = True
                    break
                if not await session.receive(await reader.readexactly(size)):
                    over = True
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if over or kind != "meeting":
                await session.close()
            else:
                session.detach(conn)

    # --- what the page asks (web.py turns these into routes) ---

    def refresh_app_info(self, force: bool = False) -> None:
        """Meeting assistant's agent and notice, from `canopy-runner app list --json`, in the
        background: it can take a few seconds."""
        if not force and time.monotonic() - self._app_info_at < 600:
            return
        self._app_info_at = time.monotonic()

        def fetch():
            try:
                res = subprocess.run(runner_argv() + ["app", "list", "--json"], capture_output=True, text=True,
                                     timeout=60, stdin=subprocess.DEVNULL)
                apps = json.loads(res.stdout).get("apps") or []
            except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
                return
            for app in apps:
                if isinstance(app, dict) and app.get("key") == APP:
                    self.app_info = {"installed": True, "agent": str(app.get("agent") or ""),
                                     "notice": str(app.get("notice") or ""), "version": str(app.get("version") or "")}
                    return
            self.app_info = {"installed": False}

        threading.Thread(target=fetch, daemon=True).start()

    def api_status(self) -> dict:
        from . import config, engines

        self.refresh_app_info()
        chosen = config.load()
        info = runner_info()
        return {
            "name": self.name,
            "runner": {"ok": bool(info), "base_url": info.get("base_url", "")},
            "models": {k: chosen[k] for k in ("asr", "speakers", "final", "language")},
            "choices": {
                "asr": [{"name": s.name, "note": s.note, "languages": s.languages} for s in engines.SPEECH.values()],
                "speakers": [{"name": k, "note": v} for k, v in engines.SPEAKERS.items()],
                "final": [{"name": k, "note": v} for k, v in engines.FINAL.items()],
            },
            "app": {"agent": "", "notice": "", **self.app_info},
            "notice": self.app_info.get("notice") or DEFAULT_NOTICE,
            "phones": len(self.phones.all()),
        }

    def api_live(self, after: int = 0) -> dict:
        view = self.controller.view()
        meeting, current, app = view["meeting"], view["current"], view["app"]
        out = {"phase": view["phase"], "detail": view["detail"], "chat_url": app.get("session_url", ""),
               "app_running": app.get("running", False), "source": (current or {}).get("source", ""),
               "level": None, "meeting": None, "input": (current or {}).get("input")}
        inp = self.hub.get((current or {}).get("input"))
        if inp is not None and not inp.ended:
            out["level"] = round(inp.level, 3)
        if meeting is not None:
            segments = meeting.get("segments") or []
            after = after if 0 <= after <= len(segments) else 0
            out["source"] = meeting.get("source") or out["source"]
            out["meeting"] = {
                "id": meeting["id"], "title": meeting.get("title", ""), "datetime": meeting.get("datetime", ""),
                "state": meeting.get("state", ""), "heard_seconds": meeting.get("heard_seconds", 0),
                "speakers": meeting.get("speakers") or [], "count": len(segments), "after": after,
                "segments": [{"start": s.get("start", 0), "end": s.get("end", 0), "speaker": s.get("speaker", 0),
                              "text": s.get("text", "")} for s in segments[after:]],
            }
        return out

    def api_start(self, body: dict) -> tuple[int, dict]:
        source = body.get("input")
        if body.get("notice_accepted") is not True:
            return 400, {"ok": False, "reason": "notice_not_accepted"}
        if source not in ("microphone", "browser-tab", "browser-mic"):
            return 400, {"ok": False, "reason": "unknown_input"}
        if self.controller.busy():
            return 409, {"ok": False, "reason": "busy"}
        input_id = None
        if source != "microphone":
            inp = self.hub.create(source)
            input_id = inp.id
            # The page opens /ws/audio next; if it never does, the input ends rather than hang.
            asyncio.get_running_loop().call_later(30, lambda: None if inp.producer else inp.end())
        result = self.controller.start(source, input_id)
        if not result["ok"]:
            self.hub.end(input_id)
            return 409, result
        return 200, result

    async def api_end(self) -> tuple[int, dict]:
        if self.controller.current is None and self.controller.active_meeting() is None:
            return 409, {"ok": False, "reason": "not_recording"}
        await self.controller.end()
        return 200, {"ok": True}

    def api_discard(self) -> tuple[int, dict]:
        if self.controller.current is None:
            if self.controller.active_meeting() is None:
                return 409, {"ok": False, "reason": "not_recording"}
            # Recording started elsewhere: discard it all the same.
            self.controller.current = {"source": "microphone", "input": None, "started_at": self.controller.clock(),
                                       "phase": "starting", "detail": "", "meeting_id": store.active(),
                                       "ending": False, "discarding": False}
        self.controller._spawn(self.controller.discard())
        return 200, {"ok": True}

    def api_dismiss(self) -> tuple[int, dict]:
        self.controller.dismiss()
        return 200, {"ok": True}

    def api_rename(self, body: dict) -> tuple[int, dict]:
        meeting_id, name = body.get("meeting"), body.get("name")
        try:
            store.meeting_dir(str(meeting_id))
        except ValueError:
            return 400, {"ok": False}
        if not isinstance(name, str) or len(name) > 200:
            return 400, {"ok": False}
        meeting = rename(str(meeting_id), body.get("speaker"), name)
        if meeting is None:
            return 404, {"ok": False}
        return 200, {"ok": True, "speakers": meeting["speakers"]}

    def api_config(self, body: dict) -> tuple[int, dict]:
        from . import config

        changes = {k: v for k, v in body.items() if k in ("asr", "speakers", "final") and isinstance(v, str)}
        try:
            return 200, {"ok": True, "models": config.save(changes)}
        except ValueError as exc:
            return 400, {"ok": False, "reason": str(exc)}

    def api_pair(self) -> tuple[int, dict]:
        from . import qr

        info = runner_info()
        if not info:
            return 409, {"ok": False, "reason": "no_runner"}
        entry = self.pairings.new()
        url = pairing_url(info["base_url"], info["runner_id"], self.key.public(), entry["secret"], self.name,
                          entry["expires"])
        log.info("made a pairing code")
        return 200, {"ok": True, "id": entry["id"], "url": url, "svg": qr.svg(url), "expires": entry["expires"]}

    def api_pair_status(self, pairing_id: str) -> tuple[int, dict]:
        status = self.pairings.status(pairing_id)
        return (404, {"state": "unknown"}) if status is None else (200, status)

    def api_phones(self) -> dict:
        return {"phones": [{"id": p.get("id"), "label": p.get("label") or "Phone", "paired_at": p.get("paired_at")}
                           for p in self.phones.all()]}

    def api_remove_phone(self, phone_id: str) -> tuple[int, dict]:
        return (200, {"ok": True}) if self.phones.remove(phone_id) else (404, {"ok": False})

    def api_meetings(self) -> dict:
        rows = []
        for m in store.listing(50):
            row = store.summary_row(m)
            row["source"] = m.get("source", "")
            row["speakers"] = len(m.get("speakers") or [])
            rows.append(row)
        return {"meetings": rows}

    def api_meeting(self, meeting_id: str) -> tuple[int, dict]:
        try:
            meeting = store.load(meeting_id)
        except ValueError:
            meeting = None
        if meeting is None:
            return 404, {}
        keep = ("id", "datetime", "title", "state", "source", "heard_seconds", "speakers", "segments")
        return 200, {k: meeting.get(k) for k in keep}

    def api_delete_meeting(self, meeting_id: str) -> tuple[int, dict]:
        try:
            if pid_of(meeting_id):
                return 409, {"ok": False, "reason": "recording"}
            return (200, {"ok": True}) if store.delete(meeting_id) else (404, {"ok": False})
        except ValueError:
            return 400, {"ok": False}


# --- the daemon ---------------------------------------------------------------------


def ping(timeout: float = 2.0) -> dict | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(store.socket_path()))
            sock.sendall(b'{"op": "ping"}\n')
            data = b""
            while not data.endswith(b"\n"):
                got = sock.recv(4096)
                if not got:
                    break
                data += got
        answer = json.loads(data or b"{}")
        return answer if answer.get("ok") else None
    except (OSError, ValueError):
        return None


async def main(port: int = DEFAULT_PORT, serve: Serve | None = None, stop: asyncio.Event | None = None,
               web_page: bool = True) -> int:
    serve = serve or Serve()
    path = store.socket_path()
    path.unlink(missing_ok=True)
    unix = await asyncio.start_unix_server(serve.handle, path=str(path))
    os.chmod(path, 0o600)
    log.info("relay socket ready")
    web_runner = None
    if web_page:
        try:
            from . import web
        except ImportError:
            log.warning("no local page: aiohttp isn't installed (canopy-listen[mac] includes it)")
        else:
            web_runner = await web.start(serve, port)
            log.info("local page on http://127.0.0.1:%d", port)
            serve.refresh_app_info(force=True)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    unix.close()
    if web_runner is not None:
        await web_runner.cleanup()
    path.unlink(missing_ok=True)
    log.info("stopped")
    return 0


def run(port: int = DEFAULT_PORT) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    home = store.home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "serve.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("canopy-listen serve is already running", file=sys.stderr)
        return 1
    (home / "serve.pid").write_text(str(os.getpid()))
    try:
        return asyncio.run(main(port))
    except OSError as exc:
        log.error("couldn't start: %s", type(exc).__name__)
        print(f"couldn't start: {exc}", file=sys.stderr)
        return 1
    finally:
        (home / "serve.pid").unlink(missing_ok=True)


def detach(port: int = DEFAULT_PORT, wait: float = 15.0) -> int:
    """Start serve in the background unless it is already running. Idempotent."""
    if ping():
        print(f"canopy-listen serve is running: http://127.0.0.1:{port}")
        return 0
    home = store.home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "serve.log", "ab") as out:
        proc = subprocess.Popen([sys.executable, "-m", "canopy_listen", "serve", "--port", str(port)],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                close_fds=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if ping():
            print(f"canopy-listen serve started: http://127.0.0.1:{port}")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if ping():  # lost a race with another start: it's running, which is what was asked
        return 0
    print(f"canopy-listen serve didn't start; see {home / 'serve.log'}", file=sys.stderr)
    return 1
