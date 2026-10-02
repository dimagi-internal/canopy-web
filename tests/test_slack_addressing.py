# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""A message that reaches no agent says why, and how to reach one.

The motivating case, 2026-10-01: a colleague typed `@canopy can you search and
summarize slack for me?` as a reply in a thread an agent had answered in. No
agent was named, so it went to that thread's agent; the agent offers nothing to
this caller, so the turn was written cancelled — and the only thing Slack said
was "Cancelled." Every outcome below replaces a silence or a one-word shrug.
"""
from __future__ import annotations

import pytest

from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.slack import services
from tests.test_slack import (  # noqa: F401
    BOT,
    TEAM,
    alice,
    configured,
    hal,
    installation,
    linked,
    mention,
    slack,
    ws,
)

pytestmark = pytest.mark.django_db

THREAD = "1700000000.000100"


def _private(slack) -> list[str]:
    return [p["text"] for p in slack.said("chat.postEphemeral")]


def _public(slack) -> list[str]:
    return [p["text"] for p in slack.said("chat.postMessage")]


@pytest.fixture
def ace(ws):
    from apps.agents.models import Agent

    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, slack_enabled=True)


# ---- no agent named --------------------------------------------------------------

def test_no_agent_named_says_canopy_is_not_the_one_answering(slack, linked, hal, ace):
    mention(f"<@{BOT}> can you search slack for me?")
    assert not Turn.objects.exists()
    (note,) = _private(slack)
    assert "canopy doesn't answer messages itself" in note
    assert "`ace`" in note and "`hal`" in note


def test_a_near_miss_agent_name_is_called_out(slack, linked, hal, ace):
    mention(f"<@{BOT}> hall summarise this")
    assert not Turn.objects.exists()
    (note,) = _private(slack)
    assert "`hall` isn't an agent here — did you mean `hal`?" in note


# ---- named or not, the agent turned it away ----------------------------------------

def test_a_refused_ask_says_why_instead_of_cancelled(slack, installation, hal):
    """A contact (no canopy membership) asking an agent with no published interface."""
    mention(f"<@{BOT}> hal can you search slack?")
    turn = Turn.objects.get()
    assert turn.status == Turn.CANCELLED and turn.result_note.startswith("not run:")

    (line,) = _public(slack)
    assert "Not run" in line and "`hal`" in line and "Cancelled." not in line
    (note,) = _private(slack)
    assert "`hal` didn't run this" in note
    assert "didn't name an agent" not in note          # it was named


def test_an_unnamed_ask_routed_to_the_threads_agent_says_so_when_refused(slack, installation, hal, ace):
    """Andrew's case: a reply in a thread hal was already in, naming nobody."""
    session = Session.objects.create(workspace=hal.workspace, agent=hal, title="t", metadata={
        services.SLACK_THREAD_KEY: services.thread_key(TEAM, "C1", THREAD),
        "slack_team": TEAM, "slack_channel": "C1", "slack_thread_ts": THREAD})
    mention(f"<@{BOT}> can you search and summarize slack for me?",
            ts="1700000000.000900", thread_ts=THREAD)
    turn = Turn.objects.get()
    assert turn.chat_session == session and turn.status == Turn.CANCELLED
    (note,) = _private(slack)
    assert "didn't name an agent, so it went to `hal`" in note
    assert "canopy doesn't answer messages itself" in note


# ---- the thread's conversation was closed --------------------------------------------

def _closed_thread(hal) -> Session:
    return Session.objects.create(workspace=hal.workspace, agent=hal, title="t",
                                  status=Session.ARCHIVED, metadata={
        services.SLACK_THREAD_KEY: services.thread_key(TEAM, "C1", THREAD),
        "slack_team": TEAM, "slack_channel": "C1", "slack_thread_ts": THREAD})


def test_an_unnamed_message_to_a_closed_conversation_says_it_went_nowhere(slack, linked, hal, ace):
    _closed_thread(hal)
    mention(f"<@{BOT}> any update?", ts="1700000000.000900", thread_ts=THREAD)
    assert not Turn.objects.exists()
    (note,) = _private(slack)
    assert "conversation with `hal` was closed" in note
    assert "`@canopy hal <your ask>`" in note


def test_a_plain_reply_in_a_closed_thread_says_so_once_per_person(slack, linked, hal, ace):
    from tests.test_slack import event

    _closed_thread(hal)
    for i in range(3):   # people keep talking in the thread after the agent is gone
        event({"type": "message", "channel_type": "channel", "user": "UALICE", "text": "still there?",
               "ts": f"1700000000.00095{i}", "thread_ts": THREAD, "channel": "C1"})
    assert not Turn.objects.exists()
    (note,) = _private(slack)
    assert "was closed" in note


def test_naming_the_agent_asks_it_again(slack, linked, hal, ace):
    session = _closed_thread(hal)
    mention(f"<@{BOT}> hal pick this back up", ts="1700000000.000900", thread_ts=THREAD)
    assert Turn.objects.get().chat_session == session
