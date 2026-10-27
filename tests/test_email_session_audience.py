"""An email-born session is visible to the people on its email thread.

Everyone on the thread — From, To and Cc of each inbound message, the recipients
of the agent's own replies — who is a canopy MEMBER of the session's workspace is
an editor participant, added as each message arrives. Only on this message's
proof: an unaligned From admits nobody, not even its To/Cc. Never a non-member,
an agent's login; never into a sensitive conversation.
"""
from __future__ import annotations

import types
import uuid

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.harness import services, session_sync
from apps.harness.channels import email as email_channel
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


def _hdr(result: str, domain: str = "dimagi.com"):
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass; spf=pass; dmarc={result} header.from={domain}"}]


def _person(name, ws=None):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    if ws is not None:
        M.objects.create(user=u, workspace=ws, role=M.EDITOR)
    return u


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    EmailAddress.objects.create(user=op, email=op.email, verified=True, primary=True)
    sam, bob, cat = _person("sam", ws), _person("bob", ws), _person("cat", ws)
    outsider = _person("x")                      # a canopy user, not in this workspace
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    bot = User.objects.create_user("ace-bot", "ace@dimagi-ai.com")
    M.objects.create(user=bot, workspace=ws, role=M.EDITOR)
    agent.user = bot
    agent.save(update_fields=["user"])
    return {"op": op, "sam": sam, "bob": bob, "cat": cat, "x": outsider, "ws": ws,
            "agent": agent, "bot": bot}


def _email(agent, frm, key, headers=None, *, to=None, cc=None, thread="t1"):
    ref = {"from": frm, "thread_id": thread, "subject": "Plan"}
    if headers is not None:
        ref["headers"] = headers
    if to is not None:
        ref["to"] = to
    if cc is not None:
        ref["cc"] = cc
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                                 origin_ref=ref)
    return t


def _people(session):
    return set(SessionParticipant.objects.filter(session=session).values_list("user__username", flat=True))


def test_everyone_on_an_aligned_message_who_is_a_member_is_in(w):
    t = _email(w["agent"], "Sam <sam@dimagi.com>", "1", _hdr("pass"),
               to=["ace@dimagi-ai.com", "bob@dimagi.com"], cc=["x@dimagi.com", "fatima@llo.org"])
    session = t.chat_session
    assert session is not None
    # sam (sender) and bob (To); not the outsider, not the stranger, not the agent's login
    assert _people(session) == {"sam", "bob"}
    assert SessionParticipant.objects.get(session=session, user=w["bob"]).role == SessionParticipant.EDITOR
    assert Session.objects.filter(access.visible_session_q(w["bob"]), pk=session.pk).exists()
    assert not Session.objects.filter(access.visible_session_q(w["cat"]), pk=session.pk).exists()


def test_header_strings_are_parsed_like_lists(w):
    """The Gmail runner forwards To/Cc as written: one header string each."""
    t = _email(w["agent"], "sam@dimagi.com", "1", _hdr("pass"),
               to="Ace <ace@dimagi-ai.com>, Bob <bob@dimagi.com>", cc="cat@dimagi.com")
    assert _people(t.chat_session) == {"sam", "bob", "cat"}


@pytest.mark.parametrize("headers", [None, _hdr("fail"), _hdr("pass", domain="evil.example")])
def test_an_unaligned_message_admits_nobody_not_even_its_cc(w, headers):
    """A forged From could otherwise Cc a member into a thread and hand them its history."""
    t = _email(w["agent"], "op@dimagi.com", "1", headers, to=["bob@dimagi.com"], cc=["cat@dimagi.com"])
    assert _people(t.chat_session) == set()


def test_an_aligned_outside_sender_brings_in_the_members_they_addressed(w):
    t = _email(w["agent"], "fatima@llo.org", "1", _hdr("pass", domain="llo.org"),
               to=["ace@dimagi-ai.com"], cc=["sam@dimagi.com"])
    assert _people(t.chat_session) == {"sam"}


def test_someone_copied_in_later_joins_then(w):
    first = _email(w["agent"], "sam@dimagi.com", "1", _hdr("pass"), to=["ace@dimagi-ai.com"])
    assert _people(first.chat_session) == {"sam"}
    second = _email(w["agent"], "sam@dimagi.com", "2", _hdr("pass"),
                    to=["ace@dimagi-ai.com"], cc=["cat@dimagi.com"])
    assert second.chat_session_id == first.chat_session_id
    assert _people(second.chat_session) == {"sam", "cat"}


def test_an_unverified_address_or_ambiguous_holder_is_not_admitted(w):
    EmailAddress.objects.filter(user=w["cat"]).update(verified=False)
    t = _email(w["agent"], "sam@dimagi.com", "1", _hdr("pass"), cc=["cat@dimagi.com"])
    assert _people(t.chat_session) == {"sam"}


def test_never_into_a_sensitive_conversation(w):
    session = session_sync.email_thread_session(w["agent"], "t1", "Plan")
    session_services.add_runner_requirements(session, {"sensitive_data"})
    assert email_channel.admit_addresses(session, ["bob@dimagi.com"]) == []
    _email(w["agent"], "sam@dimagi.com", "1", _hdr("pass"), cc=["bob@dimagi.com"])
    assert _people(session) == set()


def test_the_agents_own_replies_admit_their_recipients(w):
    """`apps/mail` sending on a thread: the agent chose them, no proof to check."""
    from apps.mail import agent_mail

    thread_uuid = uuid.uuid4()
    session = session_sync.email_thread_session(w["agent"], str(thread_uuid), "Plan")
    route = types.SimpleNamespace(agent_id=w["agent"].pk)
    out = types.SimpleNamespace(pk=1, thread=types.SimpleNamespace(uuid=thread_uuid),
                                to=["bob@dimagi.com", "fatima@llo.org"], cc=["x@dimagi.com"])
    agent_mail._admit_recipients(route, out)
    assert _people(session) == {"bob"}
