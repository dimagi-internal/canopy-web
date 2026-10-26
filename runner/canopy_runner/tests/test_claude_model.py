"""The configured Claude model rides every heartbeat as `claude_model`.

It is the only place the 1M window's `[1m]` is written down: the transcript
records the bare API id, so without it the chat could not size a session's
context window."""
import json

import pytest

from canopy_runner import claude_model, hooks
from canopy_runner.client import Client


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    claude_model.reset()
    yield
    claude_model.reset()


def test_reads_the_account_settings():
    hooks.user_settings_path().write_text(json.dumps({"model": "opus[1m]", "hooks": {}}))
    assert claude_model.configured() == "opus[1m]"


def test_the_environment_wins_as_it_does_for_claude_code(monkeypatch):
    hooks.user_settings_path().write_text(json.dumps({"model": "opus[1m]"}))
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    assert claude_model.configured() == "claude-sonnet-4-5"


def test_nothing_set_or_unreadable_is_empty():
    assert claude_model.configured() == ""          # no settings file
    claude_model.reset()
    hooks.user_settings_path().write_text("{not json")
    assert claude_model.configured() == ""
    claude_model.reset()
    hooks.user_settings_path().write_text(json.dumps({"model": 3}))
    assert claude_model.configured() == ""


def test_it_is_cached_between_beats():
    path = hooks.user_settings_path()
    path.write_text(json.dumps({"model": "opus"}))
    assert claude_model.configured(now=0) == "opus"
    path.write_text(json.dumps({"model": "opus[1m]"}))
    assert claude_model.configured(now=10) == "opus"
    assert claude_model.configured(now=10 + claude_model.TTL_SECONDS) == "opus[1m]"


def test_every_heartbeat_carries_it(monkeypatch):
    hooks.user_settings_path().write_text(json.dumps({"model": "opus[1m]"}))
    c = Client(base_url="http://x", token="tok")
    sent = []
    monkeypatch.setattr(c, "_call", lambda method, path, body=None: (sent.append(body), (200, {}))[1])
    c.heartbeat("r1", [])
    assert sent[-1]["claude_model"] == "opus[1m]"


# ---- the agent's model for one claimed turn (canopy-web `cli_model`) ----
@pytest.mark.parametrize("given,model", [
    ("opus", "opus"), ("claude-opus-5-5[1m]", "claude-opus-5-5[1m]"), ("", ""), (None, ""),
    ("opus --dangerously-skip-permissions", ""), ("-p", ""),
])
def test_a_turns_model_is_taken_only_when_it_is_a_model_name(given, model):
    assert claude_model.for_turn({"cli_model": given}) == model
    assert claude_model.for_turn(None) == ""
