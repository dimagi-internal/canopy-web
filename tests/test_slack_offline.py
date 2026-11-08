"""Taking a channel thread's work offline, and reporting back (apps/slack/offline.py,
apps/canopy_sessions/lineage.py).

A long piece of work in a channel thread drowns the channel. `@canopy offline <ask>`
(or the agent, asked in words, through `take_session_offline`) moves the work into
a private thread in the asker's DMs: a child session (relation `branch`) seeded with
the channel conversation, that only they are in. The channel thread gets one line.
When they say so, the child reports back (`report_back_to_parent`): a turn in the
channel conversation carrying the outcome, asked in their name.
"""
from __future__ import annotations

import asyncio

import pytest
from django.test import Client

from apps.canopy_sessions import lineage
from apps.canopy_sessions import services as session_services
from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, Session
from apps.harness.models import Turn
from apps.slack import offline, services
from apps.slack.models import SlackUserLink
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user
from tests.access_helpers import editors_run
from tests.fixtures.slack import ALICE, BOB, TEAM, event, mention

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

ROOT_TS = "1700000000.000100"     # the mention that started the channel thread
DM_TS = "1700000700.000100"       # the post that opens the offline thread in alice's DMs
DM = f"D{ALICE}"                  # FakeSlack's DM channel id for alice


@pytest.fixture
def hal(hal):
    editors_run(hal)
    return hal


@pytest.fixture
def bob(ws, installation):
    user = a_user("bob@dimagi.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    SlackUserLink.objects.create(installation=installation, slack_user_id=BOB, user=user)
    return user


@pytest.fixture
def hal_bot(ws, hal):
    bot = a_user("hal@dimagi-ai.com")
    wsvc.ensure_member(ws, bot, WorkspaceMembership.EDITOR)
    hal.user = bot
    hal.save(update_fields=["user"])
    return bot


def _post(user, path, body):
    c = Client()
    c.force_login(user)
    return c.post(path, body, content_type="application/json")


def _running(turn: Turn) -> Turn:
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    return turn


def _channel_conversation(slack, bob=None) -> Session:
    """A channel thread hal is in, with some of its conversation said."""
    mention("hal build the KMC bot", ts=ROOT_TS)
    if bob is not None:
        mention("hal and the timeline?", user=BOB, ts="1700000060.000100", thread_ts=ROOT_TS)
    parent = Session.objects.get()
    Message.objects.create(session=parent, turn_index=10, role=Message.ASSISTANT,
                           plaintext="Drafted the registry measures.")
    return parent


# ---- `@canopy offline` in the thread -----------------------------------------------

def test_offline_moves_the_work_to_a_private_dm_thread_seeded_with_the_conversation(
        slack, linked, hal, alice, bob):
    parent = _channel_conversation(slack, bob)
    assert set(parent.participants.values_list("user__email", flat=True)) >= {"bob@dimagi.com"}
    parent_turns = Turn.objects.filter(chat_session=parent).count()
    slack.ts_seq = [DM_TS]

    mention("offline a clearer way to use AI on EOIs", ts="1700000080.000100", thread_ts=ROOT_TS)

    child = Session.objects.get(parent_session=parent)
    assert child.relation == lineage.BRANCH and child.created_by == alice
    # Only the person who took it offline — not the channel thread's other people.
    assert list(child.participants.values_list("user__email", flat=True)) == ["alice@dimagi.com"]
    assert child.metadata[services.SLACK_THREAD_KEY] == f"slack:{TEAM}:{DM}:{DM_TS}"
    assert child.metadata[offline.OFFLINE_FROM_KEY] == parent.metadata[services.SLACK_THREAD_KEY]

    turn = Turn.objects.get(chat_session=child)
    assert "Drafted the registry measures." in turn.prompt                # the conversation so far
    assert turn.prompt.rstrip().endswith("a clearer way to use AI on EOIs")
    assert "report_back_to_parent" in turn.prompt                          # it knows how to come back

    assert slack.said("conversations.open")[-1]["users"] == ALICE
    opener = next(p for p in slack.said("chat.postMessage") if p["channel"] == DM and "thread_ts" not in p)
    assert "Offline:" in opener["text"] and "report back" in opener["text"]
    note = [p for p in slack.said("chat.postMessage")
            if p["channel"] == "C1" and p.get("thread_ts") == ROOT_TS and "took this offline" in p["text"]]
    assert len(note) == 1 and f"<@{ALICE}>" in note[0]["text"]
    # The channel conversation was not asked anything.
    assert Turn.objects.filter(chat_session=parent).count() == parent_turns
    # Alice is told where it went, privately.
    assert "Took this offline" in slack.said("chat.postEphemeral")[-1]["text"]


def test_a_reply_in_the_dm_thread_reaches_the_offline_session(slack, linked, hal):
    parent = _channel_conversation(slack)
    slack.ts_seq = [DM_TS]
    mention("offline", ts="1700000080.000100", thread_ts=ROOT_TS)
    child = Session.objects.get(parent_session=parent)

    event({"type": "message", "channel_type": "im", "user": ALICE, "text": "try the shorter form",
           "ts": "1700000800.000100", "thread_ts": DM_TS, "channel": DM})

    latest = Turn.objects.order_by("-created_at").first()
    assert latest.chat_session_id == child.pk and latest.prompt == "try the shorter form"


def test_offline_outside_a_channel_thread_says_how_it_works(slack, linked, hal):
    mention("offline something")
    assert not Turn.objects.exists()
    assert "works inside a channel thread" in slack.said("chat.postEphemeral")[-1]["text"]


def test_someone_outside_the_conversation_may_not_take_it_offline(slack, linked, hal, bob):
    _channel_conversation(slack)
    mention("offline mine", user=BOB, ts="1700000080.000100", thread_ts=ROOT_TS)
    assert Session.objects.count() == 1
    assert "can take it offline" in slack.said("chat.postEphemeral")[-1]["text"]


# ---- naming the agent: `@canopy ace offline` ----------------------------------------

@pytest.fixture
def ace(ws):
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, slack_enabled=True)
    editors_run(agent)
    return agent


