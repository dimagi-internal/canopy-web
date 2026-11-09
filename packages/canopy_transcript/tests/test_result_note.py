"""A cut result note says it was cut and links the whole reply."""
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
        f"{BASE}/w/connect/sessions/s-1"
    assert turn_url(BASE, {"id": TID, "project": "p", "workspace_slug": "connect"}) == \
        f"{BASE}/w/connect/activity"
    assert turn_url(BASE, {"id": TID}) == f"{BASE}/activity"
    assert turn_url("", {"id": TID, "agent_slug": "ace", "workspace_slug": "connect"}) == ""


def test_a_box_on_the_old_address_still_links_the_canonical_session_page():
    chat = {"id": TID, "agent_slug": "trellis", "workspace_slug": "dimagi",
            "origin_ref": {"chat_session_id": "s-1"}}
    want = "https://canopy.dimagi.com/w/dimagi/sessions/s-1"
    for old in ("https://labs.connect.dimagi.com/canopy", "https://labs.connect.dimagi.com/canopy/",
                "https://labs.connect.dimagi.com"):
        assert turn_url(old, chat) == want
    assert turn_url("https://canopy.dimagi.com", {"id": TID}) == "https://canopy.dimagi.com/activity"
    # A dev or staging box keeps its own address.
    assert turn_url("http://localhost:8000", chat) == "http://localhost:8000/w/dimagi/sessions/s-1"


def test_only_the_former_addresses_themselves_become_canonical():
    from canopy_transcript.result_note import public_base
    canonical = "https://canopy.dimagi.com"
    assert public_base("HTTPS://Labs.Connect.Dimagi.com/canopy/") == canonical
    assert public_base("  https://labs.connect.dimagi.com  ") == canonical
    # A look-alike, a userinfo trick, another path, another scheme, a port: all kept as given.
    for other in ("https://labs.connect.dimagi.com.evil.com", "https://labs.connect.dimagi.com.evil.com/canopy",
                  "https://labs.connect.dimagi.com@evil.com", "https://evil.com/labs.connect.dimagi.com/canopy",
                  "https://labs.connect.dimagi.com/canopy-staging", "https://labs.connect.dimagi.com:8443/canopy",
                  "http://labs.connect.dimagi.com/canopy", "https://canopy.example.org", "http://localhost:8000"):
        assert public_base(other) == other
    assert public_base("") == "" and public_base(None) == ""


def test_a_cut_keeps_the_runners_reason_the_turn_failed():
    # The reason must not be cut off.
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
