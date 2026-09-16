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
from allauth.core.context import request_context
from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _google_app_configured(settings):
    """Give the Google provider real credentials, as production has.

    allauth 65 resolves `sociallogin.provider` through the configured app and
    dereferences `provider.app` on its email-lookup path. The test environment
    leaves GOOGLE_OAUTH_CLIENT_ID empty, so no provider resolved and the flow
    raised `AttributeError: 'NoneType' object has no attribute 'app'` — which
    looks like a library break and is really the fixture being less configured
    than every real deployment.

    Setting it here makes these tests MORE faithful to production, not less:
    the flow they drive is the one a real Google callback takes.
    """
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {
            "APP": {"client_id": "test-client-id", "secret": "test-secret", "key": ""},
            "SCOPE": ["profile", "email"],
            "AUTH_PARAMS": {"access_type": "online"},
        }
    }


def _google_login(email: str, request) -> SocialLogin:
    """A SocialLogin shaped like allauth's Google callback for a brand-new user:
    no User row yet, provider-verified email.

    The `provider=` argument is required as of allauth 65: `SocialLogin` no
    longer derives one from the account, and the email-lookup path
    (`_lookup_by_email` -> `authenticate_by_email`) dereferences `provider.app`.
    Constructing without it raised `AttributeError: 'NoneType' object has no
    attribute 'app'` from inside allauth — a break that reads as a library bug
    and is really this helper building something a real callback never produces.
    Resolving it through the adapter is exactly what allauth's own callback does.
    """
    User = get_user_model()
    user = User(username=email.split("@")[0], email=email)
    account = SocialAccount(
        provider="google",
        uid="118273645509",
        extra_data={"email": email, "email_verified": True},
    )
    provider = get_social_adapter().get_provider(request, "google")
    sociallogin = SocialLogin(user=user, account=account, provider=provider)
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

    request = _callback_request(rf)
    # allauth 65 reads the current request from a ContextVar that its own
    # `AccountMiddleware` sets on every real request (it is in MIDDLEWARE —
    # config/settings/base.py). Without it the login stages reach
    # `is_login_by_code_required` and hit `None.session`. Entering the same
    # context here matches production rather than accommodating the library.
    with request_context(request):
        resp = complete_social_login(request, _google_login(email, request))

    body = getattr(resp, "content", b"")
    assert b"Sign Up Closed" not in body, "social sign-up is closed for a new allowlisted user"
    assert resp.status_code == 302, body[:300]
    assert User.objects.filter(email__iexact=email).exists()


def test_first_google_login_outside_allowlist_is_still_rejected(rf, settings):
    """Opening social signup must not weaken the domain gate: pre_social_login
    still refuses a non-allowlisted, non-invited email before signup is reached."""
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"
    email = "stranger@example.org"

    request = _callback_request(rf)
    # allauth 65 reads the current request from a ContextVar that its own
    # `AccountMiddleware` sets on every real request (it is in MIDDLEWARE —
    # config/settings/base.py). Without it the login stages reach
    # `is_login_by_code_required` and hit `None.session`. Entering the same
    # context here matches production rather than accommodating the library.
    with request_context(request):
        resp = complete_social_login(request, _google_login(email, request))

    assert resp.status_code == 403
    assert not get_user_model().objects.filter(email__iexact=email).exists()


def test_local_form_stays_closed_alongside():
    from allauth.account.adapter import get_adapter

    assert get_adapter().is_open_for_signup(None) is False
