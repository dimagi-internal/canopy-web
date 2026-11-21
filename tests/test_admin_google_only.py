"""The Django admin opens only to a staff person signed in through Google
(`apps/common/admin_access.py`): no stored password opens it."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from tests.token_helpers import bearer_client

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture()
def superuser():
    return User.objects.create_superuser("root", "root@dimagi.com", "a-real-password")


def test_the_admin_login_form_is_gone(superuser):
    c = Client()
    r = c.get("/admin/login/?next=/admin/")
    assert r.status_code == 302 and r["Location"].startswith("/accounts/google/login/")
    # Posting the password the form used to take signs nobody in.
    r = c.post("/admin/login/?next=/admin/", {"username": "root", "password": "a-real-password"})
    assert r.status_code == 302 and r["Location"].startswith("/accounts/google/login/")
    assert c.get("/admin/").status_code == 302
    assert "_auth_user_id" not in c.session


def test_the_allauth_password_form_signs_nobody_in(superuser):
    c = Client()
    c.post("/accounts/login/", {"login": "root@dimagi.com", "password": "a-real-password"})
    assert "_auth_user_id" not in c.session


def test_a_staff_browser_session_opens_the_admin(superuser):
    c = Client()
    c.force_login(superuser)
    assert c.get("/admin/").status_code == 200
    r = c.get("/admin/login/?next=/admin/auth/user/")
    assert r.status_code == 302 and r["Location"] == "/admin/auth/user/"


def test_a_non_staff_person_is_refused_without_a_loop(db):
    c = Client()
    c.force_login(User.objects.create_user("pat", "pat@dimagi.com"))
    assert c.get("/admin/").status_code == 302                     # to admin:login
    assert c.get("/admin/login/?next=/admin/").status_code == 403


def test_a_superusers_token_does_not_open_the_admin(superuser):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=superuser, label="t")
    assert bearer_client(raw).get("/admin/").status_code != 200


def test_the_login_never_redirects_off_site(superuser):
    c = Client()
    c.force_login(superuser)
    r = c.get("/admin/login/?next=https://evil.example/")
    assert r.status_code == 302 and r["Location"] == "/admin/"
