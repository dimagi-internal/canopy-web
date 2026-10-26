"""The ACP reducer's live rows meet the transcript row contract
(`canopy_transcript.contract`), the same bar the transcript and hook producers are
held to (packages/canopy_transcript/tests/test_contract.py)."""
from canopy_acp import UpdateReducer
from canopy_transcript import stream_event
from canopy_transcript.contract import event_as_row, producer_problems, row_problems

from tests.test_updates import BASH_SEQUENCE


def _rows_after(updates) -> list[dict]:
    r = UpdateReducer()
    for u in updates:
        r.apply(u)
    return r.rows_for_tool_call("toolu_01A")


def test_a_running_call_meets_the_contract():
    rows = _rows_after(BASH_SEQUENCE[:2])
    assert [r["role"] for r in rows] == ["tool_use"]
    for row in rows:
        assert producer_problems(row, live=True) == [], row


def test_a_finished_call_meets_the_contract_and_survives_the_wire():
    rows = _rows_after(BASH_SEQUENCE)
    assert [r["role"] for r in rows] == ["tool_use", "tool_result"]
    for row in rows:
        assert producer_problems(row, live=True) == [], row
        assert row_problems(event_as_row(stream_event(row))) == []


def test_a_failed_call_meets_the_contract():
    failed = [*BASH_SEQUENCE[:3], {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_01A",
                                  "status": "failed", "rawOutput": "boom"}]
    rows = _rows_after(failed)
    assert rows[-1]["content"]["is_error"] is True
    for row in rows:
        assert producer_problems(row, live=True) == [], row
