"""The sparse-patch merge is the whole reason this module exists.

ACP's `tool_call_update` is a PATCH, not a row: the observed stream for a single
Bash call was five messages, only the first carrying `title`/`kind`, only the
last carrying `status: completed`, and the result arriving on a message that
carried nothing else at all. Rendering any one of those as a row ships a
transcript full of half-empty tool calls, so every test here is about what
survives a merge rather than what a single update says.
"""
from canopy_acp import UpdateReducer


# The exact sequence observed on 2026-07-27 from claude-agent-acp 0.63.0 for one
# `echo hello-from-acp` Bash call. Kept verbatim — a synthetic approximation
# would not have caught that `status` is absent from the middle updates.
BASH_SEQUENCE = [
    {"sessionUpdate": "tool_call", "toolCallId": "toolu_01A", "status": "pending",
     "title": "Terminal", "kind": "execute", "rawInput": {}, "content": [],
     "_meta": {"claudeCode": {"toolName": "Bash"}}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_01A",
     "rawInput": {"command": "echo hello-from-acp"}, "title": "echo hello-from-acp",
     "kind": "execute", "_meta": {"claudeCode": {"toolName": "Bash"}}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_01A",
     "_meta": {"claudeCode": {
         "toolResponse": {"stdout": "hello-from-acp", "stderr": "", "interrupted": False},
         "toolName": "Bash"}}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_01A", "status": "completed",
     "rawOutput": "hello-from-acp",
     "content": [{"type": "content", "content": {"type": "text", "text": "```console\nhello-from-acp\n```"}}],
     "_meta": {"claudeCode": {"toolName": "Bash"}}},
]


def test_a_patch_never_erases_a_field_it_omits():
    r = UpdateReducer()
    for u in BASH_SEQUENCE:
        r.apply(u)
    call = r.tool_call("toolu_01A")
    # title/kind came from updates 1-2; status only from update 4. A last-write-wins
    # merge that took update 3 wholesale would have blanked all three.
    assert call.title == "echo hello-from-acp"
    assert call.kind == "execute"
    assert call.status == "completed"
    assert call.tool_name == "Bash"


def test_the_agents_title_wins_over_the_placeholder():
    """`tool_call` opens with a generic title ("Terminal") and the real one
    arrives on a later patch. Showing the placeholder is the bug this prevents."""
    r = UpdateReducer()
    r.apply(BASH_SEQUENCE[0])
    assert r.tool_call("toolu_01A").title == "Terminal"
    r.apply(BASH_SEQUENCE[1])
    assert r.tool_call("toolu_01A").title == "echo hello-from-acp"


def test_result_text_is_taken_from_the_claude_tool_response():
    r = UpdateReducer()
    for u in BASH_SEQUENCE:
        r.apply(u)
    assert r.tool_call("toolu_01A").result_text == "hello-from-acp"


def test_a_call_is_pending_until_a_terminal_status_arrives():
    r = UpdateReducer()
    for u in BASH_SEQUENCE[:3]:
        r.apply(u)
    assert not r.tool_call("toolu_01A").is_complete
    r.apply(BASH_SEQUENCE[3])
    assert r.tool_call("toolu_01A").is_complete


def test_rows_match_the_shape_the_hook_path_already_emits():
    """canopy_transcript.rows_for_hook is the existing contract; ACP rows must be
    interchangeable with it or the client needs two renderers."""
    r = UpdateReducer()
    for u in BASH_SEQUENCE:
        r.apply(u)
    rows = r.rows_for_tool_call("toolu_01A")
    assert [x["role"] for x in rows] == ["tool_use", "tool_result"]
    use, result = rows
    assert use["index"] == -1 and result["index"] == -1   # live rows are never persisted
    assert use["content"]["id"] == "toolu_01A"
    assert use["content"]["name"] == "Bash"
    assert use["content"]["input"] == {"command": "echo hello-from-acp"}
    assert use["content"]["status"] == "complete"
    assert result["content"]["tool_use_id"] == "toolu_01A"
    assert result["content"]["is_error"] is False
    assert result["text"] == "hello-from-acp"


def test_a_pending_call_yields_the_tool_use_alone():
    """Same rule the PreToolUse hook follows: no empty tool_result, or the UI
    renders a finished-looking call with no output."""
    r = UpdateReducer()
    r.apply(BASH_SEQUENCE[0])
    rows = r.rows_for_tool_call("toolu_01A")
    assert [x["role"] for x in rows] == ["tool_use"]
    assert rows[0]["content"]["status"] == "pending"


