"""The emdash-response bridge: tail assistant text + end-of-turn completion."""
from canopy_runner import chat_bridge
from canopy_runner.chat_bridge import (
    LiveBridge,
    conversational_messages,
    hands_back_to_human,
    new_assistant_texts,
)


def _asst(text, stop="tool_use"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                            "stop_reason": stop}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _tool():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                                             "stop_reason": "tool_use"}}


class _Reader:
    """Stands in for tail.TailReader: hands back one batch per read_new()."""

    def __init__(self, batches):
        self.batches = list(batches)

    def read_new(self):
        return self.batches.pop(0) if self.batches else []


def _bridge(batches=()):
    return LiveBridge(turn_id="t1", task="task-1", reader=_Reader(batches))


def test_new_assistant_texts_after_offset():
    recs = [_user("hi"), _asst("hello"), _tool(), _asst("done")]
    assert new_assistant_texts(recs, 0) == ["hello", "done"]
    assert new_assistant_texts(recs, 2) == ["done"]  # only after the offset
    assert new_assistant_texts([_tool()], 0) == []   # tool-only -> no text


def test_hands_back_to_human_only_on_a_terminal_stop_reason():
    assert hands_back_to_human(_asst("final", stop="end_turn")) is True
    assert hands_back_to_human(_asst("mid", stop="tool_use")) is False   # will continue
    assert hands_back_to_human(_tool()) is False
    assert hands_back_to_human(_user("hi")) is False
    # Other terminal reasons still end the turn — the floor is back with the human.
    assert hands_back_to_human(_asst("capped", stop="max_tokens")) is True
    # A writer that stamps nothing must NOT read as an ending (that would finish the
    # turn on its first record); the idle backstop covers it instead.
    assert hands_back_to_human({"type": "assistant", "message": {"content": []}}) is False


def test_bridge_survives_a_long_silent_tool_call():
    """THE BUG (labs 2026-07-26): completion was "the transcript went quiet for 3s",
    but a turn is silent for as long as its longest tool call — 296s in the session
    that exposed this. Every turn ended at its first Bash call, so chat showed the
    agent's opening line and dropped the answer (11 straight turns bridged 70-220
    chars each). Silence must mean nothing at all."""
    b = _bridge()
    b.step([_asst("On it.")])                 # the preamble, mid-turn
    assert b.pending == ["On it."]
    assert not b.done_reason                  # <- used to be "done" 3s later
    for _ in range(50):                       # a long tool call: many silent ticks
        b.step([])
    assert not b.done_reason, "a silent tool call must not end the turn"
    b.pending.clear()
    b.step([_asst("Here is the actual answer.", stop="end_turn")])
    assert b.pending == ["Here is the actual answer."]
    assert b.done_reason == "end_turn"
    b.pending.clear()          # the pump ships it
    assert b.finished


def test_bridge_collects_every_text_of_a_turn_for_the_note():
    b = _bridge()
    b.step([_asst("first"), _tool()])
    b.step([_asst("second", stop="end_turn")])
    assert b.collected == ["first", "second"]
    assert b.note == "chat reply bridged (13 chars)"  # "first\n\nsecond"


def test_bridge_is_not_finished_while_text_is_still_undelivered():
    """Completion means delivered: a failed POST leaves the text pending and the turn
    open, so the next tick retries instead of finishing on a reply nobody received."""
    b = _bridge()
    b.step([_asst("the answer", stop="end_turn")])
    assert b.done_reason == "end_turn"
    assert b.finished is False       # still pending
    b.pending.clear()                # the pump's successful post
    assert b.finished is True


def test_bridge_idle_backstop_ends_a_transcript_that_never_speaks():
    """The backstop exists for a writer that stamps no stop_reason, and for an
    injection that silently never landed — not for ordinary tool time."""
    b = _bridge()
    for _ in range(chat_bridge.IDLE_TICKS):
        b.step([])
    assert b.done_reason == "idle"
    assert "ended on idle" in b.note


def test_bridge_idle_counter_resets_on_any_record():
    b = _bridge()
    for _ in range(chat_bridge.IDLE_TICKS - 1):
        b.step([])
    b.step([_tool()])                 # a tool result — the session is alive
    assert b.idle_ticks == 0
    for _ in range(chat_bridge.IDLE_TICKS - 1):
        b.step([])
    assert not b.done_reason


def test_bridge_hard_tick_cap_releases_a_wedged_session():
    b = _bridge()
    for _ in range(chat_bridge.MAX_TICKS):
        b.step([_tool()])             # never silent, never ends: a wedged agent
    assert b.done_reason == "max_ticks"


def _ix(record, block=0):
    return chat_bridge.compose_index(record, block)


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
            for i in range(chat_bridge.BLOCK_STRIDE + 5)
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
    assert len(use["content"]["input"]["content"]) < chat_bridge.TOOL_INPUT_STR_MAX + 100
    assert len(result["text"]) < chat_bridge.TOOL_TEXT_MAX + 100
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
    assert chat_bridge.row_payload(row) == {
        "id": "t1", "name": "Bash", "input": {"command": "ls"}, "text": "",
    }


def test_end_index_marks_the_whole_file_as_already_seen():
    """A first attach with no server marker must stream FORWARD only — every row
    of every existing record, including its last block, has to fall behind it."""
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "t1", "name": "X", "input": {}},
    ]}}]
    assert conversational_messages(recs, chat_bridge.end_index(len(recs))) == []


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
    assert chat_bridge.scrub("a\tb\nc\x1b[0m") == "a\tb\nc\x1b[0m"
