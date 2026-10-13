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


def test_two_messages_on_one_thread_get_distinct_turn_keys(route):
    p1 = parse.parse((FIX / "plain.eml").read_bytes())
    p2 = parse.parse((FIX / "reply.eml").read_bytes())
    agent_mail.deliver(route, p1, "incoming/a")
    agent_mail.deliver(route, p2, "incoming/b")
    keys = list(_email_turns().values_list("idempotency_key", flat=True))
    assert len(keys) == len(set(keys)) == 2
    assert {str(m.pk) for m in MailMessage.objects.all()} == {k.rsplit("-", 1)[1] for k in keys}


def test_a_crash_before_enqueue_is_healed_by_redelivery(route, monkeypatch):
    from apps.harness import services

    p = parse.parse((FIX / "plain.eml").read_bytes())
    real = services.enqueue_turn

    def boom(**kw):
        raise RuntimeError("crash")

    monkeypatch.setattr(services, "enqueue_turn", boom)
    with pytest.raises(RuntimeError):
        agent_mail.deliver(route, p, "incoming/abc")
    assert MailMessage.objects.count() == 1 and _email_turns().count() == 0
    monkeypatch.setattr(services, "enqueue_turn", real)
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    assert _email_turns().count() == 1
    assert agent_mail.deliver(route, p, "incoming/abc") == "duplicate"
    assert _email_turns().count() == 1


def test_quiet_mail_does_not_mark_the_thread_unread(route):
    p = parse.parse((FIX / "auto.eml").read_bytes())
    agent_mail.deliver(route, p, "incoming/a2")
    assert MailMessage.objects.get().thread.unread is False


def test_ses_authentication_results_grade_the_sender(route):
    from apps.contacts.models import Contact

    p = parse.parse((FIX / "plain.eml").read_bytes())
    p.headers = list(p.headers) + [(
        "Authentication-Results",
        "amazonses.com; spf=pass smtp.mailfrom=example.org; "
        "dkim=pass header.d=example.org; dmarc=pass header.from=example.org")]
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    c = Contact.objects.get(email="someone@example.org")
    assert c.auth_result != Contact.AUTH_NONE


def test_mail_with_nul_bytes_is_stored_not_lost(route):
    from tests.test_mail_parse import _NUL_MAIL

    p = parse.parse(_NUL_MAIL)
    assert agent_mail.deliver(route, p, "incoming/nul") == "agent"
    m = MailMessage.objects.get()
    assert m.subject == "hithere" and "body" in m.text and "\x00" not in m.message_id
    assert m.attachments.get().filename == "a.pdf"


def test_reply_to_is_stored(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    p.reply_to = ["list@example.org"]
    agent_mail.deliver(route, p, "incoming/abc")
    assert MailMessage.objects.get().reply_to == ["list@example.org"]
