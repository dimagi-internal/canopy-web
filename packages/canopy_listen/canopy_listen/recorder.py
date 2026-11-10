"""The recording process: audio in, meeting.json kept current, until told to stop.

One recorder runs at a time, detached from whatever started it. It logs states and
counts only: never a word that was said.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time

import numpy as np

from . import store
from .live import SAMPLE_RATE, LiveTranscriber

STEP_SECONDS = 1.0  # one pass over a full 24 s window takes ~0.2 s on an M-series Mac
log = logging.getLogger("canopy_listen")


class MicSource:
    """The default input device. macOS asks the person once for the microphone, on behalf
    of the app that started this."""

    name = "microphone"

    def __init__(self):
        self.chunks: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.stream = None

    def start(self) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            self.chunks.put(indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=1600,
                                     callback=callback)
        self.stream.start()

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()


def read_audio(path: str) -> np.ndarray:
    """Any file libsndfile reads (wav, aiff, flac, ogg), as 16 kHz mono. Apps run with a bare
    PATH, so this needs no ffmpeg."""
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


class FileSource:
    """An audio file played in as if it were live, `speed` times faster than real time.
    For testing the whole path without a microphone or anyone speaking."""

    def __init__(self, path: str, speed: float = 1.0):
        self.path, self.speed = path, max(speed, 0.01)
        self.name = "file"
        self.chunks: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._play, args=(read_audio(self.path),), daemon=True).start()

    def _play(self, audio: np.ndarray) -> None:
        step = SAMPLE_RATE // 10
        for i in range(0, len(audio), step):
            if self._stop.is_set():
                break
            self.chunks.put(audio[i:i + step])
            time.sleep(0.1 / self.speed)
        self.done.set()

    def stop(self) -> None:
        self._stop.set()


class RelayRefusedError(Exception):
    """`canopy-listen serve` doesn't know the input. Its argument is a reason code."""


