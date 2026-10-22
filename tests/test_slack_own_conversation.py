# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""An agent knows WHERE in Slack its conversation is, and can move it.

Two halves of one gap (2026-10-16): a Slack turn to ACE was asked to "post a new
clean thread in the channel" and had to ask which channel it was in. The caller
envelope named the session but not the Slack channel or thread, and the agent's
own login could not open the session (`GET /api/canopy-sessions/<id>` 404s: the
human who wrote in Slack owns it), so `share_session_to_slack` and
`detach_session_from_slack` 404'd too.

* the envelope's `conversation.slack` carries the channel id + name, the thread
  ts and its permalink — read from the session, never fetched while building;
* the agent's own login may act on the ONE conversation of a live turn it is
  running (`access.own_turn_session`), and `start_new_slack_thread` moves that
  conversation to a fresh thread in the same channel.
"""
from __future__ import annotations

import asyncio

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.harness import caller_context
from apps.harness.models import Turn
from apps.slack import share
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user

from tests.fixtures.slack import ALICE, TEAM, event, mention

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

ROOT_TS = "1700000000.000100"          # the mention that started the thread
NEW_TS = "1700000500.000100"           # what the new thread's opening post is given
BASE = "https://dimagi.slack.com"


def _link(channel: str, ts: str) -> str:
    return f"{BASE}/archives/{channel}/p{ts.replace('.', '')}"


@pytest.fixture
def named(slack):
    """Slack as an install WITH channels:read: it can say what C1 is called."""
    slack.channels = {"C1": "dev"}
    slack.permalink_base = BASE
    return slack


@pytest.fixture
def hal_bot(ws, hal):
    """Hal's own canopy login (`Agent.user`) — what Hal's PAT authenticates as."""
    bot = a_user("hal@dimagi-ai.com")
    wsvc.ensure_member(ws, bot, WorkspaceMembership.EDITOR)
    hal.user = bot
    hal.save(update_fields=["user"])
    return bot


def _slack_turn(status=Turn.RUNNING) -> Turn:
    """The turn alice's mention created, moved to `status` (claimed and running by default)."""
    turn = Turn.objects.order_by("-created_at").first()
    Turn.objects.filter(pk=turn.pk).update(status=status)
    turn.refresh_from_db()
    return turn


# ---- the envelope names the channel and the thread ---------------------------

def test_a_slack_turn_envelope_names_its_channel_and_thread(named, linked, hal):
    mention("hal post a clean thread in the channel")
    turn = _slack_turn()
    conv = caller_context.build(turn)["conversation"]
    assert conv["session_id"] == str(turn.chat_session_id)
    assert conv["slack"] == {
        "team_id": TEAM, "channel_id": "C1", "channel_name": "dev", "thread_ts": ROOT_TS,
        "permalink": _link("C1", ROOT_TS), "is_dm": False,
    }


def test_building_the_envelope_never_calls_slack(named, linked, hal):
    mention("hal hi")
    turn = _slack_turn()
    named.calls.clear()
    caller_context.build(turn)
    assert named.calls == []


def test_without_channels_read_the_name_is_null_but_the_thread_is_still_named(slack, linked, hal):
    mention("hal hi")
    slack_block = caller_context.build(_slack_turn())["conversation"]["slack"]
    assert slack_block["channel_id"] == "C1" and slack_block["thread_ts"] == ROOT_TS
    assert slack_block["channel_name"] is None
    # No permalink from the API: the web-client link to the same thread stands in.
    assert slack_block["permalink"] == f"https://app.slack.com/client/{TEAM}/C1/thread/C1-{ROOT_TS}"


def test_a_session_from_before_the_cache_learns_its_location_on_its_next_message(named, linked, hal):
    from tests.fixtures.slack import thread_reply

    mention("hal hi")
    session = Session.objects.get()
    meta = dict(session.metadata)
    meta.pop("slack_location", None)
    Session.objects.filter(pk=session.pk).update(metadata=meta)
    assert caller_context.build(_slack_turn(Turn.DONE))["conversation"]["slack"]["channel_name"] is None
    thread_reply("and another thing")
    assert caller_context.build(_slack_turn())["conversation"]["slack"]["channel_name"] == "dev"