def test_an_errored_call_is_marked_on_the_result_row():
    r = UpdateReducer()
    r.apply({"sessionUpdate": "tool_call", "toolCallId": "t1", "status": "pending",
             "title": "x", "kind": "execute"})
    r.apply({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed",
             "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}]})
    rows = r.rows_for_tool_call("t1")
    assert rows[1]["content"]["is_error"] is True
    assert rows[0]["content"]["status"] == "complete"  # terminal, even though it failed


def test_assistant_text_accumulates_across_chunks():
    r = UpdateReducer()
    for part in ("The", " command", " printed ok."):
        r.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}})
    assert r.assistant_text == "The command printed ok."


def test_thinking_is_kept_apart_from_the_reply():
    """agent_thought_chunk is the signal the transcript never carried (measured
    empty in 11,010 of 11,011 blocks). It must not contaminate the reply text."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
    r.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}})
    assert r.assistant_text == "hello"
    assert r.thinking_text == "hmm"


def test_rate_limit_is_surfaced_because_the_cascade_needs_it():
    """The fleet runs on subscriptions and the cascade is reactive today; this is
    the only place a *predictive* signal exists."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "usage_update", "used": 55517, "size": 1_000_000,
             "_meta": {"_claude/rateLimit": {"status": "allowed", "resetsAt": 1785219600,
                                             "rateLimitType": "five_hour",
                                             "overageStatus": "rejected"}}})
    assert r.usage["used"] == 55517
    assert r.rate_limit["rateLimitType"] == "five_hour"
    assert r.rate_limit["status"] == "allowed"


def test_a_usage_update_without_rate_limit_meta_leaves_the_last_one_standing():
    """Most usage_updates carry no rate-limit meta; a naive assignment would
    null out the signal a moment after it arrived."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "usage_update", "used": 1,
             "_meta": {"_claude/rateLimit": {"status": "allowed", "rateLimitType": "five_hour"}}})
    r.apply({"sessionUpdate": "usage_update", "used": 2})
    assert r.rate_limit["status"] == "allowed"
    assert r.usage["used"] == 2


def test_session_title_is_captured():
    """The agent supplies it — PRs #475-#477 were all deriving it by hand."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "session_info_update", "title": "Fix the flaky test",
             "updatedAt": "2026-07-28T01:39:27.923Z"})
    assert r.title == "Fix the flaky test"


def test_unknown_update_kinds_are_ignored_rather_than_fatal():
    """ACP is versioned and the adapter ships new update kinds; an unrecognised
    one must never take down a turn."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "some_future_thing", "whatever": 1})
    r.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}})
    assert r.assistant_text == "ok"


def test_tool_calls_keep_arrival_order():
    r = UpdateReducer()
    for tid in ("t1", "t2", "t3"):
        r.apply({"sessionUpdate": "tool_call", "toolCallId": tid, "status": "pending",
                 "title": tid, "kind": "execute"})
    assert [c.id for c in r.tool_calls] == ["t1", "t2", "t3"]


def test_parallel_calls_do_not_bleed_into_each_other():
    """Interleaved patches for concurrent tools are the case a naive
    'current tool call' pointer gets wrong."""
    r = UpdateReducer()
    r.apply({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "A", "kind": "read",
             "status": "pending"})
    r.apply({"sessionUpdate": "tool_call", "toolCallId": "b", "title": "B", "kind": "execute",
             "status": "pending"})
    r.apply({"sessionUpdate": "tool_call_update", "toolCallId": "a", "status": "completed",
             "rawOutput": "from-a"})
    assert r.tool_call("a").is_complete
    assert not r.tool_call("b").is_complete
    assert r.tool_call("a").title == "A" and r.tool_call("b").title == "B"


def test_reset_drops_stream_state_but_keeps_session_facts():
    """`session/load` replays the whole prior conversation as ordinary updates.
    Without a reset, a resumed turn's reply is the old conversation concatenated
    with the new one and every historical tool call looks like it just ran —
    observed directly against a live resumed turn.
    """
    r = UpdateReducer()
    r.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old reply"}})
    r.apply({"sessionUpdate": "tool_call", "toolCallId": "old", "status": "completed",
             "title": "old call", "kind": "execute"})
    r.apply({"sessionUpdate": "session_info_update", "title": "Session title"})
    r.apply({"sessionUpdate": "usage_update", "used": 42,
             "_meta": {"_claude/rateLimit": {"status": "allowed"}}})

    r.reset_stream_state()

    assert r.assistant_text == ""
    assert r.tool_calls == []
    # Session facts are not part of the replayed stream and must survive.
    assert r.title == "Session title"
    assert r.usage["used"] == 42
    assert r.rate_limit["status"] == "allowed"

    r.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "new reply"}})
    assert r.assistant_text == "new reply"
