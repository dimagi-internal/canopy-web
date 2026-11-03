"""A turn Escape ended: Claude Code writes a marker and fires no `Stop`."""
import json

import pytest

from canopy_transcript import turn_ended_in_interrupt


def _interrupt_transcript(tmp_path, *records):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


_ASSISTANT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working on it"}]}}
_PROMPT = {"type": "user", "message": {"content": "carry on"}}


def _marker(text="[Request interrupted by user]"):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize("text", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"])
def test_a_turn_ending_in_escape_is_interrupted(tmp_path, text):
    assert turn_ended_in_interrupt(_interrupt_transcript(tmp_path, _ASSISTANT, _marker(text)))


def test_records_written_after_an_escape_are_not_the_conversation_moving_on(tmp_path):
    """Measured: `system/away_summary` landed three minutes after the marker."""
    assert turn_ended_in_interrupt(_interrupt_transcript(
        tmp_path, _ASSISTANT, _marker(),
        {"type": "file-history-snapshot"}, {"type": "system", "subtype": "away_summary"},
    ))


def test_a_new_prompt_after_an_escape_ends_the_interrupt(tmp_path):
    assert not turn_ended_in_interrupt(_interrupt_transcript(tmp_path, _marker(), _PROMPT))


def test_an_api_request_after_an_escape_is_work(tmp_path):
    """The request for the next prompt can land before that prompt's own record."""
    assert not turn_ended_in_interrupt(_interrupt_transcript(tmp_path, _marker(), {"type": "api-request"}))
    assert not turn_ended_in_interrupt(_interrupt_transcript(tmp_path, _marker(), {"type": "api-request-blob"}))


def test_a_subagents_interrupt_does_not_end_the_main_turn(tmp_path):
    assert not turn_ended_in_interrupt(_interrupt_transcript(
        tmp_path, _ASSISTANT, {**_marker(), "isSidechain": True},
    ))


def test_a_tool_result_quoting_the_marker_is_not_an_interrupt(tmp_path):
    """An agent that grepped a transcript for it has not been interrupted."""
    quoting = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "[Request interrupted by user]"},
    ]}}
    assert not turn_ended_in_interrupt(_interrupt_transcript(tmp_path, _ASSISTANT, quoting))


def test_an_unreadable_transcript_is_not_an_interrupt(tmp_path):
    assert not turn_ended_in_interrupt(tmp_path / "absent.jsonl")