class RelaySource:
    """Audio that `canopy-listen serve` relays from elsewhere on this Mac: a browser tab, the
    browser's microphone, or a paired phone (decrypted by serve; it never leaves the Mac).
    Serve sends int16 PCM frames, length-prefixed, and a zero-length frame when the input ends."""

    def __init__(self, input_id: str, name: str = "relay", path=None):
        self.input_id, self.name = input_id, name
        self.path = path
        self.chunks: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.sock = None

    def start(self) -> None:
        import json
        import socket

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.path or store.socket_path()))
        sock.sendall(json.dumps({"op": "audio", "input": self.input_id}).encode() + b"\n")
        line = b""
        while not line.endswith(b"\n"):
            got = sock.recv(1)
            if not got:
                break
            line += got
        answer = json.loads(line or b"{}")
        if not answer.get("ok"):
            sock.close()
            raise RelayRefusedError(str(answer.get("reason") or "refused"))
        sock.settimeout(None)
        self.sock = sock
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        import struct

        try:
            while True:
                head = self._exactly(4)
                if head is None:
                    break
                (size,) = struct.unpack(">I", head)
                if size == 0:
                    break
                body = self._exactly(size)
                if body is None:
                    break
                usable = len(body) - len(body) % 2
                self.chunks.put(np.frombuffer(body[:usable], dtype="<i2").astype(np.float32) / 32768.0)
        except OSError:
            pass
        finally:
            self.done.set()

    def _exactly(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            got = self.sock.recv(n - len(buf))
            if not got:
                return None
            buf += got
        return buf

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def _speakers(segments: list, previous: list | None = None, names: dict | None = None) -> list:
    """Everyone heard so far. A name the person gave (in names.json, or already on a speaker)
    stays when the list is recomputed; everyone else is "Speaker N"."""
    known = {s["number"]: s["name"] for s in previous or []
             if s.get("name") and s["name"] != f"Speaker {s['number'] + 1}"}
    known.update(names or {})
    numbers = sorted({s["speaker"] for s in segments})
    return [{"name": known.get(n) or f"Speaker {n + 1}", "number": n} for n in numbers]


def _name_speakers(meeting: dict) -> None:
    meeting["speakers"] = _speakers(meeting["segments"], meeting.get("speakers"), store.names(meeting["id"]))


def _transcript(meeting: dict) -> str:
    names = {s["number"]: s["name"] for s in meeting["speakers"]}
    return "\n".join(f"{names.get(s['speaker'], 'Speaker')}: {s['text']}" for s in meeting["segments"])


def record(meeting_id: str, source, *, recognise=None, embed=None, keep_audio: bool = True,
           settings: dict | None = None) -> int:
    meeting = store.load(meeting_id)
    if meeting is None:
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    (store.meeting_dir(meeting_id) / "pid").write_text(str(os.getpid()))
    store.set_active(meeting_id)
    writer = None
    started = False
    try:
        meeting["state"] = "loading"
        store.save(meeting)
        # Listen first, load the models second: what is said while they load waits in the
        # source's queue, so recording starts the moment audio can arrive.
        source.start()
        started = True
        meeting["state"] = "recording"
        store.save(meeting)
        log.info("recording %s from %s", meeting_id, source.name)
        chosen = settings or {}
        labeller = None
        if recognise is None:
            from . import config, engines

            chosen = settings or config.load()
            recognise = engines.Recogniser(chosen["asr"], chosen.get("language", "en"))
            labeller = engines.make_labeller(chosen["speakers"], chosen.get("same_voice", 0.66))
        meeting["engine"] = {k: chosen[k] for k in ("asr", "speakers", "final") if k in chosen}
        live = LiveTranscriber(recognise, embed, labeller=labeller, timed=getattr(recognise, "timed", True))
        if keep_audio:
            import soundfile as sf

            writer = sf.SoundFile(str(store.meeting_dir(meeting_id) / "audio.wav"), "w", SAMPLE_RATE, 1, "PCM_16")
        log.info("models ready for %s", meeting_id)
        next_step = time.monotonic() + STEP_SECONDS
        while not stop.is_set():
            try:
                chunk = source.chunks.get(timeout=0.2)
            except queue.Empty:
                chunk = None
            if chunk is not None:
                live.feed(chunk)
                if writer is not None:
                    writer.write(chunk)
            ended = source.done.is_set() and source.chunks.empty()
            if (time.monotonic() >= next_step and source.chunks.empty()) or ended:
                _add(meeting, live.step())
                _name_speakers(meeting)  # a rename reaches meeting.json within a step
                meeting["partial"] = live.pending
                meeting["heard_seconds"] = round(live.heard, 1)
                store.save(meeting)
                next_step = time.monotonic() + STEP_SECONDS
            if ended:
                break
        source.stop()
        started = False
        while not source.chunks.empty():
            chunk = source.chunks.get_nowait()
            live.feed(chunk)
            if writer is not None:
                writer.write(chunk)
        _add(meeting, live.step(final=True))
        meeting["partial"] = ""
        meeting["heard_seconds"] = round(live.heard, 1)
        if writer is not None:
            writer.close()
            writer = None
            _final_pass(meeting, chosen.get("final", "none"))
        meeting["state"] = "ended"
        _name_speakers(meeting)
        meeting["transcript"] = _transcript(meeting)
        meeting["summary"] = meeting["summary"] or "Recording ended."
        store.save(meeting)
        log.info("ended %s: %d segments, %d speakers, %.0fs", meeting_id, len(meeting["segments"]),
                 len(meeting["speakers"]), live.heard)
        return 0
    except Exception as exc:  # the reason, never the audio or words
        meeting["state"] = "failed"
        meeting["partial"] = ""
        meeting["error"] = type(exc).__name__
        meeting["summary"] = meeting["summary"] or "Recording failed."
        store.save(meeting)
        log.exception("recorder failed for %s", meeting_id)
        return 1
    finally:
        if started:  # the models failed to load, or something else broke mid-recording
            try:
                source.stop()
            except Exception:  # noqa: BLE001 — already failing; keep the first reason
                log.exception("stopping the source failed for %s", meeting_id)
        if writer is not None:
            writer.close()
        if store.active() == meeting_id:
            store.set_active(None)
        (store.meeting_dir(meeting_id) / "pid").unlink(missing_ok=True)


def _final_pass(meeting: dict, name: str) -> None:
    """Who spoke, decided again over the whole recording. A failure keeps the live labels."""
    if name == "none" or not meeting["segments"]:
        return
    meeting["state"] = "finishing"
    store.save(meeting)
    try:
        from . import engines

        audio = read_audio(str(store.meeting_dir(meeting["id"]) / "audio.wav"))
        meeting["segments"] = engines.relabel(meeting["segments"], engines.final_turns(name, audio))
        # Relabelling can renumber people, so a name given live is kept only where it was set by number.
        _name_speakers(meeting)
        meeting["relabelled_by"] = name
    except Exception as exc:  # noqa: BLE001 — the live labels stand
        meeting["final_error"] = type(exc).__name__
        log.exception("final pass %s failed for %s", name, meeting["id"])


def _add(meeting: dict, segments: list) -> None:
    if not segments:
        return
    meeting["segments"].extend({"text": s.text, "start": s.start, "end": s.end, "speaker": s.speaker, "words": []}
                               for s in segments)
    _name_speakers(meeting)


def pid_of(meeting_id: str) -> int | None:
    try:
        pid = int((store.meeting_dir(meeting_id) / "pid").read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None