def _two_agents_in_a_thread(slack, ace) -> tuple[Session, Session]:
    mention("ace build the EOI rubric", ts=ROOT_TS)
    mention("hal and the timeline?", ts="1700000060.000100", thread_ts=ROOT_TS)
    return Session.objects.get(agent=ace), Session.objects.get(agent__slug="hal")


def test_naming_the_agent_takes_its_conversation_offline_not_the_latest(slack, linked, hal, ace):
    ace_talk, hal_talk = _two_agents_in_a_thread(slack, ace)
    assert hal_talk.created_at >= ace_talk.created_at       # bare `offline` would pick hal's
    slack.ts_seq = [DM_TS]

    mention("ace offline the EOI rubric", ts="1700000080.000100", thread_ts=ROOT_TS)

    child = Session.objects.get(parent_session__isnull=False)
    assert child.parent_session == ace_talk and child.agent == ace
    assert "the EOI rubric" in Turn.objects.get(chat_session=child).prompt


def test_naming_the_agent_branches_its_conversation(slack, linked, hal, ace):
    ace_talk, _hal_talk = _two_agents_in_a_thread(slack, ace)
    mention("ace branch my way", ts="1700000080.000100", thread_ts=ROOT_TS)
    assert Session.objects.get(parent_session__isnull=False).parent_session == ace_talk


def test_naming_an_agent_not_in_the_thread_says_so(slack, linked, hal, ace):
    _channel_conversation(slack)                             # hal's thread; ace is not in it
    mention("ace offline mine", ts="1700000080.000100", thread_ts=ROOT_TS)
    assert not Session.objects.filter(parent_session__isnull=False).exists()
    assert "`ace` has no conversation in this thread" in slack.said("chat.postEphemeral")[-1]["text"]


def test_outside_a_thread_naming_an_agent_just_asks_it(slack, linked, hal):
    mention("hal branch out the tests into their own module", ts=ROOT_TS)
    turn = Turn.objects.get()
    assert turn.prompt == "branch out the tests into their own module"


# ---- the agent takes it offline when asked in words --------------------------------

def test_an_agent_takes_its_thread_offline_for_the_person_who_asked(slack, linked, hal, alice, hal_bot):
    mention("hal let's build this offline and report back", ts=ROOT_TS)
    parent = Session.objects.get()
    turn = _running(Turn.objects.get(chat_session=parent))
    slack.ts_seq = [DM_TS]

    r = _post(hal_bot, "/api/canopy-sessions/slack/offline", {"turn_id": str(turn.pk), "brief": "EOIs"})

    assert r.status_code == 200, r.content
    body = r.json()
    child = Session.objects.get(pk=body["session_id"])
    assert body["from_session_id"] == str(parent.pk) and body["dm_channel"] == DM
    # Alice's work, not hal's: she owns it and is its only person.
    assert child.created_by == alice and child.parent_turn_id == turn.pk
    assert list(child.participants.values_list("user__email", flat=True)) == ["alice@dimagi.com"]


