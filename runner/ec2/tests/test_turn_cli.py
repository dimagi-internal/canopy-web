"""What the claim says about a turn's agent CLI reaches the CLI: the model its agent
names (`cli_model`) is the one `claude -p` starts on, and a turn claimed for a CLI
this box's executor does not drive is refused before anything runs."""
from __future__ import annotations

import pytest

TID = "0c1d2e3f-0000-4000-8000-0000000000c1"


@pytest.mark.parametrize("given,model", [
    ("opus", "opus"), ("claude-opus-5-5[1m]", "claude-opus-5-5[1m]"), ("", ""),
    ("opus --dangerously-skip-permissions", ""), ("-p", ""), (None, ""),
])
def test_the_model_is_taken_only_when_it_is_a_model_name(cloud_runner, given, model):
    assert cloud_runner._turn_model({"cli_model": given}) == model


def test_claude_starts_on_the_turns_model(cloud_runner):
    cloud_runner._TURN_ENV.model = "opus"
    try:
        cmd = cloud_runner._claude_cmd("hello")
    finally:
        cloud_runner._TURN_ENV.model = None
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert "--model" not in cloud_runner._claude_cmd("hello")


def test_a_turn_for_another_cli_is_refused(cloud_runner, monkeypatch):
    finished = []
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda method, path, body=None, **kw: finished.append((path, body)))
    assert cloud_runner._cli_refusal({"cli": "claude-code"}) is None
    assert cloud_runner._cli_refusal({}) is None, "a server that names no CLI means Claude Code"
    cloud_runner._run_turn("runner-1", {"id": TID, "cli": "codex", "prompt": "/echo:turn"})
    ((path, body),) = finished
    assert path == f"/turns/{TID}/finish"
    assert body["status"] == "failed" and body["result_note"].startswith("cli_mismatch")
    assert getattr(cloud_runner._TURN_ENV, "model", None) is None
