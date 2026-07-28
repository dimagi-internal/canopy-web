"""Hook payloads -> live rows.

Shapes verified against real PostToolUse payloads captured from a live
emdash-driven session (2026-07-27).
"""
import canopy_transcript as ct


def _payload(**over):
    base = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "cwd": "/Users/x/emdash/worktrees/repo/emdash/task",
        "tool_name": "Bash",
        "tool_use_id": "toolu_01ABC",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "a.txt\n", "stderr": "", "interrupted": False},
        "duration_ms": 16,
    }
    base.update(over)
    return base


def test_one_hook_event_is_a_complete_tool_pair():
    """PostToolUse carries the result as well as the input, so a single event
    yields both rows — where the transcript splits them across two records."""
    use, result = ct.rows_for_hook(_payload())
    assert use["role"] == "tool_use"
    assert use["content"] == {"id": "toolu_01ABC", "name": "Bash",
                              "input": {"command": "ls"}, "status": "complete"}
    assert result["role"] == "tool_result"
    assert result["content"] == {"tool_use_id": "toolu_01ABC", "is_error": False}
    assert result["text"] == "a.txt"


def test_live_rows_carry_no_ordinal():
    """index = -1 is what keeps a hook event OUT of the durable store: the server
    persists only ordinal-keyed rows. The transcript stays the one record."""
    assert all(r["index"] == -1 for r in ct.rows_for_hook(_payload()))
    assert all(e["index"] == -1 and e["seq"] == -1
               for e in ct.events_for_hook(_payload()))


def test_an_event_with_no_tool_use_id_is_dropped():
    """Without the correlation key a live row can never be reconciled against
    its durable counterpart, so it would duplicate forever."""
    assert ct.rows_for_hook(_payload(tool_use_id="")) == []
    assert ct.rows_for_hook(_payload(tool_use_id=None)) == []


def test_pretooluse_yields_a_pending_row_and_no_result():
    """The point of forwarding PreToolUse: a row the instant the call STARTS, so
    a long tool call reads as running rather than as nothing happening. It must
    NOT emit a tool_result — an empty result would render as a finished call.

    Safe to forward because our hook cannot block: fire-and-forget, 2s cap, and
    it never returns a decision."""
    rows = ct.rows_for_hook(_payload(hook_event_name="PreToolUse", tool_response=None))
    assert len(rows) == 1
    assert rows[0]["role"] == "tool_use"
    assert rows[0]["content"]["status"] == "pending"
    assert rows[0]["content"]["name"] == "Bash"


def test_the_completed_row_replaces_the_pending_one_by_id():
    """Both carry the same tool_use_id, which is what lets the client upsert
    rather than show the call twice — once running, once done."""
    pre = ct.rows_for_hook(_payload(hook_event_name="PreToolUse"))[0]
    post_use = ct.rows_for_hook(_payload())[0]
    assert pre["content"]["id"] == post_use["content"]["id"]
    assert (pre["content"]["status"], post_use["content"]["status"]) == ("pending", "complete")


def test_a_failure_event_marks_the_result_as_an_error():
    rows = ct.rows_for_hook(_payload(hook_event_name="PostToolUseFailure"))
    assert rows[1]["content"]["is_error"] is True


def test_stderr_is_used_when_there_is_no_stdout():
    rows = ct.rows_for_hook(_payload(
        tool_response={"stdout": "", "stderr": "command not found"}))
    assert rows[1]["text"] == "command not found"


def test_a_string_tool_response_reads_like_the_transcript():
    rows = ct.rows_for_hook(_payload(tool_response="plain output"))
    assert rows[1]["text"] == "plain output"


def test_hook_payloads_are_capped_like_transcript_rows():
    """A hook carries the same enormous tool bodies the transcript does, and
    reaches the same phone over the same websocket."""
    rows = ct.rows_for_hook(_payload(
        tool_input={"content": "x" * 100_000},
        tool_response={"stdout": "y" * 100_000}))
    assert len(rows[0]["content"]["input"]["content"]) < ct.TOOL_INPUT_STR_MAX + 100
    assert len(rows[1]["text"]) < ct.TOOL_TEXT_MAX + 100


def test_nul_bytes_are_scrubbed_on_the_hook_path_too():
    """Postgres rejects NUL. The hook path doesn't persist today, but it feeds
    the same shape into the same code, so it must not be the one path that
    reintroduces the byte that took down a whole batch."""
    rows = ct.rows_for_hook(_payload(tool_response={"stdout": "bin\x00ary"}))
    assert "\x00" not in rows[1]["text"]


def test_a_notification_reports_blocked():
    """Claude Code fires Notification when it wants a human — a permission
    prompt, or an idle wait for input. Before this, a session blocked on a
    permission dialog looked exactly like one that was working, which is the
    worst of the three states to get wrong: you wait on an agent that is
    waiting on you.
    """
    assert ct.activity_for_hook({"hook_event_name": "Notification",
                              "message": "Claude needs your permission to use Bash"}) == "blocked"


def test_blocked_is_coarse_on_purpose():
    """A hook observer cannot tell "needs permission" from "idle waiting" —
    emdash can only because it is an ACP client with a real
    pendingPermissionCount. Parsing the message string to guess would be
    inventing precision we do not have, so both resolve to "blocked".
    """
    idle = ct.activity_for_hook({"hook_event_name": "Notification",
                              "message": "Claude is waiting for your input"})
    permission = ct.activity_for_hook({"hook_event_name": "Notification",
                                    "message": "Claude needs your permission to use Bash"})
    assert idle == permission == "blocked"


def test_a_notification_is_a_state_not_a_row():
    """It carries no chat content — emitting a row would put a phantom message
    in the transcript."""
    assert ct.rows_for_hook({"hook_event_name": "Notification", "message": "x"}) == []
