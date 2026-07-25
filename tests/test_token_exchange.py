import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.tokens.models import AppCredential, DelegatedToken

pytestmark = pytest.mark.django_db

URL = "/api/auth/token-exchange"


@pytest.fixture()
def cred():
    admin = User.objects.create_user("admin", "admin@dimagi.com", "pw")
    raw, cred = AppCredential.create_credential(
        name="ace-web", domains=["dimagi.com"], created_by=admin)
    return raw, cred


def _post(raw_cred, email, **extra):
    return Client().post(
        URL, data={"acting_as_email": email, **extra},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw_cred}")


def test_exchange_jit_provisions_and_mints(cred):
    raw, _ = cred
    resp = _post(raw, "newperson@dimagi.com")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    tok = DelegatedToken.lookup(body["token"])
    assert tok is not None and tok.user.email == "newperson@dimagi.com"
    # the minted token authenticates a normal API call
    ok = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {body['token']}")
    assert ok.status_code == 200


def test_exchange_rejects_foreign_domain(cred):
    raw, _ = cred
    assert _post(raw, "evil@attacker.org").status_code == 403


def test_exchange_rejects_bad_credential():
    assert _post("not-a-credential", "a@dimagi.com").status_code == 401


def test_exchange_rejects_personal_token(cred):
    from apps.tokens.models import PersonalToken
    user = User.objects.create_user("u2", "u2@dimagi.com", "pw")
    raw_pat, _ = PersonalToken.create_for_user(user=user, label="x")
    assert _post(raw_pat, "u2@dimagi.com").status_code == 401


def test_ttl_clamped(cred):
    raw, _ = cred
    resp = _post(raw, "p@dimagi.com", ttl_seconds=99999999)
    tok = DelegatedToken.objects.latest("created_at")
    delta = (tok.expires_at - tok.created_at).total_seconds()
    assert delta <= 86400 + 5
