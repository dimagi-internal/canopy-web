"""A confined turn on the cloud runner whose session lacks its profile's MCP tools
fails, with its reply withheld, instead of answering a caller from nothing.

Live case (2026-11-25, labs thread 8): a connect-helper contact turn on
cloud-ec2-live answered "I can't look up your payments in this chat right now"
and ended done, so labs never handed the worker to a person. The laptop runner
got this check in canopy-web#2959; the cloud runner, which takes those turns
first, had none.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

PROFILE = {"tools": ["mcp__plugin_canopy_canopy-web__site_call",
                     "mcp__plugin_canopy_canopy-web__who_is_asking", "Read"]}


def _delta(added, removed=()):
    return {"type": "attachment", "attachment": {
        "type": "deferred_tools_delta", "addedNames": list(added), "removedNames": list(removed)}}


def _transcript(tmp_path, records):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _with_transcript(cloud_runner, monkeypatch, path):
    core = SimpleNamespace(resolve_cli_transcript=lambda cwd, sid, claude_home=None: path)
    monkeypatch.setattr(cloud_runner, "_transcript_core", lambda: core)


def _gate(cloud_runner, sent):
    return cloud_runner._ConfinedToolsGate.for_turn(PROFILE, sent.append)


def test_no_gate_for_a_full_profile_turn_or_a_profile_without_mcp_tools(cloud_runner):
    assert cloud_runner._ConfinedToolsGate.for_turn(None, print) is None
    assert cloud_runner._ConfinedToolsGate.for_turn({"tools": ["Read"]}, print) is None


def test_text_is_held_and_other_events_pass(cloud_runner):
    sent = []
    gate = _gate(cloud_runner, sent)
    gate.emit([{"kind": "tool_start", "payload": {}}, {"kind": "assistant", "payload": {"text": "hi"}}])
    assert sent == [[{"kind": "tool_start", "payload": {}}]]


def test_unauthenticated_mcp_withholds_the_reply_and_names_the_tools(cloud_runner, monkeypatch, tmp_path):
    path = _transcript(tmp_path, [_delta([
        "mcp__plugin_canopy_canopy-web__authenticate",
        "mcp__plugin_canopy_canopy-web__complete_authentication"])])
    _with_transcript(cloud_runner, monkeypatch, path)
    sent = []
    gate = _gate(cloud_runner, sent)
    gate.emit([{"kind": "assistant", "payload": {"text": "I can't see your payment record"}}])
    why = gate.settle(tmp_path, "sid")
    assert why.startswith("missing_tools:")
    assert "mcp__plugin_canopy_canopy-web__authenticate" in why
    assert sent == []          # the caller never sees the answer made from nothing
    gate.fail_event()
    assert sent[-1][0]["kind"] == "error"


def test_a_session_with_its_tools_releases_the_reply(cloud_runner, monkeypatch, tmp_path):
    path = _transcript(tmp_path, [
        _delta(["mcp__plugin_canopy_canopy-web__authenticate"]),
        _delta(["mcp__plugin_canopy_canopy-web__site_call"],
               ["mcp__plugin_canopy_canopy-web__authenticate"])])
    _with_transcript(cloud_runner, monkeypatch, path)
    sent = []
    gate = _gate(cloud_runner, sent)
    gate.emit([{"kind": "assistant", "payload": {"text": "Your last payment was on 3 March."}}])
    assert gate.settle(tmp_path, "sid") == ""
    assert sent == [[{"kind": "assistant", "payload": {"text": "Your last payment was on 3 March."}}]]


def test_a_transcript_with_no_tool_list_is_not_judged(cloud_runner, monkeypatch, tmp_path):
    path = _transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
    _with_transcript(cloud_runner, monkeypatch, path)
    sent = []
    gate = _gate(cloud_runner, sent)
    gate.emit([{"kind": "assistant", "payload": {"text": "hello"}}])
    assert gate.settle(tmp_path, "sid") == ""
    assert sent == [[{"kind": "assistant", "payload": {"text": "hello"}}]]
