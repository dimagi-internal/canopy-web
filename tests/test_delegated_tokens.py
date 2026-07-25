import pytest
from django.contrib.auth.models import User
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
