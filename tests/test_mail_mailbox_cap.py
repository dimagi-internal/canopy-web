"""Each canopy-hosted agent mailbox holds at most MAIL_MAILBOX_CAP_BYTES: every stored copy
carries its size, and after each delivery or send an over-cap mailbox evicts its oldest
mail (archived, then read, then unread) — never the message just stored."""
from __future__ import annotations

import datetime as dt
import importlib
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.mail import agent_mail, inbound, parse, ses, storage
from apps.mail.models import MailAttachment, MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"
KB = 1024


@pytest.fixture()
def w(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    settings.MAIL_MAILBOX_CAP_BYTES = 10 * KB
    deleted: list[list[str]] = []
    monkeypatch.setattr(ses, "keep", lambda k: "kept/" + k.split("/", 1)[-1])
    monkeypatch.setattr(ses, "put_object", lambda *a, **k: None)
    monkeypatch.setattr(ses, "send_raw", lambda **kw: "ses-1")
    monkeypatch.setattr(ses, "delete_objects", lambda keys: deleted.append(list(keys)))
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified", receiving=True)
    a = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=u,
                             email="bailey@marshellis.com")
    r = MailRoute.objects.create(domain=d, local_part="bailey", kind="agent", agent=a)
    return type("W", (), dict(u=u, ws=ws, d=d, a=a, r=r, deleted=deleted))


_n = iter(range(10_000))


def _stored(w, *, size, age_days, archived=False, unread=False, route=None, raw_key="", attachments=0):
    """One stored message in its own thread, `age_days` old."""
    route = route or w.r
    t = MailThread.objects.create(workspace=w.ws, route=route, subject="s", message_count=1,
                                  archived=archived, unread=unread)
    m = MailMessage.objects.create(thread=t, direction="in", message_id=f"<m{next(_n)}@x.org>",
                                   from_addr="x@x.org", size_bytes=size, raw_key=raw_key)
    for i in range(attachments):
        MailAttachment.objects.create(message=m, filename="a", size=1, s3_key=f"attachments/marshellis/{m.pk}/{i}")
    MailMessage.objects.filter(pk=m.pk).update(created_at=timezone.now() - dt.timedelta(days=age_days))
    return m


def _deliver(w, name="plain.eml", raw_size=0, **kw):
    p = parse.parse((FIX / name).read_bytes())
    assert agent_mail.deliver(w.r, p, "incoming/new", raw_size=raw_size, **kw) == "agent"
    return MailMessage.objects.get(message_id=p.message_id, mailbox=w.r.address), p


def _alive(*msgs) -> list[bool]:
    return [MailMessage.objects.filter(pk=m.pk).exists() for m in msgs]


# --- accounting


def test_delivery_records_text_html_attachments_and_raw_size(w):
    m, p = _deliver(w, "with_attachment.eml", raw_size=5000)
    att = sum(len(a.data) for a in p.attachments)
    assert att > 0
    assert m.size_bytes == len(p.text.encode()) + len(p.html.encode()) + att + 5000
    assert storage.used_bytes(w.ws.pk, "bailey@marshellis.com") == m.size_bytes


def test_inbound_passes_the_raw_objects_size(w):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    out = inbound.deliver(p, raw, "incoming/abc", ["bailey@marshellis.com"], verdicts={})
    assert out == [{"recipient": "bailey@marshellis.com", "outcome": "agent"}]
    m = MailMessage.objects.get(message_id=p.message_id)
    assert m.size_bytes == len(p.text.encode()) + len(p.html.encode()) + len(raw)


def test_a_sent_copy_counts_toward_the_mailbox(w):
    out = agent_mail.send(w.r, to=["x@x.org"], subject="Hi", text="héllo", html="<p>hi</p>")
    assert out.size_bytes == len("héllo".encode()) + len(b"<p>hi</p>")
    assert storage.used_bytes(w.ws.pk, "bailey@marshellis.com") == out.size_bytes


