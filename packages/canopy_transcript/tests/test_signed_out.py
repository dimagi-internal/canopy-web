import json

from canopy_transcript import rows_for_record
from canopy_transcript.signed_out import (
    answered_at_transcript_end,
    signed_out_at_transcript_end,
    signed_out_in_record,
)

#: What Claude Code writes when the box has no sign-in: a synthetic assistant
#: record flagged as an API error (acedimagi-mbp-cdp, 2026-10-22).
SIGNED_OUT = {
    "type": "assistant", "uuid": "u-1", "isApiErrorMessage": True,
    "message": {"model": "<synthetic>", "role": "assistant",
                "content": [{"type": "text", "text": "Not logged in · Please run /login"}]},
}
REPLY = {
    "type": "assistant", "uuid": "u-2",
    "message": {"model": "claude-opus-5-5", "role": "assistant",
                "content": [{"type": "text", "text": "Yes, I'm here."}]},
}
PROMPT = {"type": "user", "uuid": "u-0", "message": {"role": "user", "content": "you there?"}}


def _transcript(tmp_path, *records):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(p)


def test_the_cli_notice_is_recognised():
    assert signed_out_in_record(SIGNED_OUT) == "Not logged in · Please run /login"


def test_an_agent_writing_about_login_is_not():
    """Text alone never counts: only Claude Code's own API-error record."""
    said = {**REPLY, "message": {**REPLY["message"],
            "content": [{"type": "text", "text": "Not logged in? Please run /login first."}]}}
    assert signed_out_in_record(said) is None


def test_a_cap_is_an_api_error_but_not_signed_out():
    cap = {**SIGNED_OUT, "error": "rate_limit", "message": {**SIGNED_OUT["message"],
           "content": [{"type": "text", "text": "You've hit your session limit · resets 2:30am"}]}}
    assert signed_out_in_record(cap) is None


def test_transcript_end(tmp_path):
    path = _transcript(tmp_path, PROMPT, SIGNED_OUT)
    assert signed_out_at_transcript_end(path) == ("u-1", "Not logged in · Please run /login")
    assert not answered_at_transcript_end(path)
    path = _transcript(tmp_path, PROMPT, SIGNED_OUT, PROMPT, REPLY)
    assert signed_out_at_transcript_end(path) is None
    assert answered_at_transcript_end(path)


def test_the_row_says_it_is_an_error_not_a_reply():
    [row] = rows_for_record(SIGNED_OUT)
    assert row["role"] == "assistant"
    assert row["content"] == {"api_error": "api_error", "signed_out": True}
    [reply] = rows_for_record(REPLY)
    assert reply["content"] == {}
