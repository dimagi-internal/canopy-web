"""A voice note, read once: int16 PCM at 16 kHz in, its words out.

Pure logic: the recogniser is passed in, so this runs (and is tested) without a model.
A long note is read in pieces of at most CHUNK_SECONDS, each cut at its quietest moment
near the end, so no word is split and no model is handed ten minutes at once.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SECONDS = 60.0
#: Where to look for the quietest moment: the last stretch of a piece this long.
SEARCH_SECONDS = 10.0
FRAME = SAMPLE_RATE // 10


def pieces(audio: np.ndarray, chunk_seconds: float = CHUNK_SECONDS) -> list[np.ndarray]:
    limit = int(chunk_seconds * SAMPLE_RATE)
    search = int(min(SEARCH_SECONDS, chunk_seconds / 2) * SAMPLE_RATE)
    out, start = [], 0
    while len(audio) - start > limit:
        window = audio[start + limit - search: start + limit]
        frames = len(window) // FRAME
        loudness = np.sqrt(np.mean(np.square(window[: frames * FRAME].reshape(frames, FRAME)), axis=1))
        cut = start + limit - search + int(np.argmin(loudness)) * FRAME + FRAME // 2
        out.append(audio[start:cut])
        start = cut
    out.append(audio[start:])
    return [p for p in out if len(p)]


def read(pcm: bytes, recognise) -> str:
    """The note's words, as one paragraph. `recognise(audio) -> [Sentence]`."""
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    words = []
    for piece in pieces(audio):
        for sentence in recognise(piece):
            text = " ".join(str(sentence.text).split())
            if text:
                words.append(text)
    return " ".join(words)
