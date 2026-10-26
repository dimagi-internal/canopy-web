"""A Slack-bound session's people are the people in its Slack conversation.

Every linked canopy member of the workspace in the channel (or DM) is an EDITOR
participant — as in Slack, anyone there can read and write the thread. Kept in step
with Slack: a member seen in the channel joins (on the next message or the sweep),
one who left is removed — but only someone the CHANNEL brought in, and never on an
answer Slack did not give. Contacts, guests and non-members gain nothing; never into
a sensitive conversation.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache

from apps.canopy_sessions import access
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.slack import audience
from apps.slack.models import SlackAudienceMember
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user
from tests.fixtures.slack import ALICE, BOB, BOT, TEAM, mention, thread_reply

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured", "linked", "hal")]

GUEST, DAVE = "UGUEST", "UDAVE"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _person(ws, email, *, member=True):
    user = a_user(email)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    if member:
        wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def people(slack, ws):
    bob = _person(ws, "bob@dimagi.com")
    carol = _person(ws, "carol@dimagi.com")
    dave = _person(ws, "dave@dimagi.com", member=False)
    # A guest whose profile claims a member's address: a guest is a contact, whatever it says.
    slack.users[GUEST] = {"name": "g", "team_id": TEAM, "is_restricted": True,
                          "profile": {"email": "carol@dimagi.com"}}
    slack.users[DAVE] = {"name": "dave", "team_id": TEAM, "profile": {"email": "dave@dimagi.com"}}
    slack.members["C1"] = [BOT, ALICE, BOB, GUEST, DAVE]
    return {"bob": bob, "carol": carol, "dave": dave}


def _in(session):
    return set(SessionParticipant.objects.filter(session=session).exclude(role=SessionParticipant.OWNER)
               .values_list("user__email", flat=True))


def _sees(user, session) -> bool:
    return Session.objects.filter(access.visible_session_q(user), pk=session.pk).exists()


def test_every_linked_member_in_the_channel_is_an_editor(people):
    mention("hal what's the plan?")
    session = Session.objects.get()
    assert _in(session) == {"bob@dimagi.com"}          # alice owns it; guest, outsider, carol out
    assert SessionParticipant.objects.get(session=session, user=people["bob"]).role == \
        SessionParticipant.EDITOR
    assert access.role_for(people["bob"], session) == SessionParticipant.EDITOR
    assert not _sees(people["carol"], session)          # a member, but not in the channel


def test_leaving_the_channel_leaves_the_session(slack, people):
    mention("hal what's the plan?")
    session = Session.objects.get()
    slack.members["C1"] = [BOT, ALICE]
    audience.forget(TEAM, "C1")
    audience.sweep()
    assert _in(session) == set()
    assert not SlackAudienceMember.objects.filter(session=session).exists()
    assert not _sees(people["bob"], session)


def test_someone_the_owner_shared_with_is_never_removed_by_slack(slack, people):
    mention("hal what's the plan?")
    session = Session.objects.get()
    SessionParticipant.objects.create(session=session, user=people["carol"], role=SessionParticipant.EDITOR)
    audience.forget(TEAM, "C1")
    audience.sweep()
    assert "carol@dimagi.com" in _in(session)


def test_a_member_who_joins_the_channel_joins_on_the_next_message(slack, people):
    slack.members["C1"] = [BOT, ALICE]
    mention("hal what's the plan?")
    session = Session.objects.get()
    assert _in(session) == set()
    slack.members["C1"] = [BOT, ALICE, BOB]
    audience.forget(TEAM, "C1")
    thread_reply("and the budget?")
    assert _in(session) == {"bob@dimagi.com"}


def test_slack_not_answering_removes_nobody(slack, people):
    mention("hal what's the plan?")
    session = Session.objects.get()
    slack.fail["conversations.members"] = "missing_scope"
    audience.forget(TEAM, "C1")
    audience.sweep()
    assert _in(session) == {"bob@dimagi.com"}


def test_a_list_without_the_bot_is_not_acted_on(slack, people):
    mention("hal what's the plan?")
    session = Session.objects.get()
    slack.members["C1"] = [ALICE]
    audience.forget(TEAM, "C1")
    audience.sweep()
    assert _in(session) == {"bob@dimagi.com"}


def test_the_poster_still_joins_when_slack_cannot_list(slack, people):
    slack.members.pop("C1")
    mention("hal what's the plan?")
    session = Session.objects.get()
    thread_reply("me too", user=BOB)
    assert _in(session) == {"bob@dimagi.com"}


def test_slack_is_asked_once_per_conversation_within_the_cache(slack, people):
    mention("hal what's the plan?")
    thread_reply("one")
    thread_reply("two", ts="1700000060.000100")
    assert len(slack.said("conversations.members")) == 1


def test_never_into_a_sensitive_conversation(slack, people, installation):
    slack.members["C1"] = [BOT, ALICE]
    mention("hal what's the plan?")
    session = Session.objects.get()
    session_services.add_runner_requirements(session, {"sensitive_data"})
    slack.members["C1"] = [BOT, ALICE, BOB]
    audience.forget(TEAM, "C1")
    audience.sync(session, installation)
    assert _in(session) == set()
