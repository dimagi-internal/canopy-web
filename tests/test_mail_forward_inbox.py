"""The inbound ledger (every outcome, including drops) and the kept copy of a forwarded message."""
from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.mail import agent_mail, inbound, parse, ses
from apps.mail.models import MailDomain, MailInboundLog, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def w(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    owner = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    other = User.objects.create_user("ot", "ot@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    WorkspaceMembership.objects.create(workspace=ws, user=other, role="admin")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    r = MailRoute.objects.create(domain=d, local_part="jjackson", kind="forward", forward_to=owner,
                                 keep_copy=True)
    monkeypatch.setattr(ses, "send_raw", lambda **kw: "ses-fwd-1")
    monkeypatch.setattr(ses, "keep", lambda key: "kept/" + key.split("/", 1)[-1])
    monkeypatch.setattr(ses, "put_object", lambda *a, **k: None)
    raw = (FIX / "plain.eml").read_bytes()
    monkeypatch.setattr(ses, "get_object", lambda key: raw)
    return type("W", (), dict(owner=owner, other=other, ws=ws, d=d, r=r, raw=raw))


def _note(rcpt, spam="PASS"):
    return {"receipt": {"recipients": [rcpt], "spamVerdict": {"status": spam},
                        "virusVerdict": {"status": "PASS"}, "action": {"objectKey": "incoming/abc"}},
            "mail": {"messageId": "ses-abc", "source": "a@x.org",
                     "commonHeaders": {"from": ["A <a@x.org>"], "subject": "Hi", "messageId": "<m1@x.org>"}}}


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def test_forward_with_keep_copy_stores_a_thread_only_the_destination_reads(w):
    out = inbound.handle(_note("jjackson@marshellis.com"))
    assert out == [{"recipient": "jjackson@marshellis.com", "outcome": "forwarded"}]
    t = MailThread.objects.get()
    assert t.owner == w.owner and t.agent_slug == "" and t.message_count == 1
    assert MailMessage.objects.get().mailbox == "jjackson@marshellis.com"
    mine = _c(w.owner).get("/api/mail/threads/marshellis", {"forwarded": True}).json()["items"]
    assert [i["thread_id"] for i in mine] == [str(t.uuid)] and mine[0]["forwarded"] is True
    # an admin sees neither the list entry nor the thread
    assert _c(w.other).get("/api/mail/threads/marshellis", {"forwarded": True}).json()["items"] == []
    assert _c(w.other).get("/api/mail/threads/marshellis").json()["items"] == []
    assert _c(w.other).get(f"/api/mail/threads/marshellis/{t.uuid}").status_code == 404
    assert _c(w.owner).get(f"/api/mail/threads/marshellis/{t.uuid}").status_code == 200
    # the owner's list of agent threads never mixes them in
    assert _c(w.owner).get("/api/mail/threads/marshellis").json()["items"] == []


def test_owner_can_mark_their_forwarded_thread(w):
    inbound.handle(_note("jjackson@marshellis.com"))
    t = MailThread.objects.get()
    r = _c(w.owner).post(f"/api/mail/threads/marshellis/{t.uuid}/state", {"unread": False},
                         content_type="application/json")
    assert r.status_code == 200 and r.json()["unread"] is False
    other = _c(w.other).post(f"/api/mail/threads/marshellis/{t.uuid}/state", {"unread": False},
                             content_type="application/json")
    assert other.status_code == 404


def test_no_keep_copy_stores_nothing(w):
    MailRoute.objects.filter(pk=w.r.pk).update(keep_copy=False)
    inbound.handle(_note("jjackson@marshellis.com"))
    assert MailThread.objects.count() == 0


def test_a_failing_copy_never_blocks_the_forward(w, monkeypatch):
    monkeypatch.setattr(agent_mail, "keep_forwarded", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3")))
    out = inbound.handle(_note("jjackson@marshellis.com"))
    assert out[0]["outcome"] == "forwarded"


def test_redelivery_does_not_duplicate_the_copy(w):
    inbound.handle(_note("jjackson@marshellis.com"))
    inbound.handle(_note("jjackson@marshellis.com"))
    assert MailMessage.objects.count() == 1


def test_ledger_records_forwards_and_spam_and_unrouted(w):
    inbound.handle(_note("jjackson@marshellis.com"))
    inbound.handle(_note("jjackson@marshellis.com", spam="FAIL"))
    inbound.handle(_note("nobody@marshellis.com"))
    rows = {(r.recipient, r.outcome): r for r in MailInboundLog.objects.all()}
    assert ("jjackson@marshellis.com", "forwarded") in rows
    spam = rows[("jjackson@marshellis.com", "dropped:spam")]
    assert spam.original_from == "A <a@x.org>" and spam.subject == "Hi" and spam.verdicts["spam"] == "FAIL"
    assert ("nobody@marshellis.com", "dropped:no-route") in rows
    assert all(r.domain_id == w.d.pk for r in rows.values())


def test_a_ledger_failure_never_changes_delivery(w, monkeypatch):
    monkeypatch.setattr(MailInboundLog.objects, "create", lambda **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert inbound.handle(_note("jjackson@marshellis.com"))[0]["outcome"] == "forwarded"


def test_ledger_endpoint_is_for_admins_of_the_owning_workspace(w):
    inbound.handle(_note("jjackson@marshellis.com", spam="FAIL"))
    url = f"/api/mail/domains/marshellis/{w.d.pk}/inbound"
    body = _c(w.owner).get(url).json()
    assert body["retention_days"] == MailInboundLog.RETENTION_DAYS
    assert body["items"][0]["outcome"] == "dropped:spam" and body["items"][0]["subject"] == "Hi"
    assert _c(w.owner).get(url, {"recipient": "nobody"}).json()["items"] == []
    viewer = User.objects.create_user("v", "v@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=w.ws, user=viewer, role="viewer")
    assert _c(viewer).get(url).status_code == 403


def test_route_keep_copy_round_trips_and_survives_an_edit_that_omits_it(w):
    url = f"/api/mail/domains/marshellis/{w.d.pk}/routes"
    body = {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": w.owner.pk, "enabled": False}
    r = _c(w.owner).put(url, body, content_type="application/json").json()
    assert r["keep_copy"] is True  # omitted = unchanged
    body["keep_copy"] = False
    assert _c(w.owner).put(url, body, content_type="application/json").json()["keep_copy"] is False
    body.update(local_part="new", keep_copy=True)
    assert _c(w.owner).put(url, body, content_type="application/json").json()["keep_copy"] is True
    agent_body = {"local_part": "x", "kind": "drop", "keep_copy": True}
    assert _c(w.owner).put(url, agent_body, content_type="application/json").json()["keep_copy"] is False


# --- the inbox as a mail client: search, archive, reply, compose ------------------------------

def _post(u, url, body):
    return _c(u).post(url, body, content_type="application/json")


@pytest.fixture()
def sent(w, monkeypatch):
    calls = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: calls.append(kw) or f"ses-out-{len(calls)}")
    return calls


def _kept(w, rcpt="jjackson@marshellis.com"):
    inbound.handle(_note(rcpt))
    return MailThread.objects.get()


def test_list_reports_unread_total_and_my_sendable_addresses(w):
    MailRoute.objects.create(domain=w.d, local_part="*", kind="forward", forward_to=w.owner)
    _kept(w)
    body = _c(w.owner).get("/api/mail/threads/marshellis", {"forwarded": True}).json()
    assert body["unread_total"] == 1
    assert body["addresses"] == ["jjackson@marshellis.com"]  # never the catch-all
    assert _c(w.other).get("/api/mail/threads/marshellis", {"forwarded": True}).json()["addresses"] == []


def test_search_archive_and_address_filter(w):
    t = _kept(w)
    get = lambda **q: _c(w.owner).get("/api/mail/threads/marshellis", {"forwarded": True, **q}).json()["items"]  # noqa: E731
    assert len(get()) == 1
    assert get(q="zzz-no-such-term") == []
    assert len(get(address="jjackson@marshellis.com")) == 1 and get(address="other@marshellis.com") == []
    _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/state", {"archived": True})
    assert get() == [] and len(get(archived=True)) == 1
    assert _c(w.owner).get("/api/mail/threads/marshellis", {"forwarded": True}).json()["unread_total"] == 0


def test_owner_replies_from_their_own_address_in_the_same_thread(w, sent):
    t = _kept(w)
    r = _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "Sounds good"})
    assert r.status_code == 200, r.content
    out = sent[-1]  # sent[0] is the forward of the inbound message itself
    assert out["from_addr"] == "jjackson@marshellis.com"
    assert out["to"] == [parse.parse((FIX / "plain.eml").read_bytes()).from_addr]
    t.refresh_from_db()
    assert t.message_count == 2 and t.owner == w.owner
    assert MailMessage.objects.filter(thread=t, direction="out").count() == 1


def test_nobody_else_can_reply_on_my_thread(w, sent):
    t = _kept(w)
    assert _post(w.other, f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "hi"}).status_code == 404
    assert len(sent) == 1  # just the forward


def test_reply_on_a_catch_all_thread_is_refused_when_no_address_in_it_routes_to_that_catch_all(w, sent):
    MailRoute.objects.filter(pk=w.r.pk).delete()
    MailRoute.objects.create(domain=w.d, local_part="*", kind="forward", forward_to=w.owner, keep_copy=True)
    inbound.handle(_note("apple@marshellis.com"))  # delivered to the catch-all, but the message says To: ada@
    t = MailThread.objects.get()
    # ada@ is someone else's now
    MailRoute.objects.create(domain=w.d, local_part="ada", kind="forward", forward_to=w.other)
    r = _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "yo", "to": ["a@x.org"]})
    assert r.status_code == 409 and len(sent) == 1  # only the forward: never invents a From for a catch-all


