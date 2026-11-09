"""The live transcriber decides when words are final and whose they are; these pin both
with a fake recogniser, so they need no model and run anywhere."""
import numpy as np
from canopy_listen.live import SAMPLE_RATE, LiveTranscriber, Sentence, Speakers

TONE = 0.1  # loud enough to count as speech


def audio(seconds, level=TONE):
    return np.full(int(seconds * SAMPLE_RATE), level, dtype=np.float32)


class Script:
    """Pretends to recognise: says what was 'spoken' at each meeting time, relative to the
    window it is handed, like the real model."""

    def __init__(self, lines, transcriber_ref):
        self.lines, self.ref = lines, transcriber_ref  # [(start, end, text)] in meeting time

    def __call__(self, buffer):
        t = self.ref[0]
        base, heard = t.base, t.base + len(buffer) / SAMPLE_RATE
        out = []
        for start, end, text in self.lines:
            if start >= base - 0.01 and start < heard:
                out.append(Sentence(text, start - base, min(end, heard) - base))
        return out


def make(lines, **kw):
    ref = []
    t = LiveTranscriber(Script(lines, ref), **kw)
    ref.append(t)
    return t


def test_a_sentence_is_final_only_once_newer_audio_follows_it():
    t = make([(0.0, 2.0, "hello there"), (2.5, 4.0, "second")])
    t.feed(audio(2.6))
    assert t.step() == []  # "hello there" ended 0.6s before the newest audio
    t.feed(audio(1.0))
    first = t.step()
    assert [s.text for s in first] == ["hello there"]
    assert first[0].start == 0.0 and first[0].end == 2.0
    t.feed(audio(2.0))
    assert [s.text for s in t.step()] == ["second"]


def test_each_sentence_is_emitted_once_and_final_flushes_the_rest():
    t = make([(0.0, 1.0, "one"), (1.5, 3.0, "two"), (3.2, 3.9, "three")])
    seen = []
    for _ in range(8):
        t.feed(audio(0.5))
        seen += t.step()
    seen += t.step(final=True)
    assert [s.text for s in seen] == ["one", "two", "three"]
    assert t.step(final=True) == []


def test_a_long_unbroken_stretch_is_committed_rather_re_read_forever():
    t = make([(0.0, 30.0, "a very long monologue")], max_window=24.0)
    for _ in range(26):
        t.feed(audio(1.0))
        out = t.step()
        if out:
            break
    assert [s.text for s in out] == ["a very long monologue"]
    assert len(t.buffer) / SAMPLE_RATE < 3


def test_silence_is_let_go_without_asking_the_recogniser():
    calls = []
    t = LiveTranscriber(lambda b: calls.append(len(b)) or [])
    t.feed(audio(5.0, level=0.0))
    assert t.step() == [] and calls == []
    assert len(t.buffer) / SAMPLE_RATE <= 0.51 and abs(t.heard - 5.0) < 0.01


def test_a_short_sentence_after_long_silence_is_still_heard():
    t = make([(9.5, 10.5, "yes")])
    t.feed(np.concatenate([audio(9.5, level=0.0), audio(1.0), audio(1.5, level=0.0)]))
    out = t.step()
    assert [(s.text, s.start) for s in out] == [("yes", 9.5)]


def test_voices_are_told_apart_and_short_audio_never_invents_a_person():
    a, b = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
    sp = Speakers()
    assert sp.assign(a, 3.0) == 0
    assert sp.assign(b, 3.0) == 1
    assert sp.assign(a + 0.1, 2.0) == 0
    assert sp.assign(np.array([0.0, 0.0, 1.0]), 0.8, previous=1) in (0, 1)  # too short to be new
    assert len(sp.centroids) == 2


def test_segments_carry_meeting_time_and_speaker():
    voices = {0: np.array([1.0, 0.0]), 1: np.array([0.0, 1.0])}
    t = make([(0.0, 2.0, "from alice"), (2.2, 4.5, "from bob")])
    t.embed = lambda clip: voices[0] if t.base < 1 else voices[1]
    t.feed(audio(3.5))
    first = t.step()
    t.feed(audio(3.0))
    second = t.step()
    assert [(s.text, s.speaker) for s in first + second] == [("from alice", 0), ("from bob", 1)]
    assert second[0].start == 2.2
