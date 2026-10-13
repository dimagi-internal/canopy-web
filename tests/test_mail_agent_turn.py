from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.mail import agent_mail, parse, ses
from apps.mail.models import MailDomain, MailMessage, MailRoute
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def route(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    monkeypatch.setattr(ses, "keep", lambda k: "kept/" + k.split("/", 1)[-1])
    monkeypatch.setattr(ses, "put_object", lambda *a, **k: None)
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=u)
    return MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)


def _email_turns():
    return Turn.objects.filter(origin=Turn.ORIGIN_EMAIL)


def test_agent_mail_is_stored_and_becomes_one_email_turn(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    m = MailMessage.objects.get()
    assert m.raw_key == "kept/abc" and m.thread.message_count == 1
    t = _email_turns().get()
    assert t.origin_ref["thread_id"] == str(m.thread.uuid)
    assert t.origin_ref["backend"] == "ses"
    assert t.prompt == f"/mada:turn --thread {m.thread.uuid}"


def test_redelivery_is_a_duplicate_not_a_second_turn(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    agent_mail.deliver(route, p, "incoming/abc")
    assert agent_mail.deliver(route, p, "incoming/abc") == "duplicate"
    assert _email_turns().count() == 1


def test_automated_mail_is_stored_without_a_turn(route):
    p = parse.parse((FIX / "auto.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/a2") == "stored:automated"
    assert _email_turns().count() == 0 and MailMessage.objects.count() == 1


def test_one_email_to_two_agents_wakes_both(route):
    bob = Agent.objects.create(slug="mbob", name="Bob", workspace=route.domain.workspace,
                               owner=route.agent.owner)
    rb = MailRoute.objects.create(domain=route.domain, local_part="bob", kind="agent", agent=bob)
    p = parse.parse((FIX / "plain.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    assert agent_mail.deliver(rb, p, "incoming/abc") == "agent"
    assert _email_turns().count() == 2


def test_the_agents_own_mail_never_wakes_it(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    p.from_addr = "ada@marshellis.com"
    assert agent_mail.deliver(route, p, "incoming/a3") == "stored:own-mail"
    assert _email_turns().count() == 0
