"""The emdash-response bridge: tail assistant text + end-of-turn completion.\n\nThe transcript CORE (ordinals, block extraction, caps) moved to\npackages/canopy_transcript and is tested there. What remains here is the piece\nthat is genuinely emdash-specific: the between-tick state machine."""
from canopy_runner import chat_bridge
from canopy_runner.chat_bridge import (
    LiveBridge,
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



# -- a follow-up delivered INTO the running turn (canopy-web#1153) -------------
#
# Record shapes are the ones Claude Code wrote in the #1147 incident session
# (2026-10-05): typed while busy -> `queue-operation` enqueue; taken mid-turn ->
# `remove` + a `queued_command` attachment; taken after the turn -> `dequeue` +
# an ordinary user prompt.

def _enqueue(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def _remove(text):
    return {"type": "queue-operation", "operation": "remove", "content": text}


def _dequeue():
    return {"type": "queue-operation", "operation": "dequeue"}


def _injected(text):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}}


def test_a_follow_up_taken_mid_turn_ends_with_the_same_hand_back():
    b = _bridge([[_tool()],
                 [_enqueue("skip the PR checks"), _remove("skip the PR checks"),
                  _injected("skip the PR checks"), _asst("ok, skipping them", stop="end_turn")]])
    b.step(b.reader.read_new())
    b.add_rider("t2", "skip the PR checks")
    b.step(b.reader.read_new())
    assert b.riders[0].received
    assert b.done_reason == "end_turn"
    assert "1 follow-up(s) delivered mid-turn" in b.note


def test_a_hand_back_before_the_follow_up_is_taken_does_not_end_the_turn():
    """The agent finished its own turn first; Claude Code then dequeues the
    follow-up as a new prompt. Ending at the first hand-back would finish the
    follow-up's turn before its reply exists."""
    b = _bridge()
    b.add_rider("t2", "and the other deck")
    b.step([_enqueue("and the other deck"), _asst("slide done", stop="end_turn")])
    assert not b.done_reason, "held open: the follow-up has not reached the model"
    b.step([_dequeue(), _user("and the other deck"), _tool()])
    assert b.riders[0].received and not b.done_reason
    b.step([_asst("other deck done", stop="end_turn")])
    assert b.done_reason == "end_turn"
    assert b.collected == ["slide done", "other deck done"]


def test_a_follow_up_only_queued_is_not_yet_received():
    b = _bridge()
    b.add_rider("t2", "stop")
    b.step([_enqueue("stop"), _remove("stop")])
    assert not b.riders[0].received, "an enqueue/remove is not the model reading it"


def test_a_background_task_notice_is_not_a_follow_up():
    b = _bridge()
    b.add_rider("t2", "a real follow-up")
    b.step([_injected("<task-notification>\n<task-id>x</task-id>"),
            _asst("noted", stop="end_turn")])
    assert not b.riders[0].received and not b.done_reason


def test_a_rewritten_follow_up_still_counts_so_it_cannot_wedge_the_turn():
    """Claude Code may rewrite what was typed (a collapsed paste); the oldest
    waiting follow-up takes it rather than holding the turn to the idle backstop."""
    b = _bridge()
    b.add_rider("t2", "look at /tmp/screenshot.png please")
    b.step([_injected("[Image #1] please"), _asst("seen", stop="end_turn")])
    assert b.riders[0].received and b.done_reason == "end_turn"


def test_two_follow_ups_are_matched_by_their_words():
    b = _bridge()
    b.add_rider("t2", "first thing")
    b.add_rider("t3", "second thing")
    b.step([_injected("second thing"), _asst("on it", stop="end_turn")])
    assert [r.received for r in b.riders] == [False, True]
    assert not b.done_reason
    b.step([_dequeue(), _user("first thing"), _asst("both done", stop="end_turn")])
    assert b.done_reason == "end_turn"


def test_a_follow_up_reopens_a_turn_still_flushing_its_reply():
    b = _bridge()
    b.step([_asst("done", stop="end_turn")])
    assert b.done_reason == "end_turn"
    b.add_rider("t2", "one more thing")
    assert not b.done_reason


def test_the_runner_reports_it_delivers_mid_turn():
    assert chat_bridge.MIDTURN_VERSION >= 1
