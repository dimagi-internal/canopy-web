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
    assert r.json() == {"backend": "ses", "workspace": "marshellis", "agent": "mada",
                        "address": "ada@marshellis.com"}
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


def _editor(w):
    u = User.objects.create_user("ed", "ed@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=w.d.workspace, user=u, role="editor")
    return u


def test_editor_with_full_access_can_send(w):
    assert _reply(w, {"text": "x"}, _editor(w)).status_code == 200


def test_viewer_is_refused_on_every_write(w):
    c = _c(w.viewer)
    for url, body in [(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", {"text": "x"}),
                      ("/api/mail/send/marshellis", {"agent": "mada", "to": ["a@b.org"], "subject": "s", "text": "x"}),
                      (f"/api/mail/threads/marshellis/{w.t.uuid}/state", {"archived": True})]:
        assert c.post(url, body, content_type="application/json").status_code == 403


def test_access_decide_denial_is_a_403_with_its_reason(w, monkeypatch):
    from apps.agents import access
    monkeypatch.setattr(access, "decide", lambda *a, **k: access.Decision(
        "member", access.NONE, None, False, False, False, "refused", "ada offers nothing to you"))
    for r in (_reply(w, {"text": "x"}),
              _c(w.owner).post("/api/mail/send/marshellis",
                               {"agent": "mada", "to": ["a@b.org"], "subject": "s", "text": "x"},
                               content_type="application/json"),
              _c(w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/state", {"archived": True},
                               content_type="application/json")):
        assert r.status_code == 403 and "offers nothing" in r.json()["detail"]
    assert w.sent == []


def test_provider_failure_leaves_nothing_behind(w, monkeypatch):
    from botocore.exceptions import ClientError

    def boom(**kw):
        raise ClientError({"Error": {"Code": "MessageRejected"}}, "SendRawEmail")
    monkeypatch.setattr(ses, "send_raw", boom)
    r = _c(w.owner).post("/api/mail/send/marshellis", {"agent": "mada", "to": ["a@b.org"], "subject": "s",
                                                       "text": "x"}, content_type="application/json")
    assert r.status_code == 502 and "MessageRejected" in r.json()["detail"]
    assert _reply(w, {"text": "x"}).status_code == 502
    assert not MailMessage.objects.filter(direction="out").exists()
    assert MailThread.objects.count() == 1

    def off(**kw):
        raise ses.MailOff()
    monkeypatch.setattr(ses, "send_raw", off)
    assert _reply(w, {"text": "x"}).status_code == 503


def test_recipient_limits_and_ascii(w):
    assert _reply(w, {"text": "x", "to": [f"u{i}@b.org" for i in range(51)]}).status_code == 422
    assert _reply(w, {"text": "x", "to": ["josé@b.org"]}).status_code == 422


def test_cap_counts_recipients(w):
    assert _reply(w, {"text": "x", "to": ["a@b.org", "c@b.org"]}).status_code == 200
    assert _reply(w, {"text": "x"}).status_code == 429


def test_disabled_route_and_other_workspace_and_pending_domain(w):
    MailRoute.objects.filter(pk=w.r.pk).update(enabled=False)
    assert _reply(w, {"text": "x"}).status_code == 409
    MailRoute.objects.filter(pk=w.r.pk).update(enabled=True)
    other = User.objects.create_user("o", "o@o.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="O", created_by=other)
    WorkspaceMembership.objects.create(workspace=ws2, user=other, role="owner")
    assert _c(other).get(f"/api/mail/threads/other/{w.t.uuid}").status_code == 404
    MailDomain.objects.filter(pk=w.d.pk).update(status="pending")
    assert _reply(w, {"text": "x"}).status_code == 409


def test_list_counterpart_and_agent_address(w):
    c = _c(w.owner)
    empty = MailThread.objects.create(workspace=w.d.workspace, route=w.r, subject="Empty")
    assert _reply(w, {"text": "x", "to": ["z@z.org"]}).status_code == 200  # newest on w.t is outbound
    out_t = MailThread.objects.get(pk=w.t.pk)
    inbound = MailThread.objects.create(workspace=w.d.workspace, route=w.r, subject="In")
    MailMessage.objects.create(thread=inbound, direction="in", message_id="<i2@x.org>",
                               from_addr="who@x.org", subject="In", text="hi")
    body = c.get("/api/mail/threads/marshellis", {"agent": "mada"}).json()
    by = {i["thread_id"]: i for i in body["items"]}
    assert body["agent_address"] == "ada@marshellis.com"
    assert by[str(out_t.uuid)]["counterpart"] == "z@z.org"
    assert by[str(inbound.uuid)]["counterpart"] == "who@x.org"
    assert by[str(empty.uuid)]["counterpart"] == ""
    Agent.objects.create(slug="bare", name="Bare", workspace=w.d.workspace, owner=w.owner)
    assert c.get("/api/mail/threads/marshellis", {"agent": "bare"}).json()["agent_address"] == ""
    assert c.get("/api/mail/threads/marshellis").json()["agent_address"] == ""


def _inbound(w, n, from_addr, reply_to=()):
    return MailMessage.objects.create(thread=w.t, direction="in", message_id=f"<in-{n}@x.org>",
                                      from_addr=from_addr, reply_to=list(reply_to), subject="Hello", text="hi")


def _detail(w):
    return _c(w.owner).get(f"/api/mail/threads/marshellis/{w.t.uuid}").json()


def test_same_domain_reply_to_is_honoured(w):
    _inbound(w, 2, "y@x.org", ["team@x.org", "boss@lists.x.org"])
    assert _detail(w)["default_reply_to"] == ["team@x.org", "boss@lists.x.org"]
    assert _detail(w)["messages"][-1]["reply_to"] == ["team@x.org", "boss@lists.x.org"]
    assert _reply(w, {"text": "x"}).status_code == 200
    assert w.sent[-1]["to"] == ["team@x.org", "boss@lists.x.org"]


def test_foreign_domain_reply_to_falls_back_to_the_sender(w):
    _inbound(w, 2, "y@x.org", ["team@x.org", "attacker@evil.example"])
    assert _detail(w)["default_reply_to"] == ["y@x.org"]
    assert _reply(w, {"text": "x"}).status_code == 200
    assert w.sent[-1]["to"] == ["y@x.org"]
    _inbound(w, 3, "y@x.org", ["x@notx.org"])  # a suffix that is not a subdomain
    assert _detail(w)["default_reply_to"] == ["y@x.org"]


def test_malformed_and_own_reply_to_entries_are_dropped_not_a_422(w):
    _inbound(w, 2, "y@x.org", ["not an address", "ada@marshellis.com", "ok@x.org"])
    assert _detail(w)["default_reply_to"] == ["ok@x.org"]
    assert _reply(w, {"text": "x"}).status_code == 200 and w.sent[-1]["to"] == ["ok@x.org"]
    _inbound(w, 3, "z@x.org", ["ada@marshellis.com", "bad@@x"])
    assert _detail(w)["default_reply_to"] == ["z@x.org"]


def test_thread_detail_exposes_verdicts(w):
    MailMessage.objects.filter(message_id="<in-1@x.org>").update(verdicts={"spf": "PASS", "dmarc": "FAIL"})
    msgs = _c(w.owner).get(f"/api/mail/threads/marshellis/{w.t.uuid}").json()["messages"]
    assert msgs[0]["verdicts"] == {"spf": "PASS", "dmarc": "FAIL"}


def test_send_locks_the_domain_row_around_cap_check_send_and_insert(w, monkeypatch):
    from django.db import connection

    from apps.mail import agent_mail
    locked, during = [], []
    real = MailDomain.objects.select_for_update
    monkeypatch.setattr(MailDomain.objects, "select_for_update",
                        lambda *a, **k: locked.append(connection.in_atomic_block) or real(*a, **k))

    def send_raw(**kw):
        during.append(connection.in_atomic_block)
        return "ses-x"
    monkeypatch.setattr(ses, "send_raw", send_raw)
    route = MailRoute.objects.select_related("domain", "agent").get(pk=w.r.pk)
    agent_mail.send(route, to=["a@b.org"], text="x", thread=w.t)
    assert locked == [True] and during == [True]


def test_the_cap_is_read_from_the_locked_row_not_a_stale_route(w):
    from apps.mail import agent_mail
    route = MailRoute.objects.select_related("domain", "agent").get(pk=w.r.pk)
    route.domain.daily_send_cap = 100  # stale in memory; the row says 2
    agent_mail.send(route, to=["a@b.org", "c@b.org"], text="x", thread=w.t)
    with pytest.raises(agent_mail.SendRefused) as exc:
        agent_mail.send(route, to=["d@b.org"], text="x", thread=w.t)
    assert exc.value.status == 429


def test_thread_detail_counterpart_matches_the_list(w):
    c = _c(w.owner)
    detail = c.get(f"/api/mail/threads/marshellis/{w.t.uuid}").json()
    assert detail["counterpart"] == "x@x.org"  # newest is inbound: its sender
    assert _reply(w, {"text": "x", "to": ["z@z.org"]}).status_code == 200
    detail = c.get(f"/api/mail/threads/marshellis/{w.t.uuid}").json()
    listed = {i["thread_id"]: i for i in c.get("/api/mail/threads/marshellis").json()["items"]}
    assert detail["counterpart"] == listed[str(w.t.uuid)]["counterpart"] == "z@z.org"
    empty = MailThread.objects.create(workspace=w.d.workspace, route=w.r, subject="Empty")
    assert c.get(f"/api/mail/threads/marshellis/{empty.uuid}").json()["counterpart"] == ""


# SES replaces the Message-ID canopy writes on an outbound message with its own
# (<ses-id@email.amazonses.com>), so that is what the recipient's client cites when they
# reply, and what canopy must cite when it replies again (2026-10-20: Jonathan's reply to
# Ada's first mail from ada@dimagi.ai opened a new thread).
def test_a_reply_citing_the_ses_message_id_joins_the_thread(w, settings):
    from types import SimpleNamespace

    from apps.mail import threads

    settings.MAIL_SES_REGION = "us-east-1"
    r = _c(w.owner).post("/api/mail/send/marshellis", {"agent": "mada", "to": ["jj@x.org"],
                         "subject": "Test", "text": "reply please"}, content_type="application/json")
    assert r.status_code == 200, r.content
    t = MailThread.objects.get(subject="Test", route=w.r)
    parsed = SimpleNamespace(references=[], in_reply_to="<ses-1@email.amazonses.com>", subject="Re: Test")
    assert threads.place(w.r, parsed) == t


def test_our_reply_cites_the_message_id_ses_sent(w, settings):
    settings.MAIL_SES_REGION = "us-east-1"
    MailMessage.objects.create(thread=w.t, direction="out", message_id="<ours@marshellis.com>",
                               ses_message_id="ses-0", from_addr="ada@marshellis.com", subject="Re: Hello")
    MailMessage.objects.create(thread=w.t, direction="in", message_id="<in-2@x.org>",
                               from_addr="x@x.org", subject="Re: Hello", text="again")
    assert _reply(w, {"text": "Thanks!"}).status_code == 200
    raw = w.sent[0]["raw"]
    assert b"<ses-0@email.amazonses.com>" in raw and b"<ours@marshellis.com>" not in raw
    assert b"In-Reply-To: <in-2@x.org>" in raw


def test_only_an_amazonses_host_is_read_as_an_ses_id():
    assert ses.ses_id_of("<abc@email.amazonses.com>") == "abc"
    assert ses.ses_id_of("<abc@us-west-2.amazonses.com>") == "abc"
    assert ses.ses_id_of("<abc@evilamazonses.com>") == ""
    assert ses.ses_id_of("<abc@amazonses.com.evil.org>") == ""
