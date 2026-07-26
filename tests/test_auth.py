"""Tests for the Google OAuth auth gate."""
from unittest.mock import Mock

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.common.auth_adapter import CustomSocialAccountAdapter


@pytest.fixture
def auth_client(db):
    """A test Client logged in as a dimagi.com user."""
    User = get_user_model()
    user = User.objects.create_user(
        username="tester",
        email="tester@dimagi.com",
        password="irrelevant",
    )
    client = Client()
    client.force_login(user)
    return client


# ──────────────────────────────────────────────────────────────────────
# LoginRequiredMiddleware
# ──────────────────────────────────────────────────────────────────────


@override_settings(REQUIRE_AUTH=True)
def test_api_requires_auth_returns_401(db):
    client = Client()
    resp = client.get("/api/projects/")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication required"}


@override_settings(REQUIRE_AUTH=True)
def test_page_requires_auth_redirects_to_login(db):
    client = Client()
    resp = client.get("/some-spa-route")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/accounts/google/login/")
    assert "next=%2Fsome-spa-route" in resp["Location"]


@override_settings(REQUIRE_AUTH=True)
def test_login_redirect_next_preserves_query_string(db):
    resp = Client().get("/some-spa-route?tab=syncs")
    assert resp.status_code == 302
    assert "next=%2Fsome-spa-route%3Ftab%3Dsyncs" in resp["Location"]


@override_settings(
    REQUIRE_AUTH=True,
    FORCE_SCRIPT_NAME="/canopy",
    LOGIN_URL="/canopy/accounts/google/login/",
)
def test_login_redirect_next_carries_script_prefix(db):
    # On the labs sub-path deployment the post-login bounce must land back
    # on /canopy/..., not on the root tenant's path.
    resp = Client().get("/w/dimagi/agents")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/canopy/accounts/google/login/")
    assert "next=%2Fcanopy%2Fw%2Fdimagi%2Fagents" in resp["Location"]


@override_settings(REQUIRE_AUTH=True)
def test_health_is_public(db):
    client = Client()
    resp = client.get("/health/")
    assert resp.status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_csrf_endpoint_is_public(db):
    client = Client()
    resp = client.get("/api/csrf/")
    assert resp.status_code == 200
    assert "csrftoken" in resp.cookies


@override_settings(REQUIRE_AUTH=True)
def test_accounts_paths_are_public(db):
    client = Client()
    resp = client.get("/accounts/login/")
    assert resp.status_code in (200, 302)  # allauth may redirect, but not to our login


@override_settings(REQUIRE_AUTH=True)
def test_me_returns_401_when_unauthenticated(db):
    client = Client()
    resp = client.get("/api/me/")
    assert resp.status_code == 401


