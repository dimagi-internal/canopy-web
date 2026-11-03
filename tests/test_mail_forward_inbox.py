"""The inbound ledger (every outcome, including drops) and the kept copy of a forwarded message."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.mail import agent_mail, inbound, parse, ses, storage
from apps.mail.models import (
    MailAttachment,
    MailDomain,
    MailDraft,
    MailInboundLog,
    MailMessage,
    MailRoute,
    MailThread,
    MailUpload,
)
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


def test_a_new_forward_keeps_a_copy_unless_told_not_to(w):
    url = f"/api/mail/domains/marshellis/{w.d.pk}/routes"
    put = lambda body: _c(w.owner).put(url, body, content_type="application/json").json()  # noqa: E731
    fwd = {"kind": "forward", "forward_to_user_id": w.owner.pk}
    assert put({**fwd, "local_part": "fresh"})["keep_copy"] is True
    assert put({**fwd, "local_part": "optout", "keep_copy": False})["keep_copy"] is False
    assert put({**fwd, "local_part": "optout"})["keep_copy"] is False  # an opt-out survives an edit
    assert put({"local_part": "was-drop", "kind": "drop"})["keep_copy"] is False
    assert put({**fwd, "local_part": "was-drop"})["keep_copy"] is True  # now a forward for the first time


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


# --- The mail client: folders, trash, drafts, bcc, attachments.

def _stored(w, n=1, subject="Hi"):
    th = MailThread.objects.create(workspace=w.ws, route=w.r, owner=w.owner, address=w.r.address, subject=subject,
                                   message_count=1, last_message_at=timezone.now())
    MailMessage.objects.create(thread=th, workspace=w.ws, direction="in", mailbox=w.r.address,
                               message_id=f"<{th.pk}@x>", from_addr="a@x.org", from_name="A Person", to=[w.r.address],
                               text=f"Hello there\n\nOn Tue, J wrote:\n> old words {n}")
    return th


def _list(w, **q):
    return _c(w.owner).get("/api/mail/threads/marshellis", {"forwarded": "true", **q}).json()


def test_list_rows_say_who_and_what_without_the_quote(w):
    _stored(w)
    row = _list(w, folder="inbox")["items"][0]
    assert row["counterpart_name"] == "A Person" and row["snippet"] == "Hello there"
    assert row["has_attachments"] is False


def test_list_pages(w):
    for i in range(3):
        _stored(w, subject=f"s{i}")
    page = _list(w, folder="inbox", limit=2)
    assert len(page["items"]) == 2 and page["has_more"] is True
    rest = _list(w, folder="inbox", limit=2, offset=2)
    assert len(rest["items"]) == 1 and rest["has_more"] is False


def test_folders_hold_what_they_should(w):
    live, gone, junk, sent = _stored(w), _stored(w), _stored(w), _stored(w)
    MailThread.objects.filter(pk=gone.pk).update(trashed_at=timezone.now())
    MailThread.objects.filter(pk=junk.pk).update(spam=True)
    MailMessage.objects.filter(thread=sent).update(direction="out")
    ids = lambda **q: {r["thread_id"] for r in _list(w, **q)["items"]}  # noqa: E731
    assert ids(folder="inbox") == {str(live.uuid)}
    assert ids(folder="sent") == {str(sent.uuid)}
    assert ids(folder="trash") == {str(gone.uuid)}
    assert ids(folder="spam") == {str(junk.uuid)}
    assert str(gone.uuid) not in ids() and str(junk.uuid) not in ids()  # the agents' default read
    assert _c(w.owner).get("/api/mail/threads/marshellis", {"folder": "nope"}).status_code == 422


def test_trash_then_delete_for_good_removes_the_files_too(w, monkeypatch, django_capture_on_commit_callbacks):
    gone = []
    monkeypatch.setattr(ses, "delete_objects", lambda keys: gone.extend(keys))
    th = _stored(w)
    MailMessage.objects.filter(thread=th).update(raw_key="kept/abc")
    url = f"/api/mail/threads/marshellis/{th.uuid}"
    assert _c(w.owner).delete(url).status_code == 409  # not from the inbox
    moved = _c(w.owner).post(f"{url}/state", {"trashed": True}, content_type="application/json")
    assert moved.json()["trashed"] is True
    with django_capture_on_commit_callbacks(execute=True):
        assert _c(w.owner).delete(url).status_code == 204
    assert not MailThread.objects.filter(pk=th.pk).exists() and gone == ["kept/abc"]


def test_nobody_else_can_delete_or_empty_my_mail(w):
    th = _stored(w)
    MailThread.objects.filter(pk=th.pk).update(trashed_at=timezone.now())
    assert _c(w.other).delete(f"/api/mail/threads/marshellis/{th.uuid}").status_code == 404
    assert _post(w.other, "/api/mail/empty-folder/marshellis?folder=trash", {}).json() == {"deleted": 0}
    assert MailThread.objects.filter(pk=th.pk).exists()
    assert _post(w.owner, "/api/mail/empty-folder/marshellis?folder=trash", {}).json() == {"deleted": 1}
    assert _post(w.owner, "/api/mail/empty-folder/marshellis?folder=inbox", {}).status_code == 422


def test_trash_past_its_days_is_purged(w, monkeypatch):
    monkeypatch.setattr(ses, "delete_objects", lambda keys: None)
    old, recent = _stored(w), _stored(w)
    MailThread.objects.filter(pk=old.pk).update(trashed_at=timezone.now() - timedelta(days=storage.TRASH_DAYS + 1))
    MailThread.objects.filter(pk=recent.pk).update(trashed_at=timezone.now())
    storage.purge_expired()
    assert not MailThread.objects.filter(pk=old.pk).exists() and MailThread.objects.filter(pk=recent.pk).exists()


def test_a_spam_threads_attachments_are_listed_without_a_link(w, monkeypatch):
    monkeypatch.setattr(ses, "presign", lambda key, name: "https://s3.example/f")
    th = _stored(w)
    MailAttachment.objects.create(message=th.messages.get(), filename="x.exe", size=1, s3_key="attachments/x")
    url = f"/api/mail/threads/marshellis/{th.uuid}"
    assert _c(w.owner).get(url).json()["messages"][0]["attachments"][0]["url"] == "https://s3.example/f"
    MailThread.objects.filter(pk=th.pk).update(spam=True)
    assert _c(w.owner).get(url).json()["messages"][0]["attachments"][0]["url"] == ""


def test_a_draft_is_its_authors_alone_and_saving_twice_is_one_draft(w):
    key = "11111111-1111-4111-8111-111111111111"
    url = f"/api/mail/drafts/marshellis/{key}"
    put = lambda c, text: c.put(url, {"to": "bob", "text": text}, content_type="application/json")  # noqa: E731
    assert put(_c(w.owner), "one").status_code == 200
    assert put(_c(w.owner), "two").json()["text"] == "two"
    assert [d["text"] for d in _c(w.owner).get("/api/mail/drafts/marshellis").json()["items"]] == ["two"]
    assert _c(w.other).get("/api/mail/drafts/marshellis").json()["items"] == []
    assert put(_c(w.other), "steal").status_code == 404
    assert _c(w.other).delete(url).status_code == 204 and MailDraft.objects.filter(uuid=key).exists()
    assert _c(w.owner).delete(url).status_code == 204 and not MailDraft.objects.filter(uuid=key).exists()


def test_a_draft_cannot_name_someone_elses_thread(w):
    th = _stored(w)
    url = "/api/mail/drafts/marshellis/22222222-2222-4222-8222-222222222222"
    body = {"thread_id": str(th.uuid), "text": "x"}
    assert _c(w.other).put(url, body, content_type="application/json").status_code == 404


def test_bcc_goes_on_the_envelope_never_the_headers_and_counts_against_the_cap(w, monkeypatch):
    seen = {}
    monkeypatch.setattr(ses, "send_raw", lambda **kw: seen.update(kw) or "ses-1")
    r = _c(w.owner).post("/api/mail/send/marshellis", {
        "from_address": w.r.address, "to": ["t@x.org"], "bcc": ["secret@x.org"], "subject": "S", "text": "T"},
        content_type="application/json")
    assert r.status_code == 200 and r.json()["bcc"] == ["secret@x.org"]
    assert seen["to"] == ["t@x.org", "secret@x.org"] and b"secret@x.org" not in seen["raw"]
    assert agent_mail._sent_today(w.d) == 2


def test_an_attachment_goes_out_and_stays_with_the_sent_message(w, monkeypatch):
    objects, seen = {}, {}
    monkeypatch.setattr(ses, "put_object", lambda key, data, ct: objects.update({key: data}))
    monkeypatch.setattr(ses, "get_object", lambda key: objects[key])
    monkeypatch.setattr(ses, "presign", lambda key, name: "https://s3.example/f")
    monkeypatch.setattr(ses, "send_raw", lambda **kw: seen.update(kw) or "ses-1")
    up = _c(w.owner).post("/api/mail/uploads/marshellis",
                          {"file": SimpleUploadedFile("note.txt", b"hello file", content_type="text/plain")})
    assert up.status_code == 200 and up.json()["filename"] == "note.txt"
    r = _c(w.owner).post("/api/mail/send/marshellis", {
        "from_address": w.r.address, "to": ["t@x.org"], "subject": "S", "text": "T",
        "attachments": [up.json()["id"]]}, content_type="application/json")
    assert r.status_code == 200 and b"note.txt" in seen["raw"]
    assert not MailUpload.objects.exists()
    th = MailThread.objects.get(subject="S")
    att = _c(w.owner).get(f"/api/mail/threads/marshellis/{th.uuid}").json()["messages"][0]["attachments"][0]
    assert att["filename"] == "note.txt" and att["size"] == 10
    assert MailMessage.objects.get(thread=th).size_bytes >= 10


def test_someone_elses_upload_cannot_be_sent(w, monkeypatch):
    monkeypatch.setattr(ses, "put_object", lambda *a: None)
    up = _c(w.owner).post("/api/mail/uploads/marshellis", {"file": SimpleUploadedFile("n.txt", b"x")}).json()
    MailRoute.objects.create(domain=w.d, local_part="ot", kind="forward", forward_to=w.other)
    r = _c(w.other).post("/api/mail/send/marshellis", {
        "from_address": f"ot@{w.d.name}", "to": ["t@x.org"], "subject": "S", "text": "T", "attachments": [up["id"]]},
        content_type="application/json")
    assert r.status_code == 422


def test_a_reply_names_the_address_it_goes_out_from(w):
    th = _stored(w)
    assert _c(w.owner).get(f"/api/mail/threads/marshellis/{th.uuid}").json()["reply_from"] == w.r.address
