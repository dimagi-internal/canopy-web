"""allauth must never send email.

Every identity here comes from Google, so none of allauth's mail (password
reset, "no account with that address", email confirmation) has a legitimate
recipient. It only became reachable when canopy got a real mail backend for
workspace invites: a password-reset POST would then mint a working link that
lets a Google-only account set a password (a login that skips Google and the
domain gate), and the unknown-account notice lets anyone make canopy mail an
arbitrary address from the shared labs SES domain.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("address", ["known@dimagi.com", "stranger@example.org"])
def test_password_reset_sends_nothing(address):
    user = get_user_model().objects.create(username="known", email="known@dimagi.com")
    user.set_unusable_password()
    user.save()
    mail.outbox.clear()

    Client().post("/accounts/password/reset/", data={"email": address})

    assert mail.outbox == []
