# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""`@canopy hal --codex <ask>`: a Slack thread chooses the agent CLI its conversation
runs on (spec 2026-10-26-multi-cli-agents §1). The flag is read where a
conversation starts; a thread keeps the CLI it started on."""
from __future__ import annotations

import pytest

from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.slack import services
from tests.fixtures.slack import BOT, TEAM, mention

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

THREAD = "1700000000.000100"


@pytest.fixture(autouse=True)
def _every_cli_on_some_runner(monkeypatch):
    # Which runners have which CLI is `clis.available`'s own test; here every CLI
    # is on some box, so the flag is what is tested.
    from apps.harness import clis

    monkeypatch.setattr(clis, "available", lambda _ws: set(clis.CLIS))


def _private(slack) -> list[str]:
    return [p["text"] for p in slack.said("chat.postEphemeral")]


def _both(hal):
    hal.clis = ["claude-code", "codex"]
    hal.save(update_fields=["clis"])


def test_the_flag_starts_the_thread_on_codex(slack, linked, hal):
    _both(hal)
    mention(f"<@{BOT}> hal --codex draft the launch post")
    turn = Turn.objects.get()
    assert turn.prompt.endswith("draft the launch post") and "--codex" not in turn.prompt
    assert turn.chat_session.cli == "codex"


def test_without_the_flag_the_agents_default_decides(slack, linked, hal):
    _both(hal)
    mention(f"<@{BOT}> hal draft the launch post")
    assert Turn.objects.get().chat_session.cli == ""


def test_claude_spelled_either_way(slack, linked, hal):
    hal.clis = ["codex", "claude-code"]
    hal.save(update_fields=["clis"])
    mention(f"<@{BOT}> hal --claude fix the build")
    assert Turn.objects.get().chat_session.cli == "claude-code"
    assert services.cli_flag("--claude-code fix it") == ("claude-code", "fix it")
    assert services.cli_flag("fix --codex it") == ("", "fix --codex it")


def test_a_cli_the_agent_is_not_set_up_for_is_refused(slack, linked, hal):
    mention(f"<@{BOT}> hal --codex draft the launch post")
    assert not Turn.objects.exists() and not Session.objects.exists()
    (note,) = _private(slack)
    assert "Hal isn't set up to run on Codex" in note
    assert "Nothing you wrote here was sent" in note


def test_a_thread_keeps_its_cli(slack, linked, hal):
    _both(hal)
    Session.objects.create(workspace=hal.workspace, agent=hal, title="t", cli="claude-code", metadata={
        services.SLACK_THREAD_KEY: services.thread_key(TEAM, "C1", THREAD),
        "slack_team": TEAM, "slack_channel": "C1", "slack_thread_ts": THREAD})
    mention(f"<@{BOT}> hal --codex now draft it", ts="1700000000.000900", thread_ts=THREAD)
    assert not Turn.objects.exists()
    (note,) = _private(slack)
    assert "This thread runs on Claude Code" in note
    assert "start a new thread" in note


# ---- the slash command: refused before its public anchor is posted ----
def test_slash_command_on_codex_names_it_in_the_anchor(slack, linked, hal):
    from tests.fixtures.slack import command

    _both(hal)
    command("hal --codex draft the launch post")
    (anchor,) = [p["text"] for p in slack.said("chat.postMessage")][:1]
    assert "asked *hal* on Codex: draft the launch post" in anchor
    assert Turn.objects.get().chat_session.cli == "codex"


def test_slash_command_refused_posts_nothing_public(slack, linked, hal):
    from tests.fixtures.slack import command

    resp = command("hal --codex draft the launch post")
    assert "Hal isn't set up to run on Codex" in resp.json()["text"]
    assert not slack.said("chat.postMessage")
    assert not Turn.objects.exists()
