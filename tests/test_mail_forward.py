from __future__ import annotations

from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.mail import forward, parse, ses
from apps.mail.models import MailDomain, MailForwardLog, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def route(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or "ses-fwd-1")
    r = MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=u)
    r.sent = sent
    return r


def test_forward_rewrites_from_and_keeps_reply_to(route):
    raw = (FIX / "with_attachment.eml").read_bytes()
    out = forward.forward(route, parse.parse(raw), raw, "apple@marshellis.com")
    assert out == "forwarded"
    sent = route.sent[0]
    assert sent["from_addr"] == "canopy-forwarder@marshellis.com"
    assert sent["to"] == ["jjackson@dimagi.com"]
    msg = BytesParser(policy=policy.default).parsebytes(sent["raw"])
    assert "via marshellis.com" in msg["From"]
    assert parse.parse(raw).from_addr in msg["Reply-To"]
    assert msg["X-Original-To"] == "apple@marshellis.com"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["a.pdf"]
    assert not msg.get("DKIM-Signature")  # the original's signature would now fail


def test_redelivery_does_not_forward_twice(route):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    assert forward.forward(route, p, raw, "a@marshellis.com") == "forwarded"
    assert forward.forward(route, p, raw, "a@marshellis.com") == "duplicate"
    assert len(route.sent) == 1


def test_ex_member_stops_receiving(route):
    WorkspaceMembership.objects.filter(user=route.forward_to).delete()
    raw = (FIX / "plain.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "dropped:not-a-member"
    assert route.sent == []
    assert MailForwardLog.objects.get().status == "dropped"


def test_transient_failure_raises_and_leaves_no_success_row(route, monkeypatch):
    def boom(**kw):
        raise RuntimeError("throttled")
    monkeypatch.setattr(ses, "send_raw", boom)
    raw = (FIX / "plain.eml").read_bytes()
    with pytest.raises(RuntimeError):
        forward.forward(route, parse.parse(raw), raw, "a@marshellis.com")
    assert not MailForwardLog.objects.exists()


def test_malformed_charset_original_still_forwards(route):
    raw = (FIX / "bogus_charset_multipart.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "forwarded"
    out = BytesParser(policy=policy.compat32).parsebytes(route.sent[0]["raw"])
    assert out["X-Original-To"] == "a@marshellis.com"
    assert "via marshellis.com" in out["From"]


def test_concurrent_claim_blocks_second_forward(route):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    MailForwardLog.objects.create(domain=route.domain, route=route, message_id=p.message_id,
                                  recipient="a@marshellis.com", status="sending",
                                  destination=route.forward_to)
    assert forward.forward(route, p, raw, "a@marshellis.com") == "duplicate"
    assert route.sent == []


def test_compat32_fallback_when_smtp_rewrite_raises(route, monkeypatch):
    real = forward._rewrite
    calls = []

    def flaky(msg, *a):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("malformed")
        return real(msg, *a)

    monkeypatch.setattr(forward, "_rewrite", flaky)
    raw = (FIX / "with_attachment.eml").read_bytes()
    out = forward.build(route, parse.parse(raw), raw, "a@marshellis.com")
    assert len(calls) == 2
    msg = BytesParser(policy=policy.default).parsebytes(out)
    assert "via marshellis.com" in msg["From"]
    assert not msg.get("DKIM-Signature")


def test_blank_email_is_dropped(route):
    route.forward_to.email = ""
    route.forward_to.save()
    raw = (FIX / "plain.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "dropped:no-email"
    assert route.sent == []
    assert MailForwardLog.objects.get().error == "destination has no email address"


@pytest.mark.parametrize("email", ["x@marshellis.com", "x@mail.marshellis.com"])
def test_loop_is_dropped(route, email):
    route.forward_to.email = email
    route.forward_to.save()
    raw = (FIX / "plain.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "dropped:loop"
    assert route.sent == []
    assert MailForwardLog.objects.get().error == "forwarding loop"


def _sending_row(route, p, age_minutes):
    row = MailForwardLog.objects.create(
        domain=route.domain, route=route, message_id=p.message_id, recipient="a@marshellis.com",
        status="sending", destination=route.forward_to)
    MailForwardLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
    return row


def test_stale_sending_claim_is_reclaimed_and_forwards_once(route):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    _sending_row(route, p, 11)
    assert forward.forward(route, p, raw, "a@marshellis.com") == "forwarded"
    assert len(route.sent) == 1
    assert MailForwardLog.objects.get().status == "forwarded"
    assert forward.forward(route, p, raw, "a@marshellis.com") == "duplicate"
    assert len(route.sent) == 1


def test_fresh_sending_claim_is_duplicate(route):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    _sending_row(route, p, 1)
    assert forward.forward(route, p, raw, "a@marshellis.com") == "duplicate"
    assert route.sent == []


@pytest.mark.parametrize("email", ["x@other-ws.org", "x@team.other-ws.org"])
def test_forward_to_another_canopy_mail_domain_is_a_loop(route, email):
    u2 = User.objects.create_user("o", "o@o.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="O", created_by=u2)
    MailDomain.objects.create(workspace=ws2, name="other-ws.org", status="pending")
    route.forward_to.email = email
    route.forward_to.save()
    raw = (FIX / "plain.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "dropped:loop"
    assert route.sent == []