def test_the_slash_command_names_the_channel_without_asking_slack(slack, linked, hal):
    from urllib.parse import urlencode

    from tests.fixtures.slack import _post

    body = urlencode({"team_id": TEAM, "channel_id": "C1", "channel_name": "dev", "user_id": ALICE,
                      "command": "/canopy", "text": "hal hello"}).encode()
    _post("/api/slack/commands", body, "application/x-www-form-urlencoded")
    assert caller_context.build(_slack_turn())["conversation"]["slack"]["channel_name"] == "dev"
    assert not slack.said("conversations.info")


def test_a_direct_message_is_a_dm_with_no_thread(named, linked, hal):
    event({"type": "message", "channel_type": "im", "user": ALICE, "text": "hal hi",
           "ts": "1700000100.000100", "channel": "D1"})
    block = caller_context.build(_slack_turn())["conversation"]["slack"]
    assert block["is_dm"] is True and block["channel_id"] == "D1"
    assert block["thread_ts"] is None and block["channel_name"] is None


def test_a_conversation_outside_slack_has_no_slack_block(ws, hal):
    from apps.harness import services

    turn, _ = services.enqueue_turn(agent=hal, origin=Turn.ORIGIN_EMAIL, idempotency_key="e-1",
                                    origin_ref={"from": "x@example.org", "subject": "hi"})
    assert caller_context.build(turn)["conversation"]["slack"] is None


# ---- the agent's own login may act on its own conversation, and only that ------

def test_the_agent_cannot_read_a_conversation_a_person_started(named, linked, hal_bot):
    mention("hal hi")
    assert not access.can_read(hal_bot, Session.objects.get())


def test_a_live_turn_gives_the_agent_its_one_conversation(named, linked, hal_bot):
    mention("hal hi")
    turn = _slack_turn()
    assert access.own_turn_session(hal_bot, turn.pk) == turn.chat_session


@pytest.mark.parametrize("status", [Turn.QUEUED, Turn.DONE, Turn.FAILED, Turn.CANCELLED])
def test_a_turn_not_in_flight_gives_nothing(named, linked, hal_bot, status):
    mention("hal hi")
    assert access.own_turn_session(hal_bot, _slack_turn(status).pk) is None


def test_another_agents_login_gets_nothing(named, linked, hal_bot, ws):
    mention("hal hi")
    turn = _slack_turn()
    other = a_user("eva@dimagi-ai.com")
    wsvc.ensure_member(ws, other, WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="eva", name="Eva", workspace=ws, user=other)
    assert access.own_turn_session(other, turn.pk) is None
    assert access.own_turn_session(a_user("nobody@example.com"), turn.pk) is None
    assert access.own_turn_session(hal_bot, "not-a-uuid") is None


# ---- share / detach with turn_id ---------------------------------------------

def test_the_share_tool_updates_the_turns_thread_as_the_agent(named, linked, hal_bot):
    from apps.mcp.tools.slack import _share_sync

    mention("hal hi")
    turn = _slack_turn()
    out = _share_sync(hal_bot.pk, "", "Halfway there.", "broadcast", "", "", "", "", "", str(turn.pk))
    assert out["status"] == share.UPDATED
    post = named.said("chat.postMessage")[-1]
    assert post["thread_ts"] == ROOT_TS and post["text"].startswith("*Update* from *Hal*")


def test_the_share_tool_without_a_live_turn_still_cannot_reach_it(named, linked, hal_bot):
    from apps.mcp.tools.slack import _share_sync

    mention("hal hi")
    turn = _slack_turn(Turn.DONE)
    with pytest.raises(ValueError):
        _share_sync(hal_bot.pk, "", "Halfway there.", "broadcast", "", "", "", "", "", str(turn.pk))


def test_an_agent_may_rebind_only_into_its_conversations_channel(named, linked, hal_bot):
    from apps.mcp.tools.slack import _share_sync

    mention("hal hi")
    turn = _slack_turn()
    session = turn.chat_session
    share.detach_session(hal_bot, session)
    with pytest.raises(ValueError, match="channel"):
        _share_sync(hal_bot.pk, "C9", "elsewhere", "bind", "", "", "", "", "", str(turn.pk))
    named.ts_seq = [NEW_TS]
    out = _share_sync(hal_bot.pk, "", "Fresh start.", "bind", "", "", "", "", "", str(turn.pk))
    assert out["channel"] == "C1" and out["ts"] == NEW_TS


def _post(user, path, body):
    c = Client()
    c.force_login(user)
    return c.post(path, body, content_type="application/json")


