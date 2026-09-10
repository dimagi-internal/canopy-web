"""First-time Google sign-in at an allowed domain must CREATE the account.

Local username/password signup is closed (test_local_signup_closed.py) by
CustomAccountAdapter.is_open_for_signup returning False. allauth's social adapter
delegates ITS OWN signup gate to that same account adapter, so closing the form
also closed first-time Google sign-in: pre_social_login admitted the allowlisted
email, then process_signup asked is_open_for_signup, got False, and rendered
"Sign Up Closed" to every user who did not already have a row — observed
2026-09-10 when a Dimagi associate was locked out of every /canopy/ review link
(canopy-web#740). Social sign-in is the ONLY legitimate account-creation path and
pre_social_login already gates it (domain allowlist or live invite, on a
provider-verified email), so it must stay open while the local form stays closed.

These tests drive allauth's real flow (`complete_social_login`), the way allauth's
own suite does, rather than asserting on the adapter method in isolation.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware

pytestmark = pytest.mark.django_db


def _google_login(email: str) -> SocialLogin:
    """A SocialLogin shaped like allauth's Google callback for a brand-new user:
    no User row yet, provider-verified email."""
    User = get_user_model()
    user = User(username=email.split("@")[0], email=email)
    account = SocialAccount(
        provider="google",
        uid="118273645509",
        extra_data={"email": email, "email_verified": True},
    )
    sociallogin = SocialLogin(user=user, account=account)
    sociallogin.email_addresses = [EmailAddress(email=email, verified=True, primary=True)]
    return sociallogin


def _callback_request(rf):
    request = rf.get("/accounts/google/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()
    return request


def test_first_google_login_at_allowed_domain_creates_the_user(rf, settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com,dimagi-associate.com"
    email = "newcomer@dimagi-associate.com"
    User = get_user_model()
    assert not User.objects.filter(email__iexact=email).exists()

    resp = complete_social_login(_callback_request(rf), _google_login(email))

    body = getattr(resp, "content", b"")
    assert b"Sign Up Closed" not in body, "social sign-up is closed for a new allowlisted user"
    assert resp.status_code == 302, body[:300]
    assert User.objects.filter(email__iexact=email).exists()


def test_first_google_login_outside_allowlist_is_still_rejected(rf, settings):
    """Opening social signup must not weaken the domain gate: pre_social_login
    still refuses a non-allowlisted, non-invited email before signup is reached."""
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"
    email = "stranger@example.org"

    resp = complete_social_login(_callback_request(rf), _google_login(email))

    assert resp.status_code == 403
    assert not get_user_model().objects.filter(email__iexact=email).exists()


def test_local_form_stays_closed_alongside():
    from allauth.account.adapter import get_adapter

    assert get_adapter().is_open_for_signup(None) is False
