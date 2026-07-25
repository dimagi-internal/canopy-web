"""The emdash-response bridge: tail assistant text + idle-based completion."""
from canopy_runner.chat_bridge import (
    bridge_response,
    conversational_messages,
    new_assistant_texts,
)


def _asst(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _tool():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}


def test_new_assistant_texts_after_offset():
    recs = [_user("hi"), _asst("hello"), _tool(), _asst("done")]
    assert new_assistant_texts(recs, 0) == ["hello", "done"]
    assert new_assistant_texts(recs, 2) == ["done"]  # only after the offset
    assert new_assistant_texts([_tool()], 0) == []   # tool-only -> no text


def test_bridge_posts_new_assistant_texts_and_completes_on_idle():
    states = [
        [_user("prompt")],                                    # only the injected prompt
        [_user("prompt"), _asst("thinking")],                 # first assistant chunk
        [_user("prompt"), _asst("thinking"), _asst("final")],  # second, then stable
    ]
    box = {"i": 0}

    def records_fn():
        i = min(box["i"], len(states) - 1)
        box["i"] += 1
        return states[i]

    events = []
    result = bridge_response(
        events.append, records_fn, start_index=1,  # skip the injected user prompt
        idle_rounds=2, max_rounds=50, sleep=lambda _s: None, poll=0,
    )
    assert [(e["kind"], e["payload"]["text"]) for e in events] == [
        ("assistant", "thinking"),
        ("assistant", "final"),
    ]
    assert result == "thinking\n\nfinal"


def test_bridge_times_out_without_assistant():
    def records_fn():
        return [_user("p")]  # never grows, no assistant ever

    events = []
    result = bridge_response(
        events.append, records_fn, start_index=1,
        idle_rounds=2, max_rounds=5, sleep=lambda _s: None,
    )
    assert events == []
    assert result == ""


def test_conversational_messages_carry_the_raw_record_ordinal():
    """The index is the RAW position in the .jsonl — non-conversational records
    (summaries, tool-only turns) advance it without producing a row, so the
    ordinal is stable no matter how the transcript is filtered."""
    recs = [{"type": "summary"}, _user("q1"), _asst("a1"), _tool(), _asst("a2")]
    assert conversational_messages(recs, -1) == [
        {"index": 1, "role": "user", "text": "q1"},
        {"index": 2, "role": "assistant", "text": "a1"},
        {"index": 4, "role": "assistant", "text": "a2"},  # tool_use record skipped (no text)
    ]


def test_conversational_messages_only_after_since():
    recs = [_user("q1"), _asst("a1"), _user("q2"), _asst("a2")]
    assert conversational_messages(recs, 1) == [
        {"index": 2, "role": "user", "text": "q2"},
        {"index": 3, "role": "assistant", "text": "a2"},
    ]
    assert conversational_messages(recs, 3) == []