def test_the_detach_route_takes_the_turn(named, linked, hal_bot):
    mention("hal hi")
    turn = _slack_turn()
    r = _post(hal_bot, "/api/canopy-sessions/slack/detach", {"turn_id": str(turn.pk)})
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "detached"
    # Named by session id instead, the agent still cannot open it.
    r = _post(hal_bot, "/api/canopy-sessions/slack/detach", {"session_id": str(turn.chat_session_id)})
    assert r.status_code == 404


# ---- start_new_slack_thread: move the conversation ---------------------------

def _new_thread(user, turn, summary="Clean thread for the rollout plan."):
    return _post(user, "/api/canopy-sessions/slack/new-thread",
                 {"turn_id": str(turn.pk), "summary": summary})


def test_an_agent_moves_its_conversation_to_a_new_thread_in_the_same_channel(named, linked, hal_bot):
    mention("hal post a new clean thread in the channel")
    turn = _slack_turn()
    named.ts_seq = [NEW_TS]
    named.calls.clear()
    r = _new_thread(hal_bot, turn)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "moved"
    assert (body["channel"], body["channel_name"], body["thread_ts"]) == ("C1", "dev", NEW_TS)
    assert body["permalink"] == _link("C1", NEW_TS)
    assert body["previous"] == {"channel": "C1", "thread_ts": ROOT_TS, "permalink": _link("C1", ROOT_TS)}

    opener, note = named.said("chat.postMessage")[:2]
    assert opener["channel"] == "C1" and "thread_ts" not in opener        # top level: a new thread
    assert "Clean thread for the rollout plan." in opener["text"] and opener["username"] == "Hal"
    assert note["thread_ts"] == ROOT_TS and _link("C1", NEW_TS) in note["text"]

    session = turn.chat_session
    session.refresh_from_db()
    assert session.metadata["slack_thread"] == f"slack:{TEAM}:C1:{NEW_TS}"
    assert session.metadata[share.DETACHED_KEY][-1]["thread_ts"] == ROOT_TS
    # And the envelope now says where the conversation is.
    assert caller_context.build(turn)["conversation"]["slack"]["thread_ts"] == NEW_TS


def test_replies_follow_the_conversation_to_its_new_thread(named, linked, hal_bot):
    from tests.fixtures.slack import thread_reply

    mention("hal hi")
    turn = _slack_turn()
    named.ts_seq = [NEW_TS]
    _new_thread(hal_bot, turn)
    before = Turn.objects.count()
    thread_reply("old thread, ignored", thread_ts=ROOT_TS, ts="1700000600.000100")
    assert Turn.objects.count() == before
    thread_reply("new thread, heard", thread_ts=NEW_TS, ts="1700000700.000100")
    latest = Turn.objects.order_by("-created_at").first()
    assert latest.chat_session_id == turn.chat_session_id and latest.prompt == "new thread, heard"


def test_moving_needs_a_live_turn_of_your_own(named, linked, hal_bot, alice):
    mention("hal hi")
    done = _slack_turn(Turn.DONE)
    assert _new_thread(hal_bot, done).status_code == 404
    assert not Session.objects.get().metadata["slack_thread"].endswith(NEW_TS)


def test_the_person_who_owns_the_conversation_can_move_it_too(named, linked, hal, alice):
    mention("hal hi")
    turn = _slack_turn()
    named.ts_seq = [NEW_TS]
    r = _post(alice, "/api/canopy-sessions/slack/new-thread",
              {"session_id": str(turn.chat_session_id), "summary": "moving on"})
    assert r.status_code == 200, r.content
    assert r.json()["thread_ts"] == NEW_TS


def test_a_conversation_with_no_thread_cannot_be_moved(named, linked, hal_bot):
    mention("hal hi")
    turn = _slack_turn()
    share.detach_session(hal_bot, turn.chat_session)
    assert _new_thread(hal_bot, turn).status_code == 409


def test_a_direct_message_cannot_be_moved_to_a_thread(named, linked, hal_bot):
    event({"type": "message", "channel_type": "im", "user": ALICE, "text": "hal hi",
           "ts": "1700000100.000100", "channel": "D1"})
    assert _new_thread(hal_bot, _slack_turn()).status_code == 409


def test_a_summary_is_required(named, linked, hal_bot):
    mention("hal hi")
    assert _new_thread(hal_bot, _slack_turn(), summary="  ").status_code == 422


def test_the_new_thread_route_is_an_mcp_tool():
    from apps.mcp.server import mcp

    assert "start_new_slack_thread" in {t.name for t in asyncio.run(mcp._list_tools())}