@override_settings(REQUIRE_AUTH=True)
def test_me_returns_user_when_authenticated(auth_client):
    resp = auth_client.get("/api/me/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "tester@dimagi.com"


@override_settings(REQUIRE_AUTH=True)
def test_authenticated_user_can_hit_api(auth_client):
    resp = auth_client.get("/api/projects/")
    assert resp.status_code == 200


@override_settings(REQUIRE_AUTH=False)
def test_auth_can_be_disabled(db):
    """When REQUIRE_AUTH=False, the middleware no longer redirects or 401s
    anonymous requests. Public paths like /health/ and /api/csrf/ remain
    accessible; Ninja's own session_auth still enforces per-route auth on
    protected routes, but the middleware gate is bypassed."""
    client = Client()
    # Middleware gate is off — public endpoints accessible without auth
    resp = client.get("/health/")
    assert resp.status_code == 200
    # CSRF endpoint also accessible
    resp = client.get("/api/csrf/")
    assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# CustomSocialAccountAdapter
# ──────────────────────────────────────────────────────────────────────


def _make_social_login(email: str) -> Mock:
    sociallogin = Mock()
    sociallogin.account = Mock()
    sociallogin.account.extra_data = {"email": email}
    # Real allauth SocialLogin always populates `.email_addresses` (possibly
    # empty) by the time `pre_social_login` runs; default to none-verified so
    # a bare double never accidentally exercises the invite-admission branch.
    sociallogin.email_addresses = []
    return sociallogin


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_accepts_dimagi_email(rf):
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("alice@dimagi.com")
    # Should not raise
    adapter.pre_social_login(request, sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_other_email(rf, db):
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("mallory@gmail.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(request, sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_substring_match(rf, db):
    """Someone@evildimagi.com must not be treated as dimagi.com."""
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("attacker@evildimagi.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(request, sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_missing_email(rf):
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("")
    with pytest.raises(ImmediateHttpResponse):
        adapter.pre_social_login(request, sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="")
def test_adapter_allows_any_when_domain_unset(rf):
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("anyone@example.com")
    # Should not raise
    adapter.pre_social_login(request, sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_case_insensitive(rf):
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("Alice@DIMAGI.COM")
    # Should not raise
    adapter.pre_social_login(request, sociallogin)


def _make_jit_social_login(email: str, *, verified: bool = True, is_existing: bool = False) -> Mock:
    """A sociallogin for a real (not-yet-connected) Google login attempt, as
    seen by `pre_social_login` after allauth's own `sociallogin.lookup()` has
    already run (see apps/common/auth_adapter.py's F2 discussion)."""
    sociallogin = _make_social_login(email)
    sociallogin.is_existing = is_existing
    email_address = Mock()
    email_address.email = email
    email_address.verified = verified
    sociallogin.email_addresses = [email_address]
    sociallogin.connect = Mock()
    return sociallogin


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_connects_jit_user_on_later_google_login(db):
    """F2: a JIT-provisioned delegated-identity user (bare User + verified
    allauth EmailAddress, minted by apps/tokens/exchange_api.py, no
    SocialAccount) must CONNECT to the SAME user on a later real Google login
    for that email — not fork or block on allauth's duplicate-email path."""
    from allauth.account.models import EmailAddress

    User = get_user_model()
    jit_user = User.objects.create_user(username="jit@dimagi.com", email="jit@dimagi.com")
    EmailAddress.objects.create(user=jit_user, email="jit@dimagi.com", verified=True, primary=True)

    adapter = CustomSocialAccountAdapter()
    request = Mock()
    sociallogin = _make_jit_social_login("jit@dimagi.com")

    adapter.pre_social_login(request, sociallogin)

    sociallogin.connect.assert_called_once_with(request, jit_user)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_does_not_connect_already_existing_sociallogin(db):
    """is_existing=True means this SocialAccount is already linked — must not
    re-connect (that would be allauth's own job, not ours)."""
    User = get_user_model()
    User.objects.create_user(username="jit@dimagi.com", email="jit@dimagi.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("jit@dimagi.com", is_existing=True)

    adapter.pre_social_login(Mock(), sociallogin)

    sociallogin.connect.assert_not_called()


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_does_not_connect_unverified_provider_email(db):
    """Only a PROVIDER-verified email may trigger auto-connect — a bare claim
    on sociallogin.account.extra_data is not sufficient."""
    User = get_user_model()
    jit_user = User.objects.create_user(username="jit@dimagi.com", email="jit@dimagi.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("jit@dimagi.com", verified=False)

    adapter.pre_social_login(Mock(), sociallogin)

    sociallogin.connect.assert_not_called()
    assert User.objects.get(pk=jit_user.pk).email == "jit@dimagi.com"


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_skips_connect_on_no_matching_user(db):
    """No local user with that email yet — nothing to connect to; allauth's
    normal signup flow proceeds untouched."""
    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("nobody-yet@dimagi.com")

    adapter.pre_social_login(Mock(), sociallogin)

    sociallogin.connect.assert_not_called()


def _make_invite(email, *, workspace_slug="acme", expired=False, revoked=False, accepted=False):
    """A live (or not-so-live) WorkspaceInvite addressed to `email`, for
    exercising the invite-aware login gate directly against the DB."""
    import datetime as dt

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.workspaces.models import Workspace
    from apps.workspaces.services import accept_invite, create_invite, revoke_invite

    User = get_user_model()
    owner = User.objects.create_user(username="owner@dimagi.com", email="owner@dimagi.com")
    ws, _ = Workspace.objects.get_or_create(
        slug=workspace_slug, defaults={"display_name": workspace_slug.title(), "created_by": owner}
    )
    inv = create_invite(workspace=ws, email=email, role="editor", invited_by=owner)
    if expired:
        inv.expires_at = timezone.now() - dt.timedelta(days=1)
        inv.save(update_fields=["expires_at"])
    elif revoked:
        revoke_invite(invite=inv)
    elif accepted:
        invitee = User.objects.create_user(username=f"{email}-invitee", email=email)
        accept_invite(token=inv.token, user=invitee)
        inv.refresh_from_db()
    return inv


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_admits_outside_domain_email_with_pending_invite(db):
    """The security-sensitive case this task exists for: a non-Dimagi email
    with a live workspace invite must be admitted past the domain gate."""
    _make_invite("guest@external.com")

    adapter = CustomSocialAccountAdapter()
    request = Mock()
    sociallogin = _make_jit_social_login("guest@external.com")

    # Should not raise ImmediateHttpResponse(403) despite the domain mismatch.
    adapter.pre_social_login(request, sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_login_does_not_consume_the_invite(db):
    """Logging in must NOT accept/consume the invite — only the explicit
    accept-invite call may do that. The invite stays pending after login."""
    inv = _make_invite("guest@external.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com")
    adapter.pre_social_login(Mock(), sociallogin)

    inv.refresh_from_db()
    assert inv.accepted_at is None
    assert inv.is_pending()


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_outside_domain_email_with_expired_invite(db):
    """Uses a PROVIDER-verified claim (jit social login) so this isolates the
    invite-state check itself, not F2's separate verified-email requirement."""
    _make_invite("guest@external.com", expired=True)

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(Mock(), sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_outside_domain_email_with_revoked_invite(db):
    _make_invite("guest@external.com", revoked=True)

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(Mock(), sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_admits_accepted_invitee_on_next_login(db):
    """F4: `pending_invite_for_email` alone would 403 an accepted invitee on
    their very NEXT login (accepting clears `pending`) — the right check is
    member-OR-pending-invite. Confirm the login is admitted via the
    resulting WorkspaceMembership even though no pending invite remains."""
    from apps.workspaces.services import pending_invite_for_email

    inv = _make_invite("guest@external.com", accepted=True)
    assert inv.accepted_at is not None
    assert pending_invite_for_email("guest@external.com") is None  # sanity: truly consumed

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com")
    # Should not raise despite there being no pending invite anymore.
    adapter.pre_social_login(Mock(), sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_after_last_membership_removed(db):
    """F4's other half: admission is derived from CURRENT membership, not a
    one-time fact — removing a member's last WorkspaceMembership must revoke
    their standing on their NEXT login (though not an already-open session,
    which this login-time gate cannot reach)."""
    from apps.workspaces.models import WorkspaceMembership

    inv = _make_invite("guest@external.com", accepted=True)
    WorkspaceMembership.objects.filter(
        workspace=inv.workspace, user__email__iexact="guest@external.com"
    ).delete()

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(Mock(), sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_outside_domain_email_with_no_invite_at_all(db):
    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("nobody@external.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(Mock(), sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_unverified_claim_even_with_pending_invite(db):
    """F2: the raw, self-asserted `extra_data["email"]` claim is not proof of
    control — only a PROVIDER-verified email may ride an invite/membership
    past the domain gate. An unverified claim matching a live invite must
    still be rejected, or anyone could assert an address they don't control."""
    _make_invite("guest@external.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("guest@external.com", verified=False)
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(Mock(), sociallogin)
    assert exc.value.response.status_code == 403


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_does_not_connect_invite_admitted_login_to_existing_user(db):
    """F3: an invite-admitted login must NEVER run the JIT-identity merge —
    it would connect to ANY existing local User sharing that email,
    including a non-allowlisted machine/service account (e.g. one minted by
    `manage.py create_token --create-user --email ...`) that may hold
    unrelated, more-privileged WorkspaceMemberships or PAT-minting rights.
    Simulate inviting an address that already has such a bare local User."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    machine_user = User.objects.create_user(
        username="agent@external.com", email="agent@external.com"
    )
    _make_invite("agent@external.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_jit_social_login("agent@external.com")
    adapter.pre_social_login(Mock(), sociallogin)

    sociallogin.connect.assert_not_called()
    # And no membership/state on the machine user was touched as a side effect.
    assert User.objects.get(pk=machine_user.pk).email == "agent@external.com"


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_allowlisted_domain_login_unaffected_by_invite_check(db):
    """F7: an allowlisted-domain login must not even CONSULT
    `email_admitted_outside_domain` — prove it directly (not just via the
    observable "did not raise" behavior), since the correct implementation
    short-circuits before ever reaching the invite/membership lookup."""
    from unittest.mock import patch

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_social_login("alice@dimagi.com")
    with patch("apps.common.auth_adapter.email_admitted_outside_domain") as mocked:
        adapter.pre_social_login(Mock(), sociallogin)  # should not raise
    mocked.assert_not_called()


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_adapter_rejects_missing_email_even_with_invite_table_populated(db):
    """A blank email must still be rejected — `email_admitted_outside_domain`
    must never be treated as a match on a blank lookup (guards against a bug
    where an empty string somehow matches an unrelated row)."""
    _make_invite("someone@external.com")

    adapter = CustomSocialAccountAdapter()
    sociallogin = _make_social_login("")
    with pytest.raises(ImmediateHttpResponse):
        adapter.pre_social_login(Mock(), sociallogin)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_rejection_page_shows_email_domain_and_contact(rf, db):
    """The rejection response must tell the user their email, the allowed
    domain, and a way to request access — otherwise it's a dead end."""
    adapter = CustomSocialAccountAdapter()
    request = rf.get("/accounts/google/login/callback/")
    sociallogin = _make_social_login("mallory@gmail.com")
    with pytest.raises(ImmediateHttpResponse) as exc:
        adapter.pre_social_login(request, sociallogin)
    body = exc.value.response.content.decode()
    assert "mallory@gmail.com" in body
    assert "dimagi.com" in body
    assert "mailto:jjackson@dimagi.com" in body
    assert "accounts.google.com/Logout" in body


# ──────────────────────────────────────────────────────────────────────
# CSRF enforcement on state-mutating endpoints
# ──────────────────────────────────────────────────────────────────────


@override_settings(REQUIRE_AUTH=True)
def test_post_without_csrf_rejected(db):
    """When enforce_csrf_checks=True, state-mutating POSTs without a token fail."""
    User = get_user_model()
    user = User.objects.create_user(username="tester", email="tester@dimagi.com")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    resp = client.post("/api/projects/", data={}, content_type="application/json")
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# _is_review_link: per-token review public links bypass the middleware
# ──────────────────────────────────────────────────────────────────────


@override_settings(REQUIRE_AUTH=True)
def test_review_spa_page_is_accessible_unauthenticated(db):
    """Unauthenticated request to /review/<uuid>/ passes through the middleware
    (the SPA shell is served; the endpoint itself handles ?t= token auth)."""
    import uuid

    rid = uuid.uuid4()
    client = Client()
    resp = client.get(f"/review/{rid}/")
    # Must NOT redirect to login — the middleware must let it through.
    # The SPA catch-all returns 200; any non-302/non-401 confirms the gate is open.
    assert resp.status_code != 302
    assert resp.status_code != 401


@override_settings(REQUIRE_AUTH=True)
def test_review_api_detail_is_accessible_unauthenticated(db):
    """Unauthenticated GET /api/reviews/<uuid>/ passes through the middleware
    and reaches the endpoint (which does its own token/auth check)."""
    import uuid

    rid = uuid.uuid4()
    client = Client()
    resp = client.get(f"/api/reviews/{rid}/")
    # The endpoint returns 404 (review not found), not 401 from middleware.
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_review_api_submit_is_accessible_unauthenticated(db):
    """Unauthenticated POST /api/reviews/<uuid>/submit/ passes through the middleware
    and reaches the endpoint (which does its own token/auth check)."""
    import uuid

    rid = uuid.uuid4()
    client = Client()
    resp = client.post(
        f"/api/reviews/{rid}/submit/",
        data={"response_json": {}},
        content_type="application/json",
    )
    # The endpoint returns 404 (review not found), not 401 from middleware.
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_review_api_create_still_requires_auth(db):
    """Unauthenticated POST /api/reviews/ (bare create) is still blocked by
    the middleware — creating a review requires a session or PAT."""
    client = Client()
    resp = client.post(
        "/api/reviews/",
        data={"request_json": {}, "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 401