def test_reply_refused_once_the_route_no_longer_forwards_to_me(w, sent):
    t = _kept(w)
    MailRoute.objects.filter(pk=w.r.pk).update(enabled=False)
    assert _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "hi"}).status_code == 409


def test_compose_from_my_address_starts_a_private_thread(w, sent):
    r = _post(w.owner, "/api/mail/send/marshellis",
              {"from_address": "jjackson@marshellis.com", "to": ["friend@example.org"],
               "subject": "Hello", "text": "Hi"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["subject"] == "Hello" and body["thread_id"] == str(MailThread.objects.get().uuid)
    assert sent[-1]["from_addr"] == "jjackson@marshellis.com" and sent[-1]["to"] == ["friend@example.org"]
    t = MailThread.objects.get()
    assert t.owner == w.owner
    assert _c(w.other).get(f"/api/mail/threads/marshellis/{t.uuid}").status_code == 404


def test_compose_cannot_send_as_someone_elses_or_the_catch_all_address(w, sent):
    MailRoute.objects.create(domain=w.d, local_part="support", kind="forward", forward_to=w.other)
    MailRoute.objects.create(domain=w.d, local_part="*", kind="forward", forward_to=w.owner)
    base = {"to": ["friend@example.org"], "subject": "s", "text": "t"}
    for frm in ("support@marshellis.com", "anything@marshellis.com", "*@marshellis.com", "ceo@gmail.com"):
        r = _post(w.owner, "/api/mail/send/marshellis", {**base, "from_address": frm})
        assert r.status_code == 422, frm
    assert sent == []


def test_reply_on_a_catch_all_thread_is_from_the_address_the_mail_was_sent_to(w, sent):
    MailRoute.objects.filter(pk=w.r.pk).delete()
    MailRoute.objects.create(domain=w.d, local_part="*", kind="forward", forward_to=w.owner, keep_copy=True)
    inbound.handle(_note("ada@marshellis.com"))  # the fixture message is addressed To: ada@marshellis.com
    t = MailThread.objects.get()
    r = _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "yo"})
    assert r.status_code == 200, r.content
    assert sent[-1]["from_addr"] == "ada@marshellis.com"