def test_backfill_approximates_existing_rows_from_their_stored_parts(w):
    t = MailThread.objects.create(workspace=w.ws, route=w.r)
    m = MailMessage.objects.create(thread=t, direction="in", message_id="<old@x.org>",
                                   text="héllo", html="<b>x</b>")
    MailAttachment.objects.create(message=m, size=300, s3_key="attachments/marshellis/x/0")
    MailAttachment.objects.create(message=m, size=200, s3_key="attachments/marshellis/x/1")
    mig = importlib.import_module("apps.mail.migrations.0007_message_size")
    mig.backfill(django_apps, None)
    m.refresh_from_db()
    assert m.size_bytes == len("héllo".encode()) + len(b"<b>x</b>") + 500


# --- eviction


def test_under_the_cap_nothing_is_evicted(w):
    old = _stored(w, size=2 * KB, age_days=30)
    _deliver(w)
    assert _alive(old) == [True]
    assert w.deleted == []


def test_eviction_takes_archived_then_read_then_unread_oldest_first(w, django_capture_on_commit_callbacks):
    unread_old = _stored(w, size=3 * KB, age_days=90, unread=True)
    read_old = _stored(w, size=3 * KB, age_days=60)
    read_new = _stored(w, size=3 * KB, age_days=5)
    archived_new = _stored(w, size=3 * KB, age_days=1, archived=True)
    with django_capture_on_commit_callbacks(execute=True):
        new, _ = _deliver(w, raw_size=3 * KB)  # 15 KB + text against a 10 KB cap
    # Over by >5 KB: the archived thread goes first, then the oldest read one.
    assert _alive(archived_new, read_old, read_new, unread_old, new) == [False, False, True, True, True]
    assert storage.used_bytes(w.ws.pk, "bailey@marshellis.com") <= 10 * KB


def test_unread_mail_goes_last_but_does_go(w):
    unread = [_stored(w, size=4 * KB, age_days=d, unread=True) for d in (30, 20, 10)]
    new, _ = _deliver(w)
    assert _alive(*unread, new) == [False, True, True, True]


def test_the_message_just_delivered_survives_even_alone_over_the_cap(w):
    old = _stored(w, size=1 * KB, age_days=3, archived=True)
    new, _ = _deliver(w, raw_size=50 * KB)
    assert _alive(old, new) == [False, True]
    assert storage.used_bytes(w.ws.pk, "bailey@marshellis.com") > 10 * KB


def test_evicted_messages_delete_their_s3_objects_and_empty_threads(w, django_capture_on_commit_callbacks):
    gone = _stored(w, size=9 * KB, age_days=10, archived=True, raw_key="kept/aaa", attachments=2)
    t = gone.thread
    with django_capture_on_commit_callbacks(execute=True):
        _deliver(w, raw_size=5 * KB)
    assert _alive(gone) == [False]
    assert not MailThread.objects.filter(pk=t.pk).exists()
    assert not MailAttachment.objects.filter(message_id=gone.pk).exists()
    assert w.deleted == [[f"attachments/marshellis/{gone.pk}/0", f"attachments/marshellis/{gone.pk}/1",
                          "kept/aaa"]]


def test_a_raw_object_another_mailbox_still_cites_is_kept(w, django_capture_on_commit_callbacks):
    other = MailRoute.objects.create(domain=w.d, local_part="eva", kind="agent",
                                     agent=Agent.objects.create(slug="eva2", name="Eva", workspace=w.ws, owner=w.u))
    gone = _stored(w, size=9 * KB, age_days=10, archived=True, raw_key="kept/shared")
    twin = _stored(w, size=9 * KB, age_days=10, archived=True, raw_key="kept/shared", route=other)
    with django_capture_on_commit_callbacks(execute=True):
        _deliver(w, raw_size=5 * KB)
    assert _alive(gone, twin) == [False, True]  # the other mailbox is its own cap
    assert w.deleted == []  # nothing of its own to delete; the shared raw stays


def test_a_thread_keeping_messages_has_its_count_lowered(w):
    t = MailThread.objects.create(workspace=w.ws, route=w.r, subject="s", message_count=2, archived=True)
    old = MailMessage.objects.create(thread=t, direction="in", message_id="<o@x.org>", size_bytes=8 * KB)
    keep = MailMessage.objects.create(thread=t, direction="in", message_id="<k@x.org>", size_bytes=1 * KB)
    MailMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=9))
    _deliver(w, raw_size=4 * KB)
    assert _alive(old, keep) == [False, True]
    t.refresh_from_db()
    assert t.message_count == 1


