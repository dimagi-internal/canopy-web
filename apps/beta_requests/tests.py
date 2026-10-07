"""POST /api/beta-requests — the public site's closed-beta form.

The request is KEPT whatever happens to the email, the form cannot be used to
learn who has already asked, and it grants nothing.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings

from apps.beta_requests.models import BetaRequest

URL = "/api/beta-requests"


def _post(client: Client, ip: str = "203.0.113.7", **body):
    payload = {"email": "ada@example.org", "reason": "We run CHWs and want agent oversight.", **body}
    return client.post(URL, payload, content_type="application/json", HTTP_X_FORWARDED_FOR=ip)


@pytest.fixture
def client():
    return Client()


@override_settings(REQUIRE_AUTH=True, CANOPY_BETA_REQUESTS_TO="jj@example.org")
def test_anonymous_request_is_kept_and_mailed(client, db):
    resp = _post(client, email="Ada@Example.org")
    assert resp.status_code == 200, resp.content
    assert resp.json() == {"ok": True}

    req = BetaRequest.objects.get()
    assert req.email == "ada@example.org"
    assert req.notify_result == "sent"
    assert req.client_ip == "203.0.113.7"

    [msg] = mail.outbox
    assert msg.to == ["jj@example.org"]
    assert msg.reply_to == ["ada@example.org"]
    assert "We run CHWs" in msg.body
    # The email links to the page where the decision is made, not to a settings tour.
    assert f"/beta-requests/{req.pk}" in msg.body
    assert "Settings → Members" not in msg.body


def test_grants_nothing(client, db):
    _post(client)
    assert not get_user_model().objects.filter(email="ada@example.org").exists()


def test_honeypot_is_answered_and_discarded(client, db):
    resp = _post(client, website="http://spam.example")
    assert resp.json() == {"ok": True}
    assert not BetaRequest.objects.exists()
    assert not mail.outbox


def test_a_repeat_is_kept_but_not_mailed_twice(client, db):
    first, second = _post(client), _post(client)
    assert first.json() == second.json() == {"ok": True}
    assert BetaRequest.objects.count() == 2
    assert len(mail.outbox) == 1
    assert BetaRequest.objects.order_by("created_at").last().notify_result == "skipped"


def test_rate_limited_per_address(client, db):
    for i in range(5):
        assert _post(client, email=f"p{i}@example.org").status_code == 200
    resp = _post(client, email="p6@example.org")
    assert resp.status_code == 429
    assert _post(client, ip="198.51.100.1", email="other@example.org").status_code == 200


def test_mail_failure_keeps_the_request(client, db):
    with mock.patch("apps.beta_requests.services.EmailMultiAlternatives.send",
                    side_effect=RuntimeError("SES down")):
        resp = _post(client)
    assert resp.status_code == 200
    assert BetaRequest.objects.get().notify_result == "failed"


@override_settings(CANOPY_BETA_REQUESTS_TO="")
def test_no_reader_configured_still_keeps_it(client, db):
    assert _post(client).status_code == 200
    assert BetaRequest.objects.get().notify_result == "not_configured"
    assert not mail.outbox


@pytest.mark.parametrize("body", [{"email": "not-an-email"}, {"reason": ""}, {"reason": "x" * 2001}])
def test_invalid_input_is_refused(client, db, body):
    assert _post(client, **body).status_code in (400, 422)
    assert not BetaRequest.objects.exists()



# ---- answering a request: /beta-requests/:id ---------------------------------

REVIEWER = "jj@example.org"


def _signed_in(email: str, *, ws=None, role: str = "owner") -> Client:
    from apps.workspaces.testing import a_member, a_user

    user = a_member(ws, email=email, role=role) if ws is not None else a_user(email)
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def pending(db):
    return BetaRequest.objects.create(email="ada@example.org", reason="CHW oversight")


@pytest.fixture
def ws(db):
    from apps.workspaces.testing import a_workspace

    return a_workspace("partners")


@override_settings(CANOPY_BETA_REQUESTS_TO=REVIEWER)
def test_only_the_reviewer_can_read_a_request(pending, ws):
    assert _signed_in(REVIEWER).get(f"/api/beta-requests/{pending.pk}").json()["email"] == "ada@example.org"
    # A workspace owner who is not the reviewer cannot even learn it exists.
    other = _signed_in("someone@dimagi.com", ws=ws)
    assert other.get(f"/api/beta-requests/{pending.pk}").status_code == 404
    assert other.get("/api/beta-requests").status_code == 404
    assert Client().get(f"/api/beta-requests/{pending.pk}").status_code in (401, 404)


@override_settings(CANOPY_BETA_REQUESTS_TO=REVIEWER)
def test_approve_invites_them_to_the_chosen_workspace(pending, ws):
    from apps.workspaces.models import WorkspaceInvite

    c = _signed_in(REVIEWER, ws=ws)
    resp = c.post(f"/api/beta-requests/{pending.pk}/invite", {"workspace": "partners", "role": "editor"},
                  content_type="application/json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "invited"
    assert body["workspace"] == "partners"
    assert body["email_status"] == "sent"

    inv = WorkspaceInvite.objects.get()
    assert (inv.workspace_id, inv.email, inv.role) == ("partners", "ada@example.org", "editor")
    [msg] = mail.outbox
    assert msg.to == ["ada@example.org"]
    assert f"/invite/{inv.token}" in msg.body

    again = c.post(f"/api/beta-requests/{pending.pk}/invite", {"workspace": "partners", "role": "editor"},
                   content_type="application/json")
    assert again.status_code == 409


@override_settings(CANOPY_BETA_REQUESTS_TO=REVIEWER)
def test_approve_needs_invite_rights_in_that_workspace(pending, ws):
    from apps.workspaces.testing import a_workspace

    a_workspace("elsewhere")
    viewer = _signed_in(REVIEWER, ws=ws, role="viewer")
    url = f"/api/beta-requests/{pending.pk}/invite"
    assert viewer.post(url, {"workspace": "partners", "role": "viewer"},
                       content_type="application/json").status_code == 403
    assert viewer.post(url, {"workspace": "elsewhere", "role": "viewer"},
                       content_type="application/json").status_code == 404
    pending.refresh_from_db()
    assert pending.status == "pending"


@override_settings(CANOPY_BETA_REQUESTS_TO=REVIEWER)
def test_an_admin_cannot_grant_admin(pending, ws):
    admin = _signed_in(REVIEWER, ws=ws, role="admin")
    resp = admin.post(f"/api/beta-requests/{pending.pk}/invite", {"workspace": "partners", "role": "admin"},
                      content_type="application/json")
    assert resp.status_code == 403


@override_settings(CANOPY_BETA_REQUESTS_TO=REVIEWER)
def test_decline_closes_it_and_emails_nobody(pending):
    c = _signed_in(REVIEWER)
    resp = c.post(f"/api/beta-requests/{pending.pk}/decline")
    assert resp.status_code == 200
    assert resp.json()["status"] == "declined"
    assert resp.json()["decided_by"] == REVIEWER
    assert not mail.outbox
    assert [r["id"] for r in c.get("/api/beta-requests?status=pending").json()] == []
