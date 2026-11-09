"""Turn a growing stream of audio into finished, speaker-labelled segments.

Pure logic: what recognises speech and what tells voices apart are passed in, so this
runs (and is tested) without any model, and any model can be plugged in.

Two ways to decide when words are final, chosen by what the recogniser can say:

  * BY SENTENCE, for a model that times its sentences (Parakeet, Whisper). It re-reads
    everything not yet committed each step, so the end of what it hears can still change;
    a sentence is final once it ended `stable_lag` seconds before the newest audio. Past
    `max_window`, all but the last sentence are committed, so a monologue isn't re-read
    forever.
  * BY PAUSE, for a model that only returns text (Qwen3-ASR, Cohere, Granite). Audio up to
    the newest pause is read once and committed. Someone talking past `max_window`
    without a pause is cut at the quietest moment.

Who spoke is a Labeller's job: voice fingerprints matched as people talk
(EmbeddingLabeller), or a diarization model's own timeline (TimelineLabeller).
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
        # A person first heard too briefly to fingerprint has no voice yet: the first real
        # fingerprint becomes theirs, rather than being compared with nothing.
        for i, c in enumerate(self.centroids):
            if c.shape != v.shape:
                self.centroids[i], self.counts[i] = v, 1
                return i
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


class Labeller:
    """Says who spoke a committed segment. `observe` sees all audio as it arrives."""

    def observe(self, samples: np.ndarray, at: float) -> None:  # noqa: B027 — optional hook
        pass

    def label(self, start: float, end: float, clip: np.ndarray, previous: int) -> int:
        return 0


class EmbeddingLabeller(Labeller):
    """A voice fingerprint per segment, matched against the voices heard so far."""

    def __init__(self, embed: Embed, speakers: Speakers | None = None):
        self.embed, self.speakers = embed, speakers or Speakers()

    def label(self, start, end, clip, previous):
        if len(clip) > SAMPLE_RATE // 2:
            return self.speakers.assign(self.embed(clip), end - start, previous)
        return previous if self.speakers.centroids else self.speakers.assign(None, 0.0)


class TimelineLabeller(Labeller):
    """A diarization model's own timeline of who is speaking when; a segment goes to whoever
    spoke most of it. `feed(samples) -> [(start, end, speaker)]` in meeting time."""

    def __init__(self, feed: Callable[[np.ndarray], list]):
        self.feed, self.turns = feed, []

    def observe(self, samples, at):
        self.turns.extend(self.feed(samples))

    def label(self, start, end, clip, previous):
        overlap: dict[int, float] = {}
        for a, b, who in self.turns:
            o = min(b, end) - max(a, start)
            if o > 0:
                overlap[who] = overlap.get(who, 0.0) + o
        return max(overlap, key=overlap.get) if overlap else previous


class LiveTranscriber:
    def __init__(self, recognise: Recognise, embed: Embed | None = None, *, labeller: Labeller | None = None,
                 timed: bool = True, stable_lag: float = 1.2, max_window: float | None = None,
                 silence_rms: float = 0.004, min_pause: float = 0.45, speakers: Speakers | None = None):
        self.recognise, self.timed = recognise, timed
        if labeller is None:
            labeller = EmbeddingLabeller(embed, speakers) if embed is not None else Labeller()
        self.labeller = labeller
        self.stable_lag, self.silence_rms, self.min_pause = stable_lag, silence_rms, min_pause
        self.max_window = max_window if max_window is not None else (24.0 if timed else 15.0)
        self.buffer = np.zeros(0, dtype=np.float32)
        self.base = 0.0  # meeting time, in seconds, of buffer[0]
        self.received = 0  # samples seen in all
        self.last_speaker = 0

    # Kept for callers and tests that set them directly.
    @property
    def embed(self):
        return getattr(self.labeller, "embed", None)

    @embed.setter
    def embed(self, fn):
        self.labeller = EmbeddingLabeller(fn)

    @property
    def speakers(self) -> Speakers | None:
        return getattr(self.labeller, "speakers", None)

    @property
    def heard(self) -> float:
        return self.base + len(self.buffer) / SAMPLE_RATE

    def feed(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        self.labeller.observe(samples, self.received / SAMPLE_RATE)
        self.received += len(samples)
        self.buffer = np.concatenate([self.buffer, samples])

    def step(self, *, final: bool = False) -> list[Segment]:
        """The segments that are now final, in order. `final` commits everything left."""
        window = len(self.buffer) / SAMPLE_RATE
        if window < 0.5 and not final:
            return []
        if not final and self._quiet(self.buffer):
            if window > 3.0:  # nothing said: let go of all but the newest moment
                self._drop(window - 0.5)
            return []
        return self._by_sentence(window, final) if self.timed else self._by_pause(window, final)

    # --- by sentence -------------------------------------------------------------------

    def _by_sentence(self, window: float, final: bool) -> list[Segment]:
        sentences = [s for s in self.recognise(self.buffer) if s.text.strip()] if len(self.buffer) else []
        if final:
            ready = sentences
        else:
            ready = [s for s in sentences if s.end <= window - self.stable_lag]
            if not ready and sentences and window >= self.max_window:
                ready = sentences[:-1] or sentences
        out = [self._segment(s) for s in ready]
        if ready:
            self._drop(window if final else ready[-1].end)
        elif final:
            self._drop(window)
        return out

    # --- by pause ----------------------------------------------------------------------

    def _by_pause(self, window: float, final: bool) -> list[Segment]:
        cut = window if final else self._last_pause()
        if cut is None and window >= self.max_window:
            cut = self._quietest(window)
        if not cut:
            return []
        chunk = self.buffer[: int(cut * SAMPLE_RATE)]
        out = []
        if not self._quiet(chunk):
            sentences = [s for s in self.recognise(chunk) if s.text.strip()]
            out = [self._segment(s) for s in sentences]
        self._drop(cut)
        return out

    def _frames(self, audio: np.ndarray, seconds: float = 0.05) -> np.ndarray:
        frame = int(SAMPLE_RATE * seconds)
        n = len(audio) // frame
        if n == 0:
            return np.zeros(0)
        return np.sqrt(np.mean(np.square(audio[: n * frame].reshape(n, frame)), axis=1))

    def _threshold(self, rms: np.ndarray) -> float:
        # Above the room's own noise, whatever the microphone.
        return max(self.silence_rms, 3.0 * float(np.percentile(rms, 10))) if len(rms) else self.silence_rms

    def _last_pause(self) -> float | None:
        """Where to cut: inside the newest pause of at least `min_pause` that has speech before
        it, or None."""
        rms = self._frames(self.buffer)
        if len(rms) == 0:
            return None
        loud = rms >= self._threshold(rms)
        need = int(self.min_pause / 0.05)
        run, best = 0, None
        for i, is_loud in enumerate(loud):
            run = 0 if is_loud else run + 1
            if run >= need and loud[: i - run + 1].any():
                best = (i - run + 1) * 0.05 + min(0.2, run * 0.05 / 2)
        return best

    def _quietest(self, window: float) -> float:
        rms = self._frames(self.buffer, 0.1)
        lo = int(min(window * 0.5, window - 1.0) / 0.1)
        hi = max(lo + 1, len(rms) - 10)
        return (lo + int(np.argmin(rms[lo:hi]))) * 0.1 + 0.05

    # --- shared ------------------------------------------------------------------------

    def _segment(self, s: Sentence) -> Segment:
        a, b = int(s.start * SAMPLE_RATE), int(s.end * SAMPLE_RATE)
        start, end = round(self.base + s.start, 2), round(self.base + s.end, 2)
        speaker = self.labeller.label(start, end, self.buffer[a:b], self.last_speaker)
        self.last_speaker = speaker
        return Segment(text=s.text.strip(), start=start, end=end, speaker=speaker)

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
