"""The OAuth rules both of canopy's authorization servers share (`apps/common/oauth_core.py`).

End-to-end coverage of each server is in tests/test_mcp_oauth.py and
tests/test_hcp_service.py; these pin the shared rules themselves.
"""
from __future__ import annotations

import datetime as dt
import secrets

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.common import oauth_core
from apps.common.oauth_core import OAuthError
from apps.oauth_server.models import OAuthAuthorizationCode, OAuthClient
from apps.tokens.models import hash_secret

pytestmark = pytest.mark.django_db


# -- PKCE -------------------------------------------------------------------------------


def test_a_well_formed_verifier_that_hashes_to_the_challenge_passes():
    verifier = secrets.token_urlsafe(48)
    oauth_core.verify_pkce(verifier, oauth_core.s256(verifier))


@pytest.mark.parametrize("verifier, code", [
    ("", "invalid_request"),                       # missing
    ("abc", "invalid_grant"),                      # under 43 characters (RFC 7636 §4.1)
    ("a" * 129, "invalid_grant"),                  # over 128
    ("a" * 42 + "!", "invalid_grant"),             # not unreserved
])
def test_a_malformed_verifier_is_refused_even_when_it_hashes(verifier, code):
    with pytest.raises(OAuthError) as e:
        oauth_core.verify_pkce(verifier, oauth_core.s256(verifier))
    assert e.value.code == code


def test_a_verifier_for_another_challenge_is_refused():
    with pytest.raises(OAuthError) as e:
        oauth_core.verify_pkce(secrets.token_urlsafe(48), oauth_core.s256(secrets.token_urlsafe(48)))
    assert e.value.code == "invalid_grant"


def test_the_challenge_must_be_an_s256_digest():
    good = oauth_core.s256(secrets.token_urlsafe(48))
    assert oauth_core.check_challenge(good, "S256") is None
    assert oauth_core.check_challenge("", "S256")
    assert oauth_core.check_challenge(good, "plain")
    assert oauth_core.check_challenge(good[:-1], "S256")


# -- redirect_uri at the token endpoint (RFC 6749 §4.1.3) ------------------------------------


def test_a_redirect_uri_named_at_authorization_must_be_named_identically():
    oauth_core.check_redirect_uri("https://app.example/cb", "https://app.example/cb")
    for presented in ("", "https://app.example/other"):
        with pytest.raises(OAuthError):
            oauth_core.check_redirect_uri(presented, "https://app.example/cb")


def test_a_redirect_uri_not_named_at_authorization_is_not_required():
    oauth_core.check_redirect_uri("", "")


# -- one-time redemption ---------------------------------------------------------------------


@pytest.fixture()
def code_row():
    user = get_user_model().objects.create_user("u", "u@dimagi.com", "pw")
    client = OAuthClient.objects.create(client_id="mcp_x", redirect_uris=["https://a.example/cb"])
    raw = secrets.token_urlsafe(32)
    row = OAuthAuthorizationCode.objects.create(
        code_hash=hash_secret(raw), client=client, user=user, redirect_uri="",
        code_challenge="c", expires_at=timezone.now() + dt.timedelta(minutes=5))
    return raw, row


def _redeem(raw, row, *, use, revoked, is_spent=lambda r: r.used_at is not None, client_pk=None):
    return oauth_core.redeem_once(
        raw, client_pk=row.client_id if client_pk is None else client_pk, what="code",
        find=lambda r: OAuthAuthorizationCode.objects.filter(code_hash=hash_secret(r)).first(),
        is_spent=is_spent, expires_at=lambda r: r.expires_at, use=use,
        revoke=lambda r: revoked.append(r.pk))


def _mark_used(row):
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    return {"ok": True}


def test_a_credential_is_redeemed_once_and_a_replay_revokes(code_row):
    raw, row = code_row
    revoked: list = []
    assert _redeem(raw, row, use=_mark_used, revoked=revoked) == {"ok": True}
    with pytest.raises(OAuthError) as e:
        _redeem(raw, row, use=_mark_used, revoked=revoked)
    assert e.value.code == "invalid_grant" and revoked == [row.pk]


def test_a_redemption_that_finds_it_spent_under_the_lock_is_a_replay(code_row):
    """The race: unlocked it looked fresh, but a concurrent request redeemed it
    first. Spent-ness is read only under the lock, so that is a second
    presentation too, and it revokes."""
    raw, row = code_row
    revoked: list = []
    OAuthAuthorizationCode.objects.filter(pk=row.pk).update(used_at=timezone.now())
    with pytest.raises(OAuthError):
        _redeem(raw, row, use=_mark_used, revoked=revoked, is_spent=lambda r: r.used_at is not None)
    assert revoked == [row.pk]


def test_a_replay_revocation_survives_the_refusal(code_row):
    """The revocation must not run inside the refused exchange's transaction."""
    raw, row = code_row
    OAuthAuthorizationCode.objects.filter(pk=row.pk).update(used_at=timezone.now())

    def revoke(r):
        OAuthAuthorizationCode.objects.filter(pk=r.pk).update(scope="revoked")

    with pytest.raises(OAuthError):
        oauth_core.redeem_once(
            raw, client_pk=row.client_id, what="code",
            find=lambda x: OAuthAuthorizationCode.objects.filter(code_hash=hash_secret(x)).first(),
            is_spent=lambda r: r.used_at is not None, expires_at=lambda r: r.expires_at,
            use=_mark_used, revoke=revoke)
    assert OAuthAuthorizationCode.objects.get(pk=row.pk).scope == "revoked"


def test_another_clients_credential_is_unknown_and_revokes_nothing(code_row):
    raw, row = code_row
    revoked: list = []
    with pytest.raises(OAuthError) as e:
        _redeem(raw, row, use=_mark_used, revoked=revoked, client_pk=-1)
    assert e.value.code == "invalid_grant" and revoked == []


def test_an_expired_credential_is_refused_and_not_spent(code_row):
    raw, row = code_row
    OAuthAuthorizationCode.objects.filter(pk=row.pk).update(expires_at=timezone.now())
    with pytest.raises(OAuthError) as e:
        _redeem(raw, row, use=_mark_used, revoked=[])
    assert "expired" in e.value.description
    assert OAuthAuthorizationCode.objects.get(pk=row.pk).used_at is None


def test_a_refusal_inside_use_rolls_back_and_leaves_it_unspent(code_row):
    raw, row = code_row

    def use(r):
        _mark_used(r)
        raise OAuthError("invalid_grant", "PKCE code_verifier does not match")

    with pytest.raises(OAuthError):
        _redeem(raw, row, use=use, revoked=[])
    assert OAuthAuthorizationCode.objects.get(pk=row.pk).used_at is None


def test_errors_go_on_the_wire_uncached():
    resp = oauth_core.error_response(OAuthError("invalid_client", "nope", status=401))
    assert resp.status_code == 401 and resp["Cache-Control"] == "no-store"
