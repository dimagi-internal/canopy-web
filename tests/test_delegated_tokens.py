import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.tokens.models import AppCredential, DelegatedToken

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("u", "u@dimagi.com", "pw")


def test_app_credential_lookup_roundtrip(user):
    raw, cred = AppCredential.create_credential(
        name="ace-web", domains=["dimagi.com"], created_by=user)
    assert AppCredential.lookup(raw).pk == cred.pk
    assert AppCredential.lookup("nope") is None
    cred.revoked_at = timezone.now()
    cred.save(update_fields=["revoked_at"])
    assert AppCredential.lookup(raw) is None


def test_delegated_token_expires(user):
    _, cred = AppCredential.create_credential(name="a", domains=[], created_by=user)
    raw, tok = DelegatedToken.issue(app=cred, user=user, ttl_seconds=60)
    assert DelegatedToken.lookup(raw).user_id == user.pk
    DelegatedToken.objects.filter(pk=tok.pk).update(
        expires_at=timezone.now() - timezone.timedelta(seconds=1))
    assert DelegatedToken.lookup(raw) is None


def test_middleware_rejects_delegated_token_for_deactivated_user(user):
    """F1: a delegated token minted before deactivation must stop authenticating
    REST once the user is deactivated (BearerTokenAuthMiddleware guard)."""
    _, cred = AppCredential.create_credential(name="a", domains=[], created_by=user)
    raw, _ = DelegatedToken.issue(app=cred, user=user, ttl_seconds=600)

    ok = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert ok.status_code == 200

    user.is_active = False
    user.save(update_fields=["is_active"])

    denied = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert denied.status_code == 401
