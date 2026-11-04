"""The people a verified email was addressed to may read the work session it ran in.

The thread's people were participants of its anchor, which holds nothing to read, and
not of the work session, so a member who emailed an agent they don't administer could
never open the answer. They join the work session as viewers, from that one message.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.canopy_sessions.staleness import active
from apps.contacts.models import Contact
from apps.harness import session_sync
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace
from apps.workspaces.models import WorkspaceMembership as M

pytestmark = pytest.mark.django_db

THREAD = "1a2b3c4d5e6f7a8b"
ALIGNED = sorted(Contact.EMAIL_ALIGNED)[0]


def _person(name, ws=None):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    if ws is not None:
        M.objects.create(user=u, workspace=ws, role=M.EDITOR)
    return u


@pytest.fixture
def w():
    op = _person("op")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op)
    bot = User.objects.create_user("ace-bot", "ace@dimagi-ai.com")
    M.objects.create(user=bot, workspace=ws, role=M.EDITOR)
    agent.user = bot
    agent.save(update_fields=["user"])
    runner = Runner.objects.create(
        name="cloud", kind=Runner.CLOUD, workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), owner=op, host="cloud-1", capabilities={"sessions": True})

    class W:
        pass

    x = W()
    x.op, x.ws, x.agent, x.bot, x.runner = op, ws, agent, bot, runner
    x.sam, x.bob, x.outsider = _person("sam", ws), _person("bob", ws), _person("zed")
    x.anchor = session_sync.email_thread_session(agent, THREAD, "A thread")
    return x


def _mail(w, sender, *, name="m1", to=(), cc=(), assurance=ALIGNED, kind="user", thread=THREAD):
    return Turn.objects.create(
        chat_session=w.anchor, origin=Turn.ORIGIN_EMAIL, idempotency_key=name, status=Turn.RUNNING,
        claimed_by=w.runner, initiator_kind=kind, initiator_user=sender if kind == "user" else None,
        initiator_assurance=assurance,
        origin_ref={"thread_id": thread, "from": sender.email, "to": list(to), "cc": list(cc)})


def _record(w, key=THREAD, *, turn=None, session_key="s1"):
    return session_sync.record_session(w.agent, key, runner=w.runner, session_key=session_key,
                                       title="t", turn=turn).session


def _role(session, user):
    return (SessionParticipant.objects.filter(session=session, user=user)
            .values_list("role", flat=True).first())


def test_a_member_who_emailed_can_read_the_work_session_it_ran_in(w):
    _mail(w, w.sam)
    session = _record(w)
    assert access.can_read(w.sam, session)
    assert active(access.readable_sessions(w.sam)).filter(pk=session.pk).exists()


def test_they_can_read_it_but_not_type_into_it(w):
    _mail(w, w.sam)
    session = _record(w)
    assert _role(session, w.sam) == SessionParticipant.VIEWER
    assert not access.can_write(w.sam, session)


def test_the_people_on_to_and_cc_join_too(w):
    _mail(w, w.sam, to=["ace@dimagi-ai.com", "bob@dimagi.com"], cc=["Op <op@dimagi.com>"])
    session = _record(w)
    assert access.can_read(w.bob, session) and _role(session, w.op) == SessionParticipant.VIEWER


def test_a_workspace_owner_on_the_thread_is_a_viewer_row_not_a_session_owner(w):
    """The session role is not the person's role in the workspace."""
    owner = _person("wsowner")
    M.objects.create(user=owner, workspace=w.ws, role=M.OWNER)
    _mail(w, w.sam, cc=[owner.email])
    assert _role(_record(w), owner) == SessionParticipant.VIEWER


def test_the_agents_admin_keeps_writing_with_a_viewer_row(w):
    _mail(w, w.op, cc=["sam@dimagi.com"])
    session = _record(w)
    assert _role(session, w.op) == SessionParticipant.VIEWER
    assert access.can_write(w.op, session)


def test_an_unaligned_message_admits_nobody(w):
    _mail(w, w.sam, cc=["bob@dimagi.com"], assurance="none")
    session = _record(w)
    assert not access.can_read(w.sam, session) and not access.can_read(w.bob, session)


def test_a_non_member_and_the_agents_login_are_not_admitted(w):
    _mail(w, w.sam, to=[w.bot.email], cc=[w.outsider.email])
    session = _record(w)
    assert _role(session, w.outsider) is None and _role(session, w.bot) is None


@pytest.mark.parametrize("kind", ["system", "agent"])
def test_machine_mail_admits_nobody(w, kind):
    _mail(w, w.sam, cc=["bob@dimagi.com"], kind=kind)
    session = _record(w)
    assert _role(session, w.bob) is None and _role(session, w.sam) is None


def test_a_confined_callers_cc_does_not_open_the_admins_session_on_the_thread(w):
    """The admin's mail runs in the thread's session, the caller's in `#ask`."""
    first = _mail(w, w.op, name="admin")
    full = _record(w, session_key="full")
    first.status = Turn.DONE
    first.save(update_fields=["status"])
    _mail(w, w.sam, name="caller", cc=["bob@dimagi.com"])
    confined = _record(w, f"{THREAD}#ask", session_key="cx-1")
    assert full.pk != confined.pk
    assert access.can_read(w.sam, confined) and access.can_read(w.bob, confined)
    assert not access.can_read(w.sam, full) and not access.can_read(w.bob, full)


def test_a_row_someone_already_has_keeps_its_role(w):
    first = _mail(w, w.sam, name="m1")
    session = _record(w)
    SessionParticipant.objects.filter(session=session, user=w.sam).update(role=SessionParticipant.EDITOR)
    first.status = Turn.DONE
    first.save(update_fields=["status"])
    _mail(w, w.sam, name="m2")
    _record(w)
    assert _role(session, w.sam) == SessionParticipant.EDITOR


def test_a_turn_linked_to_another_session_admits_nobody_to_this_one(w):
    turn = _mail(w, w.sam)
    other = Session.objects.create(agent=w.agent, workspace=w.ws, origin=Session.ORIGIN_RUNNER, title="o")
    Turn.objects.filter(pk=turn.pk).update(canopy_session=other)
    session = _record(w)
    assert session.pk != other.pk and _role(session, w.sam) is None


def test_a_failed_admission_never_costs_the_record(w, monkeypatch):
    from apps.harness.channels import email as email_channel

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(email_channel, "admit_addresses", boom)
    _mail(w, w.sam)
    assert _record(w).pk
    assert Turn.objects.get(idempotency_key="m1").canopy_session_id


def test_a_sensitive_work_session_takes_nobody(w, monkeypatch):
    from apps.harness import sensitivity

    monkeypatch.setattr(sensitivity, "is_sensitive", lambda s: True)
    _mail(w, w.sam)
    assert _role(_record(w), w.sam) is None
