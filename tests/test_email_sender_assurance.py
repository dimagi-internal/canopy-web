"""An email turn is graded on OUR receiver's verdict, and a blocked sender is refused.

Two gaps between the contacts design and the email channel it was built for:

* The runner shipped only `from`/`subject`/`thread_id`, so every emailer was
  graded `none` and the DMARC ladder never saw input. It now ships the newest
  message's `Authentication-Results` headers; which receiver's verdict counts is
  canopy's setting, never the poster's say-so.
* `Contact.blocked_at` was honoured by Slack and by contact tokens, and by
  nothing on the email path — the one lever for refusing a person did nothing on
  the channel that motivated it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.contacts import services as contacts
from apps.contacts.models import Contact
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.agents.testing import admit_contacts

pytestmark = pytest.mark.django_db

PASS_ALL = ("mx.google.com; dkim=pass header.i=@llo-foo.org; spf=pass "
            "smtp.mailfrom=llo-foo.org; dmarc=pass (p=NONE) header.from=llo-foo.org")


@pytest.fixture()
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=owner)
    admit_contacts(agent)
    return owner, ws, agent


def _email(agent, key="e-1", **ref):
    base = {"from": "fatima@llo-foo.org", "thread_id": "t-1", "subject": "hi"}
    return services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL,
                                 idempotency_key=key, origin_ref={**base, **ref})


# --- the grade --------------------------------------------------------------------

def test_shipped_headers_grade_the_sender_and_the_turn(ctx):
    _o, _ws, agent = ctx
    turn, created = _email(agent, headers=[
        {"name": "Authentication-Results", "value": PASS_ALL}])
    assert created
    assert turn.initiator_kind == who.CONTACT
    assert turn.initiator_assurance == Contact.AUTH_DMARC
    assert turn.initiator_contact.auth_result == Contact.AUTH_DMARC


def test_the_poster_cannot_name_the_receiver_it_is_graded_by(ctx):
    """A payload naming its own `authserv_id` would let whoever posts the turn
    pick which header counts — including one they wrote into the message."""
    _o, _ws, agent = ctx
    forged = "evil.example; dmarc=pass header.from=llo-foo.org"
    turn, _ = _email(agent, authserv_id="evil.example",
                     headers=[{"name": "Authentication-Results", "value": forged}])
    assert turn.initiator_assurance == Contact.AUTH_NONE


def test_a_predigested_verdict_string_is_not_evidence(ctx):
    """The old `authentication_results` field skipped the provenance check
    entirely: it was graded as if it had come from our receiver."""
    _o, _ws, agent = ctx
    turn, _ = _email(agent, authentication_results=PASS_ALL)
    assert turn.initiator_assurance == Contact.AUTH_NONE


@override_settings(INBOUND_EMAIL_AUTHSERV_ID="mx.example.net")
def test_the_trusted_receiver_is_a_setting(ctx):
    _o, _ws, agent = ctx
    turn, _ = _email(agent, headers=[
        {"name": "Authentication-Results", "value": PASS_ALL}])
    # Google's header is now somebody else's, so it proves nothing.
    assert turn.initiator_assurance == Contact.AUTH_NONE


def test_an_old_runner_with_no_headers_is_graded_none_not_rejected(ctx):
    _o, _ws, agent = ctx
    turn, created = _email(agent)
    assert created and turn.status == Turn.QUEUED
    assert turn.initiator_assurance == Contact.AUTH_NONE


# --- blocking ---------------------------------------------------------------------

def test_a_blocked_sender_gets_a_cancelled_turn_that_says_why(ctx):
    _o, ws, agent = ctx
    c = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    contacts.block(c, reason="spam")

    turn, created = _email(agent)
    assert created
    assert turn.status == Turn.CANCELLED
    assert turn.finished_at is not None
    assert "blocked" in turn.result_note and "spam" in turn.result_note
    # Still says who it was from — the refusal is about someone.
    assert turn.initiator_contact_id == c.pk
    # Not a conversation: no email-thread session was opened for it.
    assert turn.chat_session_id is None


def test_a_blocked_senders_repoll_is_a_noop_not_a_second_refusal(ctx):
    _o, ws, agent = ctx
    contacts.block(contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org"))
    first, _ = _email(agent, key="same")
    again, created = _email(agent, key="same")
    assert not created and again.pk == first.pk
    assert Turn.objects.count() == 1


def test_a_refused_turn_is_a_normal_201_for_the_runner(ctx):
    """A 4xx here would raise in the runner and abort its whole mailbox poll,
    stalling every other sender's mail behind one blocked address."""
    owner, ws, _agent = ctx
    contacts.block(contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org"))
    raw, _ = PersonalToken.create_for_user(user=owner, label="runner")
    r = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").post(
        "/api/harness/turns/",
        {"agent_slug": "ace", "origin": "email", "idempotency_key": "email-x",
         "origin_ref": {"from": "fatima@llo-foo.org", "thread_id": "t-9"}},
        content_type="application/json",
    )
    assert r.status_code == 201, r.content
    assert r.json()["status"] == Turn.CANCELLED


def test_unblocking_lets_the_next_message_through(ctx):
    _o, ws, agent = ctx
    c = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    contacts.block(c)
    _email(agent, key="a")
    contacts.unblock(c)
    turn, _ = _email(agent, key="b")
    assert turn.status == Turn.QUEUED
