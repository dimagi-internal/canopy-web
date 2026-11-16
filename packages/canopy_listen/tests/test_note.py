"""Reading a voice note: long notes are read in pieces cut where it's quiet."""
import numpy as np
from canopy_listen import note
from canopy_listen.live import SAMPLE_RATE, Sentence


def test_a_short_note_is_one_piece():
    audio = np.ones(SAMPLE_RATE * 5, dtype=np.float32)
    assert [len(p) for p in note.pieces(audio)] == [len(audio)]


def test_a_long_note_is_cut_at_its_quiet_moments_and_nothing_is_lost():
    audio = np.full(SAMPLE_RATE * 150, 0.5, dtype=np.float32)
    quiet = SAMPLE_RATE * 55  # a pause five seconds before the first limit
    audio[quiet:quiet + SAMPLE_RATE // 5] = 0.0
    pieces = note.pieces(audio)
    assert sum(len(p) for p in pieces) == len(audio)
    assert all(len(p) <= SAMPLE_RATE * note.CHUNK_SECONDS for p in pieces)
    assert quiet <= len(pieces[0]) <= quiet + SAMPLE_RATE // 5


def test_read_joins_what_each_piece_says():
    heard = []

    def recognise(audio):
        heard.append(len(audio))
        return [Sentence(" Call   Sam ", 0, 1), Sentence("", 1, 2), Sentence("tomorrow.", 2, 3)]

    pcm = (np.full(SAMPLE_RATE * 70, 8000, dtype="<i2")).tobytes() + b"\x01"  # an odd byte is dropped
    assert note.read(pcm, recognise) == "Call Sam tomorrow. Call Sam tomorrow."
    assert len(heard) == 2
