"""A cut result note says it was cut and links the whole reply (canopy-web#1955)."""
from __future__ import annotations

from canopy_transcript.result_note import MAX_CHARS, cap, turn_url

BASE = "https://canopy.example.org"
TID = "3f2b8c1e-0000-4000-8000-000000000009"


def test_a_note_that_fits_is_unchanged():
    assert cap("short reply", "https://x/y") == "short reply"
    exact = "a" * MAX_CHARS
    assert cap(exact, "https://x/y") == exact
    assert cap("", "https://x/y") == "" and cap(None) == ""


def test_a_cut_note_ends_with_the_marker_and_link_within_the_cap():
    url = turn_url(BASE, {"id": TID, "agent_slug": "ace", "workspace_slug": "connect"})
    note = cap("word " * 1000, url)
    assert len(note) <= MAX_CHARS
    assert note.endswith(f"… [truncated — full reply: {url}]")
    assert note.startswith("word word")


def test_a_cut_note_with_no_url_still_says_it_was_cut():
    note = cap("x" * 5000)
    assert len(note) <= MAX_CHARS and note.endswith("… [truncated]")


def test_a_cap_smaller_than_the_marker_never_overruns():
    assert len(cap("x" * 50, "https://x/" + "y" * 100, limit=20)) <= 20


def test_the_url_names_the_page_holding_the_turn():
    assert turn_url(BASE, {"id": TID, "agent_slug": "ace", "workspace_slug": "connect"}) == \
        f"{BASE}/w/connect/agents/ace/turns#{TID}"
    assert turn_url(BASE + "/", {"id": TID, "agent_slug": "ace", "workspace_slug": "connect",
                                 "origin_ref": {"chat_session_id": "s-1"}}) == \
        f"{BASE}/w/connect/chat/s-1"
    assert turn_url(BASE, {"id": TID, "project": "p", "workspace_slug": "connect"}) == \
        f"{BASE}/w/connect/activity"
    assert turn_url(BASE, {"id": TID}) == f"{BASE}/activity"
    assert turn_url("", {"id": TID, "agent_slug": "ace", "workspace_slug": "connect"}) == ""


def test_a_cut_keeps_the_runners_reason_the_turn_failed():
    # Failure investigation 21 (turn c4dd7019, 2026-10-30): the reason was cut off.
    reason = ("[runner] out of Claude usage — every Claude credential on this box is "
              "exhausted (tried: a, b, c). This runner has paused itself until 21:02.")
    note = cap("Phase 4 is done. " * 300 + "\n\n" + reason, "https://x/chat/1")
    assert len(note) <= MAX_CHARS
    assert note.startswith("Phase 4 is done.")
    assert "… [truncated — full reply: https://x/chat/1]" in note
    assert note.endswith("\n\n" + reason)


def test_a_runner_reason_too_long_for_half_the_note_is_cut_itself():
    reason = "[runner] " + "why " * 1000
    note = cap("reply " * 1000 + "\n\n" + reason, "u")
    assert len(note) <= MAX_CHARS
    assert note.startswith("reply reply")
    assert "\n\n[runner] why why" in note and note.endswith("…")


def test_a_runner_mention_mid_reply_is_not_a_reason():
    note = cap("see [runner] logs\n\n[runner] x\n\nthen more " + "w " * 2000, "u")
    assert note.endswith("… [truncated — full reply: u]")
