from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import ses
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or f"ses-{len(sent)}")
    owner = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    viewer = User.objects.create_user("v", "v@x.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role="viewer")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified", daily_send_cap=2)
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="Hello", message_count=1)
    MailMessage.objects.create(thread=t, direction="in", message_id="<in-1@x.org>",
                               from_addr="x@x.org", subject="Hello", text="hi")
    return type("W", (), dict(owner=owner, viewer=viewer, d=d, t=t, r=r, sent=sent))


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def _reply(w, body, user=None):
    return _c(user or w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", body,
                                    content_type="application/json")


def test_resolve_names_the_ses_backend_for_members_only(w):
    r = _c(w.owner).get("/api/mail/resolve", {"address": "ada@marshellis.com"})
    assert r.json() == {"backend": "ses", "workspace": "marshellis", "agent": "mada"}
    stranger = User.objects.create_user("s", "s@s.com", "pw")
    assert _c(stranger).get("/api/mail/resolve", {"address": "ada@marshellis.com"}).status_code == 404
    assert _c(w.owner).get("/api/mail/resolve", {"address": "eva@dimagi-ai.com"}).status_code == 404


def test_reply_threads_and_sends_as_the_agent(w):
    r = _reply(w, {"text": "Thanks!"})
    assert r.status_code == 200, r.content
    sent = w.sent[0]
    assert sent["from_addr"] == "ada@marshellis.com" and sent["to"] == ["x@x.org"]
    assert b"In-Reply-To: <in-1@x.org>" in sent["raw"]
    out = MailMessage.objects.get(direction="out")
    assert out.thread == w.t and out.ses_message_id == "ses-1" and out.delivery_status == "sent"
    assert out.mailbox == "ada@marshellis.com"


def test_viewer_can_read_but_not_send(w):
    assert _c(w.viewer).get(f"/api/mail/threads/marshellis/{w.t.uuid}").status_code == 200
    assert _reply(w, {"text": "x"}, w.viewer).status_code == 403


def test_daily_cap_is_enforced(w):
    for _ in range(2):
        assert _reply(w, {"text": "x"}).status_code == 200
    assert _reply(w, {"text": "x"}).status_code == 429


def test_suspended_domain_refuses_sends(w):
    MailDomain.objects.filter(pk=w.d.pk).update(status="suspended", suspended_reason="bounces")
    r = _reply(w, {"text": "x"})
    assert r.status_code == 409 and "bounces" in r.json()["detail"]


def test_reply_with_no_inbound_has_no_one_to_reply_to(w):
    w.t.messages.all().delete()
    assert _reply(w, {"text": "x"}).status_code == 422


def test_recipients_must_be_addresses_and_headers_cannot_be_injected(w):
    assert _reply(w, {"text": "x", "to": ["not an address"]}).status_code == 422
    assert _reply(w, {"text": "x", "to": ["a@b.org\r\nBcc: evil@e.org"]}).status_code == 422
    r = _c(w.owner).post("/api/mail/send/marshellis",
                         {"agent": "mada", "to": ["a@b.org"], "subject": "Hi\r\nBcc: evil@e.org", "text": "x"},
                         content_type="application/json")
    assert r.status_code == 200, r.content
    assert b"\r\nBcc:" not in w.sent[0]["raw"] and b"\nBcc:" not in w.sent[0]["raw"]


def test_cannot_send_to_the_agents_own_address(w):
    assert _reply(w, {"text": "x", "to": ["Ada@marshellis.com"]}).status_code == 422
    assert w.sent == []


def test_send_starts_a_thread_and_lists_and_state(w):
    c = _c(w.owner)
    r = c.post("/api/mail/send/marshellis", {"agent": "mada", "to": ["a@b.org"], "subject": "Yo", "text": "x"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert MailThread.objects.filter(subject="Yo", route=w.r).count() == 1
    assert c.post("/api/mail/send/marshellis", {"agent": "nobody", "to": ["a@b.org"], "subject": "Yo",
                                                "text": "x"}, content_type="application/json").status_code == 404
    items = c.get("/api/mail/threads/marshellis", {"agent": "mada"}).json()["items"]
    assert len(items) == 2
    s = c.post(f"/api/mail/threads/marshellis/{w.t.uuid}/state", {"archived": True, "unread": False},
               content_type="application/json")
    assert s.status_code == 200 and s.json()["archived"] is True
    assert len(c.get("/api/mail/threads/marshellis", {"agent": "mada"}).json()["items"]) == 1
    assert c.get("/api/mail/threads/marshellis/00000000-0000-0000-0000-000000000000").status_code == 404