def test_an_agents_inbox_gets_the_same_list_features(w):
    from apps.agents.models import Agent

    a = Agent.objects.create(slug="mada", name="Ada", workspace=w.ws, owner=w.owner)
    r = MailRoute.objects.create(domain=w.d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=w.ws, route=r, subject="Grant question", message_count=1)
    MailMessage.objects.create(thread=t, direction="in", message_id="<g1@x.org>", from_addr="amy@x.org",
                               subject="Grant question", text="budget attached")
    get = lambda **q: _c(w.owner).get("/api/mail/threads/marshellis", {"agent": "mada", **q}).json()  # noqa: E731
    body = get()
    assert body["agent_address"] == "ada@marshellis.com" and body["addresses"] == ["ada@marshellis.com"]
    assert body["unread_total"] == 1 and len(body["items"]) == 1
    assert len(get(q="budget")["items"]) == 1 and get(q="zzz")["items"] == []
    assert get(address="other@marshellis.com")["items"] == []
    _post(w.owner, f"/api/mail/threads/marshellis/{t.uuid}/state", {"archived": True})
    assert get()["items"] == [] and len(get(archived=True)["items"]) == 1 and get()["unread_total"] == 0
    # and never mixed with anyone's forwarded copies
    inbound.handle(_note("jjackson@marshellis.com"))
    assert all(i["forwarded"] is False for i in get(archived=True)["items"])
