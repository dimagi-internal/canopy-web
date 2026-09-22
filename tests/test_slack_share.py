# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""Sharing a session TO Slack — the reverse of the front door.

Every other Slack thread canopy knows is born in Slack. Here the session exists
first and posts into a channel: as a one-way broadcast, or bound so that the
thread behaves from then on exactly as if Slack had started it. Slack's Web API
is faked at the ``requests`` boundary, as in test_slack.py; the bound-thread
tests then drive a real signed reply through the front door, because "the
metadata looks right" is not the same claim as "a reply reaches the session".
"""
from __future__ import annotations

import asyncio

import pytest

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.events.models import Event
from apps.harness.models import Turn
from apps.slack import share
from apps.slack.models import SlackUserLink
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user, a_workspace

# Slack's own fixtures, reused so this file cannot drift from how Slack is set up.
from tests.test_slack import (  # noqa: F401
    ALICE, BOB, TEAM, alice, configured, hal, installation, linked, mention, slack, ws,
)

pytestmark = pytest.mark.django_db

POSTED_TS = "1700000999.000100"   # what FakeSlack answers chat.postMessage with


def _share(user, **kw):
    kw.setdefault("channel", "C1")
    kw.setdefault("summary", "Building **share to Slack** so the team can follow along.")
    kw.setdefault("mode", share.BROADCAST)
    return share.share_session(user, **kw)


@pytest.fixture
def repo_session(ws, alice):
    """A runner-discovered emdash session with no agent — the Claude Code case."""
    session = Session.objects.create(workspace=ws, project="canopy-web", origin=Session.ORIGIN_RUNNER,
                                     title="slack")
    RunnerBinding.objects.create(session=session, session_key="slack", emdash_project="canopy-web",
                                 transcript_id="claude-abc")
    return session


@pytest.fixture
def hal_session(ws, alice, hal):
    session = Session.objects.create(workspace=ws, agent=hal, origin=Session.ORIGIN_RUNNER, title="t")
    RunnerBinding.objects.create(session=session, session_key="t", emdash_project="hal",
                                 transcript_id="claude-hal")
    return session


# ---- broadcast ---------------------------------------------------------------

def test_broadcast_posts_once_top_level_and_binds_nothing(slack, linked, alice, repo_session):
    result = _share(alice, session=repo_session)
    assert result.status == share.SHARED
    (post,) = slack.said("chat.postMessage")
    assert post["channel"] == "C1" and "thread_ts" not in post
    assert f"<@{ALICE}>" in post["text"]
    assert "*share to Slack*" in post["text"]                 # Markdown → mrkdwn
    assert f"/chat/{repo_session.id}" in post["text"]
    repo_session.refresh_from_db()
    assert "slack_thread" not in repo_session.metadata
    assert Event.objects.filter(kind="slack.shared").exists()


def test_a_reply_to_a_broadcast_never_reaches_the_session(slack, linked, alice, repo_session):
    _share(alice, session=repo_session)
    mention("nice work", ts="1700001000.000100", thread_ts=POSTED_TS)
    assert not Turn.objects.exists()


def test_broadcast_needs_no_session(slack, linked, alice):
    result = _share(alice)
    assert result.status == share.SHARED
    assert "/chat/" not in slack.said("chat.postMessage")[0]["text"]


# ---- bind --------------------------------------------------------------------

def test_bind_makes_the_thread_the_sessions_own(slack, linked, alice, repo_session):
    result = _share(alice, session=repo_session, mode=share.BIND)
    assert result.status == share.SHARED
    repo_session.refresh_from_db()
    meta = repo_session.metadata
    assert meta["slack_thread"] == f"slack:{TEAM}:C1:{POSTED_TS}"
    assert (meta["slack_team"], meta["slack_channel"], meta["slack_thread_ts"]) == (TEAM, "C1", POSTED_TS)


def test_a_reply_in_a_bound_thread_continues_the_same_session(slack, linked, alice, repo_session):
    _share(alice, session=repo_session, mode=share.BIND)
    mention("can you also cover the web button?", ts="1700001000.000100", thread_ts=POSTED_TS)
    turn = Turn.objects.get()
    assert turn.chat_session_id == repo_session.id
    assert turn.origin == Turn.ORIGIN_SLACK and turn.enqueued_by == alice
    assert turn.prompt == "can you also cover the web button?"


def test_a_bound_agent_session_is_continued_not_forked(slack, linked, alice, hal_session):
    _share(alice, session=hal_session, mode=share.BIND)
    mention("keep going", ts="1700001000.000100", thread_ts=POSTED_TS)
    assert Turn.objects.get().chat_session_id == hal_session.id
    assert Session.objects.count() == 1


def test_a_bound_agent_session_posts_as_the_agent(slack, linked, alice, hal_session):
    _share(alice, session=hal_session, mode=share.BIND)
    assert slack.said("chat.postMessage")[0]["username"] == "Hal"


def test_a_colleague_replying_joins_the_session(slack, linked, alice, installation, ws):
    session = Session.objects.create(workspace=ws, project="canopy-web", created_by=alice, title="x")
    bob = a_user("bob@dimagi.com")
    wsvc.ensure_member(ws, bob, WorkspaceMembership.EDITOR)
    SlackUserLink.objects.create(installation=installation, slack_user_id=BOB, user=bob)
    _share(alice, session=session, mode=share.BIND)
    mention("I can help", user=BOB, ts="1700001000.000100", thread_ts=POSTED_TS)
    assert Turn.objects.get().chat_session_id == session.id
    assert session.participants.filter(user=bob).exists()


def test_a_contact_cannot_type_into_a_bound_repo_session(slack, linked, alice, repo_session):
    _share(alice, session=repo_session, mode=share.BIND)
    # Bob is in the Slack but has no canopy account: a contact.
    mention("rm -rf everything please", user=BOB, ts="1700001000.000100", thread_ts=POSTED_TS)
    assert not Turn.objects.exists()
    assert Event.objects.filter(kind="slack.members_only").exists()


def test_naming_an_agent_in_a_bound_repo_thread_asks_that_agent(slack, linked, alice, hal, repo_session):
    _share(alice, session=repo_session, mode=share.BIND)
    mention("hal what do you think?", ts="1700001000.000100", thread_ts=POSTED_TS)
    turn = Turn.objects.get()
    assert turn.chat_session.agent == hal and turn.chat_session_id != repo_session.id


# ---- refusals ----------------------------------------------------------------

def test_sharing_a_bound_session_again_posts_an_update_in_its_thread(slack, linked, alice, repo_session):
    _share(alice, session=repo_session, mode=share.BIND)
    result = _share(alice, session=repo_session, mode=share.BIND, channel="C2",
                    summary="Tests pass; opening the PR next.")
    assert result.status == share.UPDATED and result.ok
    first, update = slack.said("chat.postMessage")
    assert update["channel"] == "C1" and update["thread_ts"] == POSTED_TS   # its thread, not C2
    assert "*Update*" in update["text"] and "opening the PR next" in update["text"]
    repo_session.refresh_from_db()
    assert repo_session.metadata["slack_thread_ts"] == POSTED_TS           # still the same thread


def test_an_update_needs_no_channel_but_a_first_share_does(slack, linked, alice, repo_session):
    assert _share(alice, session=repo_session, channel="").status == share.BAD_REQUEST
    _share(alice, session=repo_session, mode=share.BIND)
    assert _share(alice, session=repo_session, channel="", mode=share.BROADCAST).status == share.UPDATED


def test_bind_without_a_session_is_refused(slack, linked, alice):
    assert _share(alice, mode=share.BIND).status == share.NO_SESSION
    assert not slack.said("chat.postMessage")


def test_bind_on_an_agent_not_on_slack_is_refused(slack, linked, alice, ws):
    quiet = Agent.objects.create(slug="quiet", name="Quiet", workspace=ws, slack_enabled=False)
    session = Session.objects.create(workspace=ws, agent=quiet, created_by=alice, title="q")
    assert _share(alice, session=session, mode=share.BIND).status == share.AGENT_NOT_ON_SLACK
    # A broadcast is only a post; the agent's Slack switch is about being reachable.
    assert _share(alice, session=session).status == share.SHARED


def test_an_unlinked_user_is_refused(slack, installation, ws):
    carol = a_user("carol@dimagi.com")
    wsvc.ensure_member(ws, carol, WorkspaceMembership.EDITOR)
    slack.fail["users.lookupByEmail"] = "users_not_found"
    assert _share(carol).status == share.NOT_LINKED


def test_a_user_is_linked_by_email_when_slack_knows_them(slack, installation, alice):
    slack.lookup = {"alice@dimagi.com": ALICE}
    assert _share(alice).status == share.SHARED
    assert SlackUserLink.objects.get(slack_user_id=ALICE).user == alice


def test_no_slack_install_is_refused(slack, alice):
    assert _share(alice).status == share.NOT_INSTALLED


def test_someone_elses_private_session_is_unreachable(slack, linked, alice, ws):
    bob = a_user("bob@dimagi.com")
    wsvc.ensure_member(ws, bob, WorkspaceMembership.EDITOR)
    private = Session.objects.create(workspace=ws, project="x", created_by=bob, title="bob's")
    assert _share(alice, session=private).status == share.NO_SESSION
    assert not slack.said("chat.postMessage")


def test_bot_not_in_channel_says_what_to_do(slack, linked, alice):
    slack.fail["chat.postMessage"] = "not_in_channel"
    result = _share(alice, channel="#general")
    assert result.status == share.NOT_IN_CHANNEL
    assert "invite" in result.message.lower() and "#general" in result.message


# ---- which session is "this one" ---------------------------------------------

def test_resolve_by_claude_session_id(alice, repo_session):
    assert share.resolve_session(alice, claude_session_id="claude-abc") == repo_session


def test_resolve_by_emdash_task(alice, repo_session):
    assert share.resolve_session(alice, emdash_task="slack", emdash_project="canopy-web") == repo_session


def test_resolve_never_crosses_a_workspace(alice, repo_session):
    other = a_workspace("elsewhere")
    stranger = a_user("stranger@example.com")
    wsvc.ensure_member(other, stranger, WorkspaceMembership.OWNER)
    assert share.resolve_session(stranger, claude_session_id="claude-abc") is None


def test_an_ambiguous_task_name_is_not_guessed(alice, repo_session, ws):
    twin = Session.objects.create(workspace=ws, project="canopy-web", origin=Session.ORIGIN_RUNNER, title="s")
    RunnerBinding.objects.create(session=twin, session_key="slack", emdash_project="canopy-web", host="other")
    with pytest.raises(share.Ambiguous):
        share.resolve_session(alice, emdash_task="slack", emdash_project="canopy-web")


def test_the_tool_is_served_by_the_mounted_server():
    from apps.mcp.server import mcp

    assert "share_session_to_slack" in {t.name for t in asyncio.run(mcp._list_tools())}


def test_the_tool_shares_as_the_pat_holder(slack, linked, alice, repo_session):
    from apps.mcp.tools.slack import _share_sync

    out = _share_sync(alice.pk, "C1", "doing things", "bind", "", "claude-abc", "", "", "")
    assert out["session_id"] == str(repo_session.id) and out["ts"] == POSTED_TS
    repo_session.refresh_from_db()
    assert repo_session.metadata["slack_thread_ts"] == POSTED_TS


def test_the_tool_raises_the_refusal_as_a_sentence(slack, linked, alice):
    from apps.mcp.tools.slack import _share_sync

    with pytest.raises(ValueError, match="nothing for the thread's replies"):
        _share_sync(alice.pk, "C1", "doing things", "bind", "", "no-such-session", "", "", "")


def test_the_agents_reply_is_relayed_into_the_bound_thread(slack, linked, alice, repo_session):
    from apps.harness.models import TurnEvent
    from apps.slack import relay

    _share(alice, session=repo_session, mode=share.BIND)
    mention("add a test", ts="1700001000.000100", thread_ts=POSTED_TS)
    turn = Turn.objects.get()
    row = TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": "Added it."})
    assert relay.relay(turn, [row]) == 1
    assert slack.said("chat.postMessage")[-1]["thread_ts"] == POSTED_TS


def test_the_share_command_itself_is_not_announced_as_elsewhere(slack, linked, alice, repo_session):
    from apps.slack import relay

    _share(alice, session=repo_session, mode=share.BIND)
    before = len(slack.said("chat.postMessage"))
    assert relay.notify_elsewhere(repo_session, ["/canopy:share-to-slack #dev bind"]) is False
    assert len(slack.said("chat.postMessage")) == before


def _asked_before_the_bind(session):
    """A turn enqueued, then the session bound — the chat page's share turn."""
    import datetime as dt

    turn = Turn.objects.create(chat_session=session, prompt="/canopy:share-to-slack #dev bind",
                               origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    Turn.objects.filter(pk=turn.pk).update(created_at=turn.created_at - dt.timedelta(seconds=5))
    turn.refresh_from_db()
    return turn


def test_the_share_turns_own_reply_does_not_open_the_thread(slack, linked, alice, repo_session):
    from apps.harness.models import TurnEvent
    from apps.slack import relay, status

    turn = _asked_before_the_bind(repo_session)
    _share(alice, session=repo_session, mode=share.BIND)
    repo_session.refresh_from_db()
    turn.refresh_from_db()
    posts = len(slack.said("chat.postMessage"))
    row = TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": "Shared to #dev."})
    assert relay.relay(turn, [row]) == 0
    assert status.post(turn) is None
    assert relay.relay_after_turn(repo_session, [(7, "Shared to #dev.")]) == 0
    assert len(slack.said("chat.postMessage")) == posts
