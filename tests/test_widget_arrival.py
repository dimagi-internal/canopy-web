"""Widget arrival (who-is-asking §2): an existing canopy account arrives as itself.

A connected site signs an assertion about its visitor. canopy resolves: an
already-linked contact → that user; else a site-signed VERIFIED email held
verified by exactly one existing canopy user who is a member of the site's
workspace → link and arrive as that user; else a contact. Never a new account (D1).
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

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


def test_a_member_with_a_verified_email_arrives_as_the_user_with_no_setting(w):
    """No per-site opt-in: being allowed in the workspace is the whole gate."""
    got = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)
    assert got["kind"] == "user"
    tok = DelegatedToken.lookup(got["token"])
    assert tok.user == w["alice"] and tok.assurance == "host_signed"
    assert Contact.objects.get(pk=got["contact_id"]).user == w["alice"]     # linked


def test_a_linked_contact_arrives_as_the_user_without_repeating_the_email(w):
    _arrive(w["priv"], sub="u-7", email="alice@dimagi.com", email_verified=True)
    got = _arrive(w["priv"], sub="u-7")                                     # no email this time
    assert got["kind"] == "user"


@pytest.mark.parametrize("claims", [
    {"email": "alice@dimagi.com"},                                   # not verified
    {"email": "alice@dimagi.com", "email_verified": "true"},         # a string is not True
    {"email": "alice@dimagi.com", "email_verified": False},
    {"email": "alice@other.org", "email_verified": True},            # nobody holds it
])
def test_anything_less_is_a_contact(w, claims):
    assert _arrive(w["priv"], **claims)["kind"] == "contact"


def test_arrival_never_creates_an_account(w):
    before = User.objects.count()
    got = _arrive(w["priv"], email="newcomer@dimagi.com", email_verified=True)
    assert got["kind"] == "contact" and User.objects.count() == before


def test_an_unverified_canopy_address_proves_nothing(w):
    EmailAddress.objects.filter(user=w["alice"]).update(verified=False)
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_a_canopy_user_outside_the_sites_workspace_stays_a_contact(w):
    M.objects.filter(user=w["alice"]).delete()
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_the_user_token_reaches_the_user_surface_not_the_contact_one(w):
    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.get("/api/embed/agents").status_code == 200
    assert c.get("/api/contact/me").status_code in (401, 403)


def test_a_turn_a_resolved_user_starts_says_host_signed(w):
    from apps.harness.models import Turn

    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    sid = c.post("/api/canopy-sessions/", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    r = c.post(f"/api/canopy-sessions/{sid}/send", {"text": "hi", "client_id": "c1"},
               content_type="application/json")
    assert r.status_code in (200, 201), r.content
    t = Turn.objects.filter(chat_session_id=sid).latest("created_at")
    assert (t.initiator_user_id, t.initiator_assurance) == (w["alice"].pk, "host_signed")
