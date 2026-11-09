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

STEP_SECONDS = 2.0
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


def _speakers(segments: list) -> list:
    numbers = sorted({s["speaker"] for s in segments})
    return [{"name": f"Speaker {n + 1}", "number": n} for n in numbers]


def _transcript(meeting: dict) -> str:
    names = {s["number"]: s["name"] for s in meeting["speakers"]}
    return "\n".join(f"{names.get(s['speaker'], 'Speaker')}: {s['text']}" for s in meeting["segments"])


def record(meeting_id: str, source, *, recognise=None, embed=None, keep_audio: bool = True) -> int:
    meeting = store.load(meeting_id)
    if meeting is None:
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    (store.meeting_dir(meeting_id) / "pid").write_text(str(os.getpid()))
    store.set_active(meeting_id)
    writer = None
    try:
        meeting["state"] = "loading"
        store.save(meeting)
        if recognise is None:
            from .models import Recogniser, VoiceEmbedder

            recognise, embed = Recogniser(), VoiceEmbedder()
        live = LiveTranscriber(recognise, embed)
        if keep_audio:
            import soundfile as sf

            writer = sf.SoundFile(str(store.meeting_dir(meeting_id) / "audio.wav"), "w", SAMPLE_RATE, 1, "PCM_16")
        source.start()
        meeting["state"] = "recording"
        store.save(meeting)
        log.info("recording %s from %s", meeting_id, source.name)
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
            if time.monotonic() >= next_step or ended:
                _add(meeting, live.step())
                meeting["heard_seconds"] = round(live.heard, 1)
                store.save(meeting)
                next_step = time.monotonic() + STEP_SECONDS
            if ended:
                break
        source.stop()
        while not source.chunks.empty():
            live.feed(source.chunks.get_nowait())
        _add(meeting, live.step(final=True))
        meeting["heard_seconds"] = round(live.heard, 1)
        meeting["state"] = "ended"
        meeting["transcript"] = _transcript(meeting)
        meeting["summary"] = meeting["summary"] or "Recording ended."
        store.save(meeting)
        log.info("ended %s: %d segments, %d speakers, %.0fs", meeting_id, len(meeting["segments"]),
                 len(meeting["speakers"]), live.heard)
        return 0
    except Exception as exc:  # the reason, never the audio or words
        meeting["state"] = "failed"
        meeting["error"] = type(exc).__name__
        meeting["summary"] = meeting["summary"] or "Recording failed."
        store.save(meeting)
        log.exception("recorder failed for %s", meeting_id)
        return 1
    finally:
        if writer is not None:
            writer.close()
        if store.active() == meeting_id:
            store.set_active(None)
        (store.meeting_dir(meeting_id) / "pid").unlink(missing_ok=True)


def _add(meeting: dict, segments: list) -> None:
    if not segments:
        return
    meeting["segments"].extend({"text": s.text, "start": s.start, "end": s.end, "speaker": s.speaker, "words": []}
                               for s in segments)
    meeting["speakers"] = _speakers(meeting["segments"])


def pid_of(meeting_id: str) -> int | None:
    try:
        pid = int((store.meeting_dir(meeting_id) / "pid").read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None
