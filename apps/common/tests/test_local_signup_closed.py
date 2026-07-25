"""Local username/password signup must be closed.

allauth's URLconf is mounted whole, which ships an OPEN /accounts/signup/ by
default. That route bypasses CustomSocialAccountAdapter entirely — so before
this was closed, a stranger could register ANY address (including one at an
allowlisted domain), pick their own password, and be auto-joined as an EDITOR
of every workspace trusting that domain, without ever touching Google. Every
legitimate identity here is provider-issued or provisioned server-side with an
unusable password, so the form has no legitimate user.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db


def test_signup_page_offers_no_form():
    # allauth renders its "signup closed" template with a 200, so assert on the
    # thing that matters: no credential form is offered.
    html = Client().get("/accounts/signup/").content.decode()
    for field in ('name="password1"', 'name="password2"', 'name="username"'):
        assert field not in html, f"signup form still exposes {field}"


def test_signup_post_creates_no_user():
    User = get_user_model()
    before = User.objects.count()
    Client().post(
        "/accounts/signup/",
        data={
            "username": "intruder",
            "email": "intruder@dimagi.com",
            "password1": "hunter2-hunter2",
            "password2": "hunter2-hunter2",
        },
    )
    assert User.objects.count() == before
    assert not User.objects.filter(email__iexact="intruder@dimagi.com").exists()


def test_account_adapter_reports_signup_closed():
    from allauth.account.adapter import get_adapter

    assert get_adapter().is_open_for_signup(None) is False
