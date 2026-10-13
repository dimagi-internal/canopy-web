from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from django.contrib.auth.models import User

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
    assert msg["Reply-To"]  # the original sender
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
    assert not MailForwardLog.objects.filter(status="forwarded").exists()


def test_malformed_charset_original_still_forwards(route):
    raw = (FIX / "bogus_charset_multipart.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "forwarded"
    out = BytesParser(policy=policy.compat32).parsebytes(route.sent[0]["raw"])
    assert out["X-Original-To"] == "a@marshellis.com"
    assert "via marshellis.com" in out["From"]
