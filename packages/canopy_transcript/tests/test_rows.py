"""The transcript core: ordinals, block extraction, caps, NUL scrubbing.

These were the laptop runner's tests before the core was extracted. They live
with the code now so the cloud runner inherits the same guarantees rather than
re-proving them.
"""
import canopy_transcript as ct
from canopy_transcript import conversational_messages


def _asst(text, stop="tool_use"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                             "stop_reason": stop}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _ix(record, block=0):
    return ct.compose_index(record, block)


def test_conversational_messages_carry_the_composite_record_ordinal():
    """The index is derived from the RAW position in the .jsonl — records that
    produce no row (summaries) still advance it, so the ordinal is stable no
    matter how the transcript is filtered."""
    recs = [{"type": "summary"}, _user("q1"), _asst("a1"), _asst("a2")]
    assert conversational_messages(recs, -1) == [
        {"index": _ix(1), "role": "user", "text": "q1", "content": {}},
        {"index": _ix(2), "role": "assistant", "text": "a1", "content": {}},
        {"index": _ix(3), "role": "assistant", "text": "a2", "content": {}},
    ]


def test_conversational_messages_only_after_since():
    recs = [_user("q1"), _asst("a1"), _user("q2"), _asst("a2")]
    assert [r["text"] for r in conversational_messages(recs, _ix(1))] == ["q2", "a2"]
    assert conversational_messages(recs, _ix(3)) == []


def test_tool_calls_become_rows_the_ui_can_pair():
    """A tool_use and its result carry the correlating ids the client pairs on —
    without them a stream with parallel calls is genuinely ambiguous to render."""
    recs = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}]}},
    ]
    use, result = conversational_messages(recs, -1)
    assert use == {"index": _ix(0), "role": "tool_use", "text": "",
                   "content": {"id": "toolu_1", "name": "Bash",
                               "input": {"command": "ls"}}}
    assert result == {"index": _ix(1), "role": "tool_result", "text": "a.txt",
                      "content": {"tool_use_id": "toolu_1", "is_error": False}}


def test_every_block_of_a_multi_block_record_gets_its_own_ordinal():
    """THE reason the ordinal is composite: one record can hold prose AND several
    parallel tool calls (observed live). Keying on the record alone kept only the
    first block — i.e. dropped exactly the tool calls this feature shows."""
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "checking both"},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "Grep", "input": {}},
    ]}}]
    rows = conversational_messages(recs, -1)
    assert [r["index"] for r in rows] == [_ix(0, 0), _ix(0, 1), _ix(0, 2)]
    assert [r["role"] for r in rows] == ["assistant", "tool_use", "tool_use"]
    # Strictly inside the record's own slots: never into the next record's.
    assert all(r["index"] < _ix(1, 0) for r in rows)


def test_ordinals_never_collide_across_records():
    """The composite ordinal is the Message primary key within a session, so a
    collision would silently drop a row (get_or_create finds the other one)."""
    recs = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "X", "input": {}}
            for i in range(ct.BLOCK_STRIDE + 5)
        ]}},
        _asst("after"),
    ]
    rows = conversational_messages(recs, -1)
    indices = [r["index"] for r in rows]
    assert len(set(indices)) == len(indices)             # no ordinal used twice
    assert max(i for i in indices if i < _ix(1)) < _ix(1)  # never reaches record 1
    assert rows[-1]["index"] == _ix(1)
    # Overflow is REPORTED, not silently swallowed: the reserved last slot says
    # how many blocks of that record went unrecorded.
    overflow = [r for r in rows if r["content"].get("_overflow")]
    assert len(overflow) == 1
    assert overflow[0]["content"]["_overflow"] == 6  # 69 blocks - 63 kept
    assert "not recorded" in overflow[0]["text"]


def test_record_offset_shifts_the_record_not_the_composite_index():
    """An incremental batch starts partway through the file; the offset applies to
    the record ordinal, so its rows land in that record's slots exactly as a
    full-file read would have placed them."""
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ]}}]
    assert [r["index"] for r in conversational_messages(recs, -1, record_offset=7)] == [
        _ix(7, 0), _ix(7, 1),
    ]


def test_tool_payloads_are_capped_at_the_producer():
    """A Read of a large file or a Bash dump flows to a phone over a websocket and
    into a JSONField — the cap belongs here, before the wire."""
    big = "x" * 100_000
    recs = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Write",
             "input": {"content": big}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": big}]}},
    ]
    use, result = conversational_messages(recs, -1)
    assert len(use["content"]["input"]["content"]) < ct.TOOL_INPUT_STR_MAX + 100
    assert len(result["text"]) < ct.TOOL_TEXT_MAX + 100
    assert "truncated" in result["text"]


def test_a_non_text_tool_result_still_emits_so_its_pair_resolves():
    """An image-only result that emitted nothing would leave its tool_use stuck
    rendering "running…" forever."""
    recs = [{"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "image"}]}]}}]
    (row,) = conversational_messages(recs, -1)
    assert row["text"] == "[image]"
    assert row["content"]["tool_use_id"] == "t1"


def test_tool_result_error_flag_survives():
    recs = [{"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": "boom"}]}}]
    (row,) = conversational_messages(recs, -1)
    assert row["content"]["is_error"] is True


def test_row_payload_is_one_shape_for_every_hop():
    """The live WS frame's `block`, the stored Message.content and the backfill
    payload are all this dict — so a row reads the same live as from history."""
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}]
    (row,) = conversational_messages(recs, -1)
    assert ct.row_payload(row) == {
        "id": "t1", "name": "Bash", "input": {"command": "ls"}, "text": "",
    }


def test_end_index_marks_the_whole_file_as_already_seen():
    """A first attach with no server marker must stream FORWARD only — every row
    of every existing record, including its last block, has to fall behind it."""
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "t1", "name": "X", "input": {}},
    ]}}]
    assert conversational_messages(recs, ct.end_index(len(recs))) == []


def test_nul_bytes_are_stripped_from_tool_payloads():
    """Postgres rejects NUL in text/jsonb, and the server writes a backfill batch
    in ONE transaction — so a single binary tool result (a Read of a compressed
    file) 500s the whole batch and the session's history can never rebuild.
    Labs 2026-07-26: exactly one row in 683 did this."""
    recs = [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "binary\x00payload"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Write",
             "input": {"content": "also\x00bad"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "prose\x00too"}]}},
    ]
    result, use, prose = conversational_messages(recs, -1)
    assert "\x00" not in result["text"]
    assert "\x00" not in use["content"]["input"]["content"]
    assert "\x00" not in prose["text"]
    # Stripped, not mangled — the surrounding content survives.
    assert result["text"] == "binarypayload"


def test_scrub_leaves_other_control_characters_alone():
    """Only NUL is rejected by Postgres; tabs/newlines/escapes are real content
    and stripping them would corrupt every terminal capture we ship."""
    assert ct.scrub("a\tb\nc\x1b[0m") == "a\tb\nc\x1b[0m"
