"""Site operators: granting and revoking superuser, and their alert mail.

Superuser holds the fleet-wide switches (hold, stop everything, this page) and no
workspace access. Granting it is superuser-only and passkey-confirmed; the last one
cannot be removed, or nobody could release a fleet hold. Which alert mail reaches a
person is separate from both (`push.NotificationPreference.alert_mail`).
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.passkeys.testing import grant_step_up
from apps.push.models import NotificationPreference, alert_mail_for
from apps.push.services import alert_mail_recipients

pytestmark = pytest.mark.django_db


def _client(user, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


@pytest.fixture()
def jj():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", is_superuser=True)


@pytest.fixture()
def lucina():
    return User.objects.create_user("lucina", "lucina@dimagi.com", "pw")


def test_a_superuser_grants_and_lists_operators(jj, lucina):
    c = _client(jj)
    resp = c.post("/api/system/operators", data={"email": "Lucina@dimagi.com"}, content_type="application/json")

    assert resp.status_code == 201
    assert resp.json()["email"] == "lucina@dimagi.com" and resp.json()["alert_mail"] == "all"
    lucina.refresh_from_db()
    assert lucina.is_superuser
    assert [o["email"] for o in c.get("/api/system/operators").json()] == ["jj@dimagi.com", "lucina@dimagi.com"]


def test_granting_someone_who_never_signed_in_is_a_404(jj):
    resp = _client(jj).post("/api/system/operators", data={"email": "nobody@dimagi.com"},
                            content_type="application/json")
    assert resp.status_code == 404


def test_granting_needs_a_fresh_passkey(jj, lucina):
    resp = _client(jj, step_up=False).post("/api/system/operators", data={"email": "lucina@dimagi.com"},
                                           content_type="application/json")
    assert resp.status_code == 403
    lucina.refresh_from_db()
    assert not lucina.is_superuser


def test_a_non_superuser_can_neither_list_nor_grant(lucina):
    c = _client(lucina)
    assert c.get("/api/system/operators").status_code == 403
    assert c.post("/api/system/operators", data={"email": "lucina@dimagi.com"},
                  content_type="application/json").status_code == 403


def test_revoke_and_the_last_one_stays(jj, lucina):
    c = _client(jj)
    c.post("/api/system/operators", data={"email": "lucina@dimagi.com"}, content_type="application/json")

    assert c.delete(f"/api/system/operators/{lucina.pk}").status_code == 204
    lucina.refresh_from_db()
    assert not lucina.is_superuser
    assert c.delete(f"/api/system/operators/{jj.pk}").status_code == 409


def test_a_superuser_sets_an_operators_alert_mail(jj, lucina):
    c = _client(jj)
    c.post("/api/system/operators", data={"email": "lucina@dimagi.com"}, content_type="application/json")

    resp = c.patch(f"/api/system/operators/{lucina.pk}", data={"alert_mail": "assigned"},
                   content_type="application/json")

    assert resp.status_code == 200 and resp.json()["alert_mail"] == "assigned"
    assert alert_mail_for(lucina) == "assigned"


def test_anyone_sets_their_own_alert_mail_without_touching_quiet_minutes(lucina):
    c = _client(lucina, step_up=False)
    c.patch("/api/push/preferences", data={"session_idle_minutes": 12}, content_type="application/json")

    body = c.patch("/api/push/preferences", data={"alert_mail": "assigned"},
                   content_type="application/json").json()

    assert body == {"session_idle_minutes": 12, "alert_mail": "assigned"}


def test_access_request_mail_skips_assigned_only_admins(jj, lucina):
    NotificationPreference.objects.create(user=lucina, alert_mail="assigned")
    assert alert_mail_recipients([jj, lucina]) == [jj]
    assert alert_mail_recipients([lucina]) == [lucina]
