import json

from canopy_transcript.api_error import api_error_at_transcript_end, api_error_in_record


def _rec(text, **over):
    return {"type": "assistant", "uuid": "u1", "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": text}]}, **over}


def test_a_generic_api_error_record_is_recognised():
    assert api_error_in_record(_rec("API Error: Your computer went to sleep")) \
        == "API Error: Your computer went to sleep"


def test_the_flag_is_required_not_the_text():
    assert api_error_in_record(_rec("API Error: 500", isApiErrorMessage=False)) is None


def test_caps_and_signed_out_are_not_generic():
    assert api_error_in_record(_rec("You've hit your session limit", error="rate_limit")) is None
    assert api_error_in_record(_rec("Not logged in · Please run /login")) is None


def test_only_the_transcript_tail_counts(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(_rec("API Error: boom")) + "\n"
                 + json.dumps({"type": "assistant", "uuid": "u2",
                               "message": {"content": [{"type": "text", "text": "fine now"}]}}) + "\n")
    assert api_error_at_transcript_end(p) is None
    p.write_text(json.dumps(_rec("API Error: boom")) + "\n"
                 + json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    assert api_error_at_transcript_end(p) == ("u1", "API Error: boom")
