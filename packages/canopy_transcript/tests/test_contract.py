"""Every producer of transcript rows meets the row contract (`canopy_transcript.contract`).

The server stores rows without knowing which agent CLI made them, so the contract is
the only thing a second CLI's mapper has to get right. Each producer is a fixture
below; every test runs against every one of them.

`fixtures/claude_code_records.jsonl` is one record of each shape seen in two real
Claude Code transcripts (2026-10), every free-text value redacted: conversational
and non-conversational records alike, since a record that yields no row still has
to consume its ordinal.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from canopy_transcript import (
    BLOCK_STRIDE,
    conversational_messages,
    events_for_hook,
    read_records,
    rows_for_hook,
    stream_event,
)
from canopy_transcript.contract import (
    NO_ORDINAL,
    ORDINAL_MAX,
    ROLES,
    event_as_row,
    producer_problems,
    row_problems,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_records.jsonl"


def _claude_recorded() -> list[dict]:
    return conversational_messages(read_records(FIXTURE), -1)


def _asst(*blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks),
                                             "stop_reason": "tool_use"}, **extra}


def _user(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


_LINK = {"type": "resource_link", "uri": "labs-result://abc", "name": "Visits",
         "mimeType": "application/json"}

#: The shapes the recorded fixture does not happen to hold, from test_rows.py's cases.
EDGE_RECORDS = [
    _user("a typed prompt", promptSource="typed", turnOrigin="human"),
    _asst({"type": "thinking", "thinking": "…"},
          {"type": "text", "text": "two calls at once"},
          {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {"file_path": "/x"}},
          {"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {"command": "ls\x00"}}),
    _user([{"type": "tool_result", "tool_use_id": "toolu_A",
            "content": [{"type": "text", "text": "body"}, _LINK]},
           {"type": "tool_result", "tool_use_id": "toolu_B", "is_error": True,
            "content": [{"type": "image", "source": {}}]}]),
    _user("a skill body", isMeta=True, sourceToolUseID="toolu_S"),
    _user("the summary", isCompactSummary=True),
    {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                          "origin": {"kind": "human"}, "prompt": "mid-turn"}},
    _asst({"type": "text", "text": "Not logged in · Please run /login"},
          isApiErrorMessage=True, error="authentication_failed"),
    {"type": "assistant", "message": {
        "role": "assistant", "model": "claude-opus-5-5", "id": "msg_1", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90_000,
                  "cache_creation_input_tokens": 5, "output_tokens": 3}}},
    # More blocks than the stride holds: the last slot becomes the overflow marker.
    _asst(*({"type": "text", "text": f"b{i}"} for i in range(BLOCK_STRIDE + 3))),
]


def _claude_edge_cases() -> list[dict]:
    return conversational_messages(EDGE_RECORDS, -1)


# Producers of DURABLE rows: what reaches `persist_transcript_rows` and becomes history.
DURABLE_PRODUCERS = [
    pytest.param(_claude_recorded, id="claude-code-jsonl-recorded"),
    pytest.param(_claude_edge_cases, id="claude-code-jsonl-edge-cases"),
    # THE HOOK FOR THE NEXT CLI. Codex's mapper (`canopy_transcript.codex_rows`, spec
    # docs/superpowers/specs/2026-10-26-multi-cli-agents-design.md §5) replaces this
    # skip with a callable returning its rows for a recorded `thread/items/list`
    # fixture, and must pass every test below unchanged.
    pytest.param(None, id="codex-app-server-items",
                 marks=pytest.mark.skip(reason="no Codex mapper yet")),
]


def _hook_rows() -> list[dict]:
    base = {"tool_use_id": "toolu_H", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    return [
        *rows_for_hook({**base, "hook_event_name": "PreToolUse"}),
        *rows_for_hook({**base, "hook_event_name": "PostToolUse",
                        "tool_response": {"stdout": "a\nb", "stderr": ""}}),
        *rows_for_hook({**base, "hook_event_name": "PostToolUseFailure",
                        "tool_response": "boom"}),
    ]


# Producers of LIVE overlay rows: shown to watchers, never persisted. The ACP
# reducer's conformance lives with it (runner/canopy_acp/tests/test_contract_conformance.py),
# since this package cannot import that one.
LIVE_PRODUCERS = [
    pytest.param(_hook_rows, id="claude-code-hooks"),
]


@pytest.mark.parametrize("produce", DURABLE_PRODUCERS)
def test_every_durable_row_meets_the_contract(produce):
    rows = produce()
    assert rows, "a producer fixture that yields nothing proves nothing"
    for row in rows:
        assert producer_problems(row) == [], row


@pytest.mark.parametrize("produce", DURABLE_PRODUCERS)
def test_ordinals_are_unique_increasing_and_storable(produce):
    indices = [row["index"] for row in produce()]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    assert all(0 <= i <= ORDINAL_MAX for i in indices)


@pytest.mark.parametrize("produce", DURABLE_PRODUCERS)
def test_rows_come_from_position_alone(produce):
    """Read twice, the same rows under the same ordinals: what makes a re-ship a no-op."""
    assert produce() == produce()


@pytest.mark.parametrize("produce", DURABLE_PRODUCERS)
def test_a_row_survives_the_stream_wire(produce):
    """`stream_event` (the session-stream envelope) carries a row the server reads back
    as itself (`event_as_row`, the mapping its ingest applies)."""
    for row in produce():
        back = event_as_row(stream_event(row))
        assert row_problems(back) == [], back
        assert (back["index"], back["role"], back["text"]) == (row["index"], row["role"], row["text"])
        assert {k: v for k, v in back["content"].items() if k != "text"} == row["content"]


@pytest.mark.parametrize("produce", LIVE_PRODUCERS)
def test_every_live_row_meets_the_contract(produce):
    rows = produce()
    assert rows
    for row in rows:
        assert producer_problems(row, live=True) == [], row


def test_hook_events_on_the_wire_meet_the_contract():
    payload = {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_H", "tool_name": "Bash",
               "tool_input": {}, "tool_response": {"stdout": "x"}}
    events = events_for_hook(payload)
    assert events
    for event in events:
        assert row_problems(event_as_row(event)) == []


def test_the_edge_cases_cover_every_role_a_runner_ships():
    """`system` is a stored role no producer emits; every other one must be exercised."""
    seen = {r["role"] for r in _claude_edge_cases()}
    assert seen == ROLES - {"system"}


# --- the server's bar: tolerant of every runner in the field, strict on the rest ----

@pytest.mark.parametrize("row", [
    {"index": 5, "role": "user", "text": "q1"},                    # no content: an old shape
    {"index": NO_ORDINAL, "role": "assistant", "text": "a"},      # an old runner, no ordinal
    {"index": None, "role": "assistant", "text": "a"},
    {"index": 3, "role": "tool_use", "text": "", "content": {}},  # fields absent, not mistyped
    {"index": 3, "role": "tool_result", "text": "x", "content": {"text": "x", "client_id": "c"}},
    {"index": 0, "role": "system", "text": "note"},
], ids=lambda r: f"{r['role']}@{r['index']}")
def test_the_server_accepts_what_old_runners_ship(row):
    assert row_problems(row) == []


@pytest.mark.parametrize("row", [
    "not an object",
    {"index": 1, "role": "tool_start", "text": ""},
    {"index": 1, "role": None, "text": ""},
    {"index": "1", "role": "user", "text": ""},
    {"index": True, "role": "user", "text": ""},
    {"index": ORDINAL_MAX + 1, "role": "user", "text": "past the column"},
    {"index": 1, "role": "user", "text": 7},
    {"index": 1, "role": "user", "text": "", "content": ["a list"]},
    {"index": 1, "role": "user", "text": "", "content": {"record": "typed"}},
    {"index": 1, "role": "assistant", "text": "", "content": {"usage": {"context_tokens": "9"}}},
    {"index": 1, "role": "tool_use", "text": "", "content": {"id": 4, "name": "Bash", "input": {}}},
    {"index": 1, "role": "tool_use", "text": "", "content": {"input": "ls"}},
    {"index": 1, "role": "tool_use", "text": "", "content": {"status": "running"}},
    {"index": 1, "role": "tool_result", "text": "", "content": {"is_error": "no"}},
    {"index": 1, "role": "tool_result", "text": "", "content": {"resource_links": [{"uri": 1}]}},
    {"index": 1, "role": "user", "text": "", "cwd": 3},
])
def test_the_server_refuses_a_malformed_row(row):
    assert row_problems(row) != []


def test_a_producer_is_held_to_more_than_the_server():
    """The server stores a tool call with no id; no producer may ship one."""
    row = {"index": 64, "role": "tool_use", "text": "", "content": {"id": "", "name": "Bash", "input": {}}}
    assert row_problems(row) == []
    assert "content.id must not be empty" in producer_problems(row)
    assert "a durable row's index must be >= 0" in producer_problems({**row, "index": -1})
    assert producer_problems({**row, "content": {**row["content"], "id": "t"}}, live=True) == [
        "a live row's index must be -1"]
