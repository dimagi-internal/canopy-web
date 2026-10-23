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
