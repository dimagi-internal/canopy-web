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
from tests.access_helpers import editors_run
from tests.fixtures.slack import BOT, TEAM, mention

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

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


def _notices(slack) -> list[dict]:
    return [p for p in slack.said("chat.postMessage")
            if any(e.get("action_id") == services.RESTART_CLOSED
                   for b in p.get("blocks") or [] for e in b.get("elements") or [])]


def _restart(session, *, user="UALICE", message_ts="1700000999.000100"):
    import json

    from tests.fixtures.slack import click

    return click(services.RESTART_CLOSED, json.dumps({"s": str(session.pk)}), user=user,
                 message_ts=message_ts, thread_ts=THREAD)


def _plain(text, ts, user="UALICE"):
    from tests.fixtures.slack import event

    return event({"type": "message", "channel_type": "channel", "user": user, "text": text,
                  "ts": ts, "thread_ts": THREAD, "channel": "C1"})


def test_an_unnamed_message_to_a_closed_conversation_posts_a_notice_with_a_button(slack, linked, hal, ace):
    closed = _closed_thread(hal)
    mention(f"<@{BOT}> any update?", ts="1700000000.000900", thread_ts=THREAD)
    assert not Turn.objects.exists()
    (notice,) = _notices(slack)
    assert notice["thread_ts"] == THREAD
    assert "This conversation with `hal` was closed, so <@UALICE>'s message didn't reach it" in notice["text"]
    (button,) = notice["blocks"][1]["elements"]
    assert button["text"]["text"] == "Start a new session with this thread's context"
    assert not _private(slack)          # the public notice is the answer
    closed.refresh_from_db()
    assert closed.metadata[services.RESTART_ASK]["text"] == "any update?"


def test_a_plain_reply_in_a_closed_thread_is_never_silent(slack, linked, hal, ace):
    """Sophie's case, 2026-10-26: the first reply got a note only she could see,
    and every later one was dropped without a word."""
    _closed_thread(hal)
    for i in range(3):   # people keep talking in the thread after the agent is gone
        _plain("still there?", f"1700000000.00095{i}")
    _plain("hello?", "1700000000.000960", user="UBOB")
    assert not Turn.objects.exists()
    assert len(_notices(slack)) == 1     # one public notice per closed session
    notes = _private(slack)
    assert len(notes) == 3               # every later reply is told, privately
    assert all("Start a new session with this thread's context" in n for n in notes)


def test_the_button_starts_a_new_session_with_the_thread_and_the_refused_message(slack, linked, hal, ace):
    editors_run(hal)
    closed = _closed_thread(hal)
    slack.threads[THREAD] = [
        {"ts": THREAD, "user": "UBOB", "text": "Can hal check the RUTF numbers?"},
        {"ts": "1700000000.000900", "user": "UALICE", "text": "and the March figures too"},
    ]
    _plain("and the March figures too", "1700000000.000900")
    (notice,) = _notices(slack)
    slack.calls.clear()

    _restart(closed)

    new = Session.objects.exclude(pk=closed.pk).get()
    closed.refresh_from_db()
    assert closed.status == Session.ARCHIVED                       # the old one stays closed
    assert new.status != Session.ARCHIVED and new.agent == hal and new.created_by.email == "alice@dimagi.com"
    assert new.metadata[services.SLACK_THREAD_KEY] == closed.metadata[services.SLACK_THREAD_KEY]
    turn = Turn.objects.get()
    assert turn.chat_session == new
    assert "Can hal check the RUTF numbers?" in turn.prompt        # the thread so far
    assert turn.prompt.rstrip().endswith("and the March figures too")  # then the refused ask
    assert turn.prompt.count("and the March figures too") == 1
    (update,) = slack.said("chat.update")                          # the button is gone
    assert "started a new session with `hal`" in update["text"]
    assert not any(b.get("type") == "actions" for b in update["blocks"])
    assert slack.said("chat.postMessage")                          # the usual status line

    # From here the thread is the new conversation: a plain reply continues it.
    _plain("thanks!", "1700000000.000990")
    assert Turn.objects.filter(chat_session=new).count() == 2
    assert len(_notices(slack)) == 0


def test_a_double_click_starts_one_session(slack, linked, hal, ace, ws):
    editors_run(hal)
    from apps.slack.models import SlackUserLink
    from apps.workspaces import services as wsvc
    from apps.workspaces.models import WorkspaceMembership
    from apps.workspaces.testing import a_user

    bob = a_user("bob@dimagi.com")
    wsvc.ensure_member(ws, bob, WorkspaceMembership.EDITOR)
    SlackUserLink.objects.create(installation=linked.installation, slack_user_id="UBOB", user=bob)
    closed = _closed_thread(hal)
    _plain("any news?", "1700000000.000900")
    _restart(closed)
    _restart(closed)
    _restart(closed, user="UBOB")
    assert Session.objects.exclude(pk=closed.pk).count() == 1
    assert Turn.objects.count() == 1
    assert sum("already going in this thread" in n for n in _private(slack)) == 2


def test_a_click_by_someone_the_agent_does_not_serve_starts_nothing(slack, linked, hal, ace):
    """UBOB has no canopy account: a contact, and hal publishes no interface."""
    closed = _closed_thread(hal)
    _plain("any news?", "1700000000.000900")
    _restart(closed, user="UBOB")
    assert Session.objects.count() == 1 and not Turn.objects.exists()
    (note,) = _private(slack)
    assert "`hal` didn't start a new session" in note
    assert not slack.said("chat.update")          # the button stays for someone who may


def test_a_closed_private_conversation_takes_nothing_from_slack(slack, linked, hal, ace):
    from unittest import mock

    closed = _closed_thread(hal)
    with mock.patch("apps.harness.sensitivity.is_sensitive", return_value=True):
        _plain("any news?", "1700000000.000900")
        _restart(closed)
    assert not _notices(slack) and not Turn.objects.exists() and Session.objects.count() == 1
    notes = _private(slack)
    assert len(notes) == 2 and all("This conversation is private" in n for n in notes)


def test_a_click_that_would_run_confined_forks_like_a_confined_reply(slack, linked, hal, ace, alice):
    """Bob is a contact and hal answers contacts only through `ask`: in someone
    else's (closed) conversation he gets a thread of his own, as a reply would."""
    from tests.access_helpers import publish

    publish(hal, {"capabilities": {"ask": {"description": "Ask", "callers": ["contact"]}}})
    closed = _closed_thread(hal)
    closed.created_by = alice
    closed.save(update_fields=["created_by"])
    _plain("any news?", "1700000000.000900")
    slack.calls.clear()
    _restart(closed, user="UBOB")
    turn = Turn.objects.get()
    fork = turn.chat_session
    assert turn.capability == "ask" and fork.contact.email == "bob@dimagi.com"
    assert fork.metadata[services.FORKED_FROM_SESSION] == str(closed.pk)
    assert fork.metadata[services.SLACK_THREAD_KEY] != closed.metadata[services.SLACK_THREAD_KEY]
    assert not slack.said("chat.update")          # the thread's own restart is still on offer


def test_naming_the_agent_asks_it_again(slack, linked, hal, ace):
    session = _closed_thread(hal)
    mention(f"<@{BOT}> hal pick this back up", ts="1700000000.000900", thread_ts=THREAD)
    assert Turn.objects.get().chat_session == session
