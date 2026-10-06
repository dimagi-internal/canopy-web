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