def test_a_send_over_the_cap_evicts_too_but_keeps_the_sent_copy(w):
    old = _stored(w, size=10 * KB, age_days=10)
    out = agent_mail.send(w.r, to=["x@x.org"], subject="Hi", text="x" * 2000)
    assert _alive(old, out) == [False, True]


def test_a_failing_eviction_never_fails_the_delivery(w, monkeypatch):
    _stored(w, size=20 * KB, age_days=10)
    monkeypatch.setattr(storage, "_victims", lambda *a, **k: 1 / 0)
    new, _ = _deliver(w)
    assert _alive(new) == [True]


def test_a_zero_cap_turns_eviction_off(w, settings):
    settings.MAIL_MAILBOX_CAP_BYTES = 0
    old = _stored(w, size=500 * KB, age_days=10, archived=True)
    _deliver(w)
    assert _alive(old) == [True]


# --- visibility


def test_the_agent_mailbox_reports_usage_and_cap(w):
    _stored(w, size=3 * KB, age_days=1)
    _stored(w, size=2 * KB, age_days=1)
    c = Client()
    c.force_login(w.u)
    body = c.get("/api/mail/agents/bailey/mailbox").json()
    assert body["kind"] == "hosted"
    assert body["used_bytes"] == 5 * KB and body["cap_bytes"] == 10 * KB



# --- per-message attachment limit


def test_attachments_over_the_message_limit_are_recorded_but_not_kept(w, settings, monkeypatch):
    put = []
    monkeypatch.setattr(ses, "put_object", lambda key, data, ct: put.append(key))
    p = parse.parse((FIX / "with_attachment.eml").read_bytes())
    size = len(p.attachments[0].data)
    settings.MAIL_MAX_ATTACHMENT_BYTES = size - 1
    m, _ = _deliver(w, "with_attachment.eml", raw_size=5000)
    att = m.attachments.get()
    assert att.kept is False and att.s3_key == "" and att.size == size
    assert att.filename == "a.pdf" and att.content_type
    assert put == []
    # The raw MIME holds the dropped content, so it is not kept either; only what is stored counts.
    assert m.raw_key == ""
    assert m.size_bytes == len(p.text.encode()) + len(p.html.encode())
    c = Client()
    c.force_login(w.u)
    body = c.get(f"/api/mail/threads/marshellis/{m.thread.uuid}").json()
    assert body["messages"][0]["attachments"][0] == {
        "filename": "a.pdf", "content_type": att.content_type, "size": size, "url": "", "kept": False}


def test_attachments_within_the_limit_are_kept(w, settings, monkeypatch):
    put = []
    monkeypatch.setattr(ses, "put_object", lambda key, data, ct: put.append(key))
    m, p = _deliver(w, "with_attachment.eml", raw_size=5000)
    att = m.attachments.get()
    assert att.kept is True and att.s3_key == put[0] and m.raw_key == "kept/new"
    assert m.size_bytes == len(p.text.encode()) + len(p.html.encode()) + att.size + 5000


def test_the_limit_keeps_what_fits_in_order(settings):
    settings.MAIL_MAX_ATTACHMENT_BYTES = 10
    assert storage.kept_attachments([4, 8, 6, 1]) == [True, False, True, False]
    settings.MAIL_MAX_ATTACHMENT_BYTES = 0
    assert storage.kept_attachments([10**9]) == [True]


def test_eviction_skips_s3_for_an_attachment_never_kept(w, django_capture_on_commit_callbacks):
    gone = _stored(w, size=9 * KB, age_days=10, archived=True, attachments=1)
    MailAttachment.objects.create(message=gone, filename="big", size=10**8, s3_key="", kept=False)
    with django_capture_on_commit_callbacks(execute=True):
        _deliver(w, raw_size=5 * KB)
    assert w.deleted == [[f"attachments/marshellis/{gone.pk}/0"]]
