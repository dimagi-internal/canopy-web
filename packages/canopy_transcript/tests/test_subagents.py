"""The subagent summary a chat hangs on its `Agent` row."""
import json

from canopy_transcript import summarize_subagents


def _write(tmp_path, agent_id, records, meta=None):
    d = tmp_path / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"agent-{agent_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    if meta is not None:
        (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
    return tmp_path / "sess.jsonl"


def _prompt(ts):
    return {"type": "user", "isSidechain": True, "timestamp": ts,
            "message": {"role": "user", "content": "go"}}


def _call(ts, name, inp, stop="tool_use"):
    return {"type": "assistant", "timestamp": ts, "message": {
        "stop_reason": stop, "content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}}


def _result(ts):
    return {"type": "user", "timestamp": ts, "message": {
        "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


def _reply(ts):
    return {"type": "assistant", "timestamp": ts, "message": {
        "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}


META = {"toolUseId": "toolu_1", "description": "fix the tests", "agentType": "general-purpose",
        "requestShape": "background"}


def test_a_working_subagent_reads_running_with_its_latest_step(tmp_path):
    path = _write(tmp_path, "a1", [
        _prompt("2026-10-20T10:00:00Z"),
        _call("2026-10-20T10:00:05Z", "Read", {"file_path": "/x.py"}),
        _result("2026-10-20T10:00:06Z"),
        _call("2026-10-20T10:00:09Z", "Bash", {"command": "make test\nmore"}),
    ], META)
    [s] = summarize_subagents(path)
    assert s == {
        "agent_id": "a1", "tool_use_id": "toolu_1", "parent_agent_id": "",
        "description": "fix the tests", "agent_type": "general-purpose", "background": True,
        "state": "running", "steps": 2, "last": "Bash · make test",
        "started_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-20T10:00:09Z",
    }


def test_a_subagent_that_handed_back_reads_done_until_it_is_resumed(tmp_path):
    records = [_prompt("1"), _call("2", "Grep", {"pattern": "x"}), _result("3"), _reply("4")]
    path = _write(tmp_path, "a1", records, META)
    assert summarize_subagents(path)[0]["state"] == "done"
    # SendMessage resumes a finished agent with a new prompt.
    path = _write(tmp_path, "a1", records + [_prompt("5")], META)
    assert summarize_subagents(path)[0]["state"] == "running"


def test_no_subagents_dir_is_an_empty_list(tmp_path):
    assert summarize_subagents(tmp_path / "sess.jsonl") == []
    assert summarize_subagents(None) == []


def test_only_a_grown_file_is_read_again(tmp_path, monkeypatch):
    path = _write(tmp_path, "a1", [_prompt("1")], META)
    cache: dict = {}
    summarize_subagents(path, cache)
    from canopy_transcript import subagents

    reads = []
    real = subagents.summarize_one
    monkeypatch.setattr(subagents, "summarize_one", lambda p: reads.append(p) or real(p))
    summarize_subagents(path, cache)
    assert reads == []
    _write(tmp_path, "a1", [_prompt("1"), _call("2", "Read", {"file_path": "/y"})], META)
    assert summarize_subagents(path, cache)[0]["steps"] == 1
    assert len(reads) == 1


def test_a_nested_subagent_names_its_parent_and_mcp_tools_their_tail(tmp_path):
    path = _write(tmp_path, "b2", [_prompt("1"), _call("2", "mcp__srv__do_thing", {})],
                  {**META, "parentAgentId": "a1", "requestShape": "sync"})
    [s] = summarize_subagents(path)
    assert (s["parent_agent_id"], s["background"], s["last"]) == ("a1", False, "do_thing")
