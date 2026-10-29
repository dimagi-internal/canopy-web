"""The closeout verdict a turn ended on is read off its final message, and survives a cut note."""
from __future__ import annotations

from canopy_transcript.closeout import find
from canopy_transcript.result_note import MAX_CHARS, cap

LINE = "⏸ Session paused — waiting on you for: @jjackson: run `aws sso login`. [closeout: NEEDS_YOU]"


def test_finds_the_verdict_and_its_line():
    assert find(f"Did things.\n\n{LINE}") == ("NEEDS_YOU", LINE)


def test_the_last_token_wins_and_none_is_blank():
    text = ("✅ Session complete — safe to close. [closeout: CLOSED]\nlater\n"
            "⚠️ Session incomplete — x. [closeout: INCOMPLETE]")
    assert find(text)[0] == "INCOMPLETE"
    assert find("✅ Session complete — safe to close.") == ("", "")
    assert find(None) == ("", "") and find("") == ("", "")


def test_an_unknown_verdict_is_not_one():
    assert find("[closeout: DONE]") == ("", "")


def test_a_cut_note_keeps_the_closeout_line():
    note = cap("word " * 1000 + "\n\n" + LINE, "https://x/turns#1")
    assert len(note) <= MAX_CHARS
    assert note.endswith(LINE) and "[truncated — full reply: https://x/turns#1]" in note
    assert find(note) == ("NEEDS_YOU", LINE)


def test_a_cut_note_without_a_closeout_line_is_cut_as_before():
    assert cap("word " * 1000, "u").endswith("… [truncated — full reply: u]")


def test_a_token_mid_reply_is_not_moved():
    note = cap(LINE + "\n" + "word " * 1000, "u")
    assert note.endswith("… [truncated — full reply: u]")