def test_an_agent_cannot_take_a_dm_offline(slack, linked, hal, hal_bot):
    event({"type": "message", "channel_type": "im", "user": ALICE, "text": "hal hi",
           "ts": "1700000100.000100", "channel": "D1"})
    turn = _running(Turn.objects.get())
    r = _post(hal_bot, "/api/canopy-sessions/slack/offline", {"turn_id": str(turn.pk)})
    assert r.status_code == 409


# ---- reporting back -----------------------------------------------------------------

def _offline_child(slack) -> tuple[Session, Session]:
    parent = _channel_conversation(slack)
    slack.ts_seq = [DM_TS]
    mention("offline EOIs", ts="1700000080.000100", thread_ts=ROOT_TS)
    return parent, Session.objects.get(parent_session=parent)


def test_the_agent_reports_back_to_the_channel_conversation_when_asked(slack, linked, hal, alice, hal_bot):
    parent, child = _offline_child(slack)
    child_turn = _running(Turn.objects.get(chat_session=child))
    before = Turn.objects.filter(chat_session=parent).count()

    r = _post(hal_bot, "/api/canopy-sessions/lineage/report-back",
              {"turn_id": str(child_turn.pk), "summary": "Built the EOI rubric: https://x/doc"})

    assert r.status_code == 200, r.content
    assert r.json()["parent_session_id"] == str(parent.pk)
    report = Turn.objects.filter(chat_session=parent).order_by("-created_at").first()
    assert Turn.objects.filter(chat_session=parent).count() == before + 1
    assert report.pk.hex == r.json()["turn_id"].replace("-", "")
    assert "REPORT BACK" in report.prompt and "Built the EOI rubric: https://x/doc" in report.prompt
    assert "finished" in report.prompt
    # Asked in alice's name — it is her work — not hal's.
    assert report.initiator_user == alice and report.initiator_kind == "user"
    child.refresh_from_db()
    (sent,) = child.metadata[lineage.REPORTS_KEY]
    assert sent["final"] is True and sent["by"] == alice.pk


def test_an_interim_report_says_the_work_carries_on(slack, linked, hal, alice):
    parent, child = _offline_child(slack)
    r = _post(alice, "/api/canopy-sessions/lineage/report-back",
              {"session_id": str(child.pk), "summary": "Halfway: rubric drafted.", "final": False})
    assert r.status_code == 200, r.content
    report = Turn.objects.filter(chat_session=parent).order_by("-created_at").first()
    assert "interim update" in report.prompt


def test_a_session_with_no_parent_has_nothing_to_report_back_to(slack, linked, hal, alice):
    mention("hal hi", ts=ROOT_TS)
    session = Session.objects.get()
    r = _post(alice, "/api/canopy-sessions/lineage/report-back",
              {"session_id": str(session.pk), "summary": "done"})
    assert r.status_code == 409


def test_a_report_needs_a_summary(slack, linked, hal, alice):
    _parent, child = _offline_child(slack)
    r = _post(alice, "/api/canopy-sessions/lineage/report-back", {"session_id": str(child.pk), "summary": " "})
    assert r.status_code == 422


def test_someone_who_cannot_open_the_child_cannot_report_for_it(slack, linked, hal, bob):
    _parent, child = _offline_child(slack)
    r = _post(bob, "/api/canopy-sessions/lineage/report-back", {"session_id": str(child.pk), "summary": "x"})
    assert r.status_code == 404


# ---- one lineage vocabulary ----------------------------------------------------------

def test_a_session_started_with_a_parent_is_a_dispatch_and_takes_its_people(ws, hal, alice, bob):
    parent = session_services.create_session(workspace=ws, created_by=alice, agent=hal, title="p")
    from apps.canopy_sessions.participants import ensure_participant

    ensure_participant(parent, bob, "editor")
    child = session_services.create_session(workspace=ws, created_by=alice, agent=hal, title="c",
                                            parent={"session": parent})
    assert child.relation == lineage.DISPATCH
    assert {"bob@dimagi.com"} <= set(child.participants.values_list("user__email", flat=True))
    branch = session_services.create_session(workspace=ws, created_by=alice, agent=hal, title="b",
                                             parent={"session": parent}, relation=lineage.BRANCH)
    assert list(branch.participants.values_list("user__email", flat=True)) == ["alice@dimagi.com"]


def test_a_slack_branch_is_recorded_as_a_branch(slack, linked, hal):
    mention("hal build it", ts=ROOT_TS)
    mention("branch my way", ts="1700000080.000100", thread_ts=ROOT_TS)
    parent = Session.objects.get(parent_session__isnull=True)
    assert Session.objects.get(parent_session=parent).relation == lineage.BRANCH


def test_offline_and_report_back_are_mcp_tools():
    from apps.mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp._list_tools())}
    assert {"take_session_offline", "report_back_to_parent"} <= names
