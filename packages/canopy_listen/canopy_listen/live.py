"""Turn a growing stream of audio into finished, speaker-labelled segments.

Pure logic: what recognises speech and what tells voices apart are passed in, so this
runs (and is tested) without any model.

  * COMMIT ONLY WHAT WON'T CHANGE. The recogniser re-reads everything not yet committed
    each step, so the end of what it hears can still change as more audio arrives. A
    sentence is final once it ended `stable_lag` seconds before the newest audio.
  * A WINDOW NEVER GROWS FOREVER. Someone talking without a pause would otherwise be
    re-read in full every step. Past `max_window`, all but the last sentence are
    committed, and if there is only one, it is committed anyway.
  * SPEAKERS ARE LEARNED AS THEY TALK. Each committed segment's voice is compared with
    the voices heard so far; a long enough segment unlike all of them is a new person.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 16000


@dataclass
class Sentence:
    text: str
    start: float  # seconds, relative to the audio handed to the recogniser
    end: float


@dataclass
class Segment:
    text: str
    start: float  # seconds from the start of the meeting
    end: float
    speaker: int


Recognise = Callable[[np.ndarray], list]  # audio -> [Sentence]
Embed = Callable[[np.ndarray], np.ndarray]  # audio -> voice vector


@dataclass
class Speakers:
    """Voices heard so far, as running means of unit vectors."""

    # TitaNet-small cosine. Measured on a three-voice test: same voice 0.61-0.95 (mean
    # 0.81), two similar voices up to 0.67 (mean 0.56); 0.55 merged them, 0.62-0.74 didn't.
    same_voice: float = 0.66
    min_new_seconds: float = 1.5
    centroids: list = field(default_factory=list)
    counts: list = field(default_factory=list)

    def assign(self, vector: np.ndarray | None, seconds: float, previous: int = 0) -> int:
        if vector is None or not np.all(np.isfinite(vector)) or not np.any(vector):
            return previous if self.centroids else self._new(None)
        v = vector / np.linalg.norm(vector)
        if not self.centroids:
            return self._new(v)
        sims = [float(np.dot(v, c)) for c in self.centroids]
        best = int(np.argmax(sims))
        if sims[best] >= self.same_voice or seconds < self.min_new_seconds:
            # Short audio carries little voice: join the closest rather than invent someone.
            self._update(best, v)
            return best
        return self._new(v)

    def _new(self, v) -> int:
        self.centroids.append(v if v is not None else np.zeros(1))
        self.counts.append(1)
        return len(self.centroids) - 1

    def _update(self, i: int, v: np.ndarray) -> None:
        if self.centroids[i].shape != v.shape:
            self.centroids[i], self.counts[i] = v, 1
            return
        n = self.counts[i]
        mean = (self.centroids[i] * n + v) / (n + 1)
        self.centroids[i] = mean / np.linalg.norm(mean)
        self.counts[i] = n + 1


class LiveTranscriber:
    def __init__(self, recognise: Recognise, embed: Embed | None = None, *, stable_lag: float = 1.2,
                 max_window: float = 24.0, silence_rms: float = 0.004, speakers: Speakers | None = None):
        self.recognise, self.embed = recognise, embed
        self.stable_lag, self.max_window, self.silence_rms = stable_lag, max_window, silence_rms
        self.speakers = speakers or Speakers()
        self.buffer = np.zeros(0, dtype=np.float32)
        self.base = 0.0  # meeting time, in seconds, of buffer[0]
        self.last_speaker = 0

    @property
    def heard(self) -> float:
        return self.base + len(self.buffer) / SAMPLE_RATE

    def feed(self, samples: np.ndarray) -> None:
        self.buffer = np.concatenate([self.buffer, np.asarray(samples, dtype=np.float32).reshape(-1)])

    def step(self, *, final: bool = False) -> list[Segment]:
        """The segments that are now final, in order. `final` commits everything left."""
        window = len(self.buffer) / SAMPLE_RATE
        if window < 0.5 and not final:
            return []
        if not final and self._quiet(self.buffer):
            if window > 3.0:  # nothing said: let go of all but the newest moment
                self._drop(window - 0.5)
            return []
        sentences = [s for s in self.recognise(self.buffer) if s.text.strip()] if len(self.buffer) else []
        if final:
            ready = sentences
        else:
            ready = [s for s in sentences if s.end <= window - self.stable_lag]
            if not ready and sentences and window >= self.max_window:
                ready = sentences[:-1] or sentences
        out = []
        for s in ready:
            out.append(self._segment(s))
        if ready:
            self._drop(window if final else ready[-1].end)
        elif final:
            self._drop(window)
        return out

    def _segment(self, s: Sentence) -> Segment:
        a, b = int(s.start * SAMPLE_RATE), int(s.end * SAMPLE_RATE)
        speaker = self.last_speaker
        if self.embed is not None and b - a > SAMPLE_RATE // 2:
            speaker = self.speakers.assign(self.embed(self.buffer[a:b]), s.end - s.start, self.last_speaker)
        elif not self.speakers.centroids:
            speaker = self.speakers.assign(None, 0.0)
        self.last_speaker = speaker
        return Segment(text=s.text.strip(), start=round(self.base + s.start, 2), end=round(self.base + s.end, 2),
                       speaker=speaker)

    def _drop(self, seconds: float) -> None:
        n = min(len(self.buffer), max(0, int(seconds * SAMPLE_RATE)))
        self.buffer = self.buffer[n:]
        self.base += n / SAMPLE_RATE

    def _quiet(self, audio: np.ndarray) -> bool:
        """No quarter second of it is loud enough to be speech (a whole-window average would
        let a long silence hide a short sentence)."""
        frame = SAMPLE_RATE // 4
        n = len(audio) // frame
        if n == 0:
            return True
        frames = audio[: n * frame].reshape(n, frame)
        return float(np.sqrt(np.mean(np.square(frames), axis=1)).max()) < self.silence_rms
