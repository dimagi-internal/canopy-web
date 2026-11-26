"""The cloud runner reports its half of a turn's reply-latency trace on finish."""
from __future__ import annotations


def test_marks_are_first_write_wins_and_taken_once(cloud_runner):
    cloud_runner._lat_mark("t1", "session_ready")
    first = cloud_runner._LATENCY["t1"]["session_ready"]
    cloud_runner._lat_mark("t1", "session_ready")
    cloud_runner._lat_fact("t1", warm=False)
    taken = cloud_runner._lat_take("t1")
    assert taken == {"session_ready": first, "warm": False}
    assert cloud_runner._lat_take("t1") is None


def test_first_text_is_the_first_assistant_event_and_every_event_still_passes(cloud_runner):
    seen = []
    emit = cloud_runner._lat_emit("t2", seen.append)
    emit({"kind": "tool_start"})
    assert "first_text" not in cloud_runner._LATENCY.get("t2", {})
    emit({"kind": "assistant", "payload": {"text": "hi"}})
    at = cloud_runner._LATENCY["t2"]["first_text"]
    emit({"kind": "assistant", "payload": {"text": "more"}})
    assert cloud_runner._LATENCY["t2"]["first_text"] == at
    assert [e["kind"] for e in seen] == ["tool_start", "assistant", "assistant"]
    cloud_runner._lat_take("t2")


def test_the_trace_is_bounded(cloud_runner):
    for i in range(cloud_runner._LATENCY_MAX + 5):
        cloud_runner._lat_mark(f"b{i}", "prompt_sent")
    assert len(cloud_runner._LATENCY) <= cloud_runner._LATENCY_MAX
    cloud_runner._LATENCY.clear()
