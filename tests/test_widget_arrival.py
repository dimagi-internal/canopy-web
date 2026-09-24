"""Widget arrival (who-is-asking §2): an existing canopy account arrives as itself.

A connected site signs an assertion about its visitor. canopy resolves: an
already-linked contact → that user; else a site-signed VERIFIED email at one of
the site's `resolvable_domains`, held verified by exactly one existing canopy
user → link and arrive as that user; else a contact. Never a new account (D1).
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings

from apps.contacts.models import Contact
from apps.tokens import assertions
from apps.tokens.models import DelegatedToken
from apps.workspaces.models import WorkspaceMembership
from tests.test_contact_websocket import _world

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner, ws, app, priv = _world()
    alice = User.objects.create_user("alice", "alice@dimagi.com", "pw")
    EmailAddress.objects.create(user=alice, email=alice.email, verified=True, primary=True)
    M.objects.create(user=alice, workspace=ws, role=M.EDITOR)
    return {"owner": owner, "ws": ws, "app": app, "priv": priv, "alice": alice}


def _arrive(priv, sub="u-42", **claims):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
                            "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()), **claims},
                           priv, algorithm="EdDSA")
    r = Client().post("/api/auth/contact-token", data={"assertion": assertion},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _allow(app, *domains):
    """This tenant's own site, so this tenant's own grant."""
    app.resolvable_domains = list(domains)
    app.save(update_fields=["resolvable_domains"])


def test_without_resolvable_domains_everyone_is_a_contact(w):
    got = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)
    assert got["kind"] == "contact"


def test_a_verified_email_at_a_resolvable_domain_arrives_as_the_user(w):
    _allow(w["app"], "dimagi.com")
    got = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)
    assert got["kind"] == "user"
    tok = DelegatedToken.lookup(got["token"])
    assert tok.user == w["alice"] and tok.assurance == "host_signed"
    assert Contact.objects.get(pk=got["contact_id"]).user == w["alice"]     # linked


def test_a_linked_contact_arrives_as_the_user_without_repeating_the_email(w):
    _allow(w["app"], "dimagi.com")
    _arrive(w["priv"], sub="u-7", email="alice@dimagi.com", email_verified=True)
    got = _arrive(w["priv"], sub="u-7")                                     # no email this time
    assert got["kind"] == "user"


@pytest.mark.parametrize("claims", [
    {"email": "alice@dimagi.com"},                                   # not verified
    {"email": "alice@dimagi.com", "email_verified": "true"},         # a string is not True
    {"email": "alice@dimagi.com", "email_verified": False},
    {"email": "alice@other.org", "email_verified": True},            # not a resolvable domain
])
def test_anything_less_is_a_contact(w, claims):
    _allow(w["app"], "dimagi.com")
    assert _arrive(w["priv"], **claims)["kind"] == "contact"


def test_arrival_never_creates_an_account(w):
    _allow(w["app"], "dimagi.com")
    before = User.objects.count()
    got = _arrive(w["priv"], email="newcomer@dimagi.com", email_verified=True)
    assert got["kind"] == "contact" and User.objects.count() == before


def test_an_unverified_canopy_address_proves_nothing(w):
    _allow(w["app"], "dimagi.com")
    EmailAddress.objects.filter(user=w["alice"]).update(verified=False)
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_a_canopy_user_outside_the_sites_workspace_stays_a_contact(w):
    _allow(w["app"], "dimagi.com")
    M.objects.filter(user=w["alice"]).delete()
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_the_user_token_reaches_the_user_surface_not_the_contact_one(w):
    _allow(w["app"], "dimagi.com")
    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.get("/api/embed/agents").status_code == 200
    assert c.get("/api/contact/me").status_code in (401, 403)


def test_a_turn_a_resolved_user_starts_says_host_signed(w):
    from apps.harness.models import Turn

    _allow(w["app"], "dimagi.com")
    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    sid = c.post("/api/canopy-sessions/", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    r = c.post(f"/api/canopy-sessions/{sid}/send", {"text": "hi", "client_id": "c1"},
               content_type="application/json")
    assert r.status_code in (200, 201), r.content
    t = Turn.objects.filter(chat_session_id=sid).latest("created_at")
    assert (t.initiator_user_id, t.initiator_assurance) == (w["alice"].pk, "host_signed")


# --- the setting's bounds ------------------------------------------------------------

def _patch(user, app, domains):
    c = Client()
    c.force_login(user)
    return c.patch(f"/api/workspaces/{app.workspace_id}/connected-apps/{app.pk}",
                   {"resolvable_domains": domains}, content_type="application/json")


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com,dimagi-associate.com")
def test_an_owner_may_allow_their_own_admitted_domain(w):
    r = _patch(w["owner"], w["app"], ["dimagi.com"])
    assert r.status_code == 200, r.content
    assert r.json()["resolvable_domains"] == ["dimagi.com"]


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com,dimagi-associate.com")
def test_not_someone_elses_domain_even_if_admitted(w):
    assert _patch(w["owner"], w["app"], ["dimagi-associate.com"]).status_code in (400, 422)


def test_not_a_domain_canopy_does_not_admit(w):
    assert _patch(w["owner"], w["app"], ["gmail.com"]).status_code in (400, 422)


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com,dimagi-associate.com")
def test_removing_a_domain_does_not_require_being_in_it(w):
    _allow(w["app"], "dimagi.com", "dimagi-associate.com")
    r = _patch(w["owner"], w["app"], ["dimagi.com"])
    assert r.status_code == 200 and r.json()["resolvable_domains"] == ["dimagi.com"]
