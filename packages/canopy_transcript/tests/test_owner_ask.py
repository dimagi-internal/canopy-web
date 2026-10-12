"""The owner line a caller's session leaves (canopy-web#1416)."""
from __future__ import annotations

import json

from canopy_transcript import owner_ask


def test_the_last_owner_line_is_found_and_normalised():
    text = ("Thanks — I can explain how it works.\n\nThat is for Jonathan, Hal's owner.\n"
            "For Hal's owner: give Vikram read access to the labs dashboard\n")
    assert owner_ask.find(text) == "For Hal's owner: give Vikram read access to the labs dashboard"
    assert owner_ask.find("**For Hal’s owner:** add vkumar@ to callers\n") == \
        "For Hal's owner: add vkumar@ to callers"
    assert owner_ask.find("> For ACE's owner: a\nFor ACE's owner: b") == "For ACE's owner: b"


def test_prose_that_mentions_the_owner_is_not_a_line():
    for text in ("", None, "I'll pass this to Hal's owner.", "For Hal's owner:", "Ask for Hal's owner: x"):
        assert owner_ask.find(text) is None


def _rec(kind, content):
    return {"type": kind, "message": {"role": kind, "content": content}}


def test_the_final_answer_is_the_text_after_the_last_user_record():
    records = [
        _rec("user", "Can Hal help?"),
        _rec("assistant", [{"type": "text", "text": "For Hal's owner: an OLD ask"},
                           {"type": "tool_use", "name": "Read"}]),
        _rec("user", [{"type": "tool_result", "content": "..."}]),
        _rec("assistant", [{"type": "text", "text": "Here is how it works."}]),
        {"type": "system", "subtype": "x"},
        _rec("assistant", [{"type": "text", "text": "For Hal's owner: the new ask"}]),
    ]
    assert owner_ask.final_text(records) == "Here is how it works.\nFor Hal's owner: the new ask"
    assert owner_ask.find(owner_ask.final_text(records)) == "For Hal's owner: the new ask"


def test_a_stop_hook_prefers_its_own_message_then_the_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        _rec("user", "hi"), _rec("assistant", [{"type": "text", "text": "For Hal's owner: from file"}])]))
    assert owner_ask.from_stop_hook({"transcript_path": str(path)}) == "For Hal's owner: from file"
    assert owner_ask.from_stop_hook({"transcript_path": str(path),
                                     "last_assistant_message": "For Hal's owner: from hook"}) == \
        "For Hal's owner: from hook"
    assert owner_ask.from_stop_hook({}) is None
