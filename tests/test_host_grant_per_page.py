"""A conversation reaches its site through the grant of the page it is on.

Each of a visitor's pages arrives with a grant of its own (and re-arrives every few
minutes), and a host may hold a page's grant to what that page shows: connect-labs
holds a data space's agent to the space and the Data page's agent to its selection.
One grant row per visitor let the latest arrival from ANY of their pages speak for
every conversation: with a space open in one tab, the Data page's agent read the
space. So each arrival is its own row, the token minted with it points at it, and a
conversation remembers the grant of the page that last acted in it.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.embedding import assertions
from apps.host_grants import client_identity, outbound
from apps.host_grants.host_grants import grant_for
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# The arrival, as tests/test_host_grants.py drives it (a test module is never imported).
ISSUER = "https://host.test"
RESOURCE = "https://host.test/mcp/"
TOKEN_ENDPOINT = "https://host.test/o/token/"


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, pub


@pytest.fixture()
def site():
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    priv, pub = _keypair()
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.public_keys = [pub]
    app.host_issuer = ISSUER
    app.host_mcp_resource = RESOURCE
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return {"app": app, "priv": priv, "ws": ws, "agent": agent, "owner": owner}


def _now():
    return int(dt.datetime.now(dt.UTC).timestamp())


def _assertion(priv, sub="u-42", **extra):
    now = _now()
    return jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
                       "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()),
                       "name": "Gillian", **extra}, priv, algorithm="EdDSA")


def _id_jag(priv, *, typ="oauth-id-jag+jwt", lifetime=120, key=None, **over):
    now = _now()
    claims = {"iss": ISSUER, "aud": ISSUER, "sub": "u-42",
              "client_id": client_identity.client_id(), "resource": RESOURCE,
              "scope": "marketplace:read", "iat": now, "exp": now + lifetime,
              "jti": str(uuid.uuid4())}
    claims.update(over)
    return jwt.encode(claims, key or priv, algorithm="EdDSA", headers={"typ": typ})


class FakeHost:
    """The host's metadata and token endpoint, recording what canopy sent."""

    def __init__(self, *, status=200, body=None, issuer=ISSUER, nonce_first=False):
        self.status, self.issuer, self.nonce_first = status, issuer, nonce_first
        self.body = body if body is not None else {
            "access_token": "host-at-SECRET", "token_type": "DPoP",
            "expires_in": 900, "scope": "marketplace:read"}
        self.posts: list[dict] = []

    def get_json(self, url, *, what="URL"):
        assert url == f"{ISSUER}/.well-known/oauth-authorization-server"
        return {"issuer": self.issuer, "token_endpoint": TOKEN_ENDPOINT}

    def post_form(self, url, data, *, headers, what="URL"):
        self.posts.append({"url": url, "data": dict(data), "headers": dict(headers)})
        if self.nonce_first and len(self.posts) == 1:
            return 400, {"error": "use_dpop_nonce"}, {"DPoP-Nonce": "n-1"}
        return self.status, self.body, {}


@pytest.fixture()
def host(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(outbound, "get_json", fake.get_json)
    monkeypatch.setattr(outbound, "check_url", lambda url, what="URL": url)
    monkeypatch.setattr(outbound, "post_form", fake.post_form)
    return fake


def _arrive(site, *, id_jag=None, sub="u-42", **extra):
    body = {"assertion": _assertion(site["priv"], sub=sub, **extra), "agent_slug": "ace"}
    if id_jag is not None:
        body["id_jag"] = id_jag
    return Client().post("/api/auth/contact-token", data=body, content_type="application/json")


def _page(site):
    """One page's arrival: its token, and the grant redeemed with it."""
    r = _arrive(site, id_jag=_id_jag(site["priv"]))
    assert r.status_code == 200, r.content
    token = r.json()["token"]
    row = ContactToken.lookup(token)
    assert row.host_grant_id is not None
    return token, row.host_grant


def _as(token):
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def _start(token) -> Session:
    r = _as(token).post("/api/contact/sessions", data={"agent_slug": "ace"},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(pk=r.json()["id"])


def _declare(token, session):
    r = _as(token).put(f"/api/contact/sessions/{session.pk}/page-state",
                       data={"state": {"resource": "labs-data://", "path": "/labs/data/"}},
                       content_type="application/json")
    assert r.status_code == 200, r.content


def test_each_arrival_is_its_own_grant_and_its_token_points_at_it(site, host):
    _, a = _page(site)
    _, b = _page(site)
    assert a.pk != b.pk
    assert HostGrant.objects.count() == 2


def test_a_conversation_reads_through_its_own_pages_grant_not_the_latest(site, host):
    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    # Another of the visitor's pages (another tab) arrives afterwards.
    _, grant_b = _page(site)
    contact = grant_a.contact

    assert grant_for(site["app"], agent=site["agent"], contact=contact).pk == grant_b.pk
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], contact=contact,
                     session=conversation).pk == grant_a.pk


def test_the_page_that_acts_in_a_conversation_moves_it_to_its_grant(site, host):
    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    token_b, grant_b = _page(site)
    _declare(token_b, conversation)
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], contact=grant_a.contact,
                     session=conversation).pk == grant_b.pk
    # An older arrival never moves it back (a socket opened before the page re-arrived).
    _declare(token_a, conversation)
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], contact=grant_a.contact,
                     session=conversation).pk == grant_b.pk


def test_once_its_pages_grant_has_expired_a_conversation_reads_through_the_latest(site, host):
    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    _, grant_b = _page(site)
    HostGrant.objects.filter(pk=grant_a.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], contact=grant_a.contact,
                     session=conversation).pk == grant_b.pk


def test_someone_elses_stamp_on_a_conversation_is_not_their_grant(site, host):
    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    # Another visitor's arrival: their grant is never this visitor's, stamped or not.
    r = _arrive(site, id_jag=_id_jag(site["priv"], sub="u-99"), sub="u-99")
    other = ContactToken.lookup(r.json()["token"]).host_grant
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], contact=other.contact,
                     session=conversation).pk == other.pk


def test_a_visitors_expired_grants_go_at_their_next_arrival(site, host):
    _, old = _page(site)
    HostGrant.objects.filter(pk=old.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    _, fresh = _page(site)
    assert list(HostGrant.objects.values_list("pk", flat=True)) == [fresh.pk]


def test_a_stamped_grant_bound_to_another_agent_is_never_this_agents(site, host):
    from apps.canopy_sessions.services import note_host_grant

    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    other = Agent.objects.create(slug="labsdata", name="Labs data", workspace=site["ws"],
                                 owner=site["owner"])
    HostGrant.objects.filter(pk=grant_a.pk).update(agent=other)
    note_host_grant(conversation, grant_a.pk, contact=grant_a.contact)
    assert grant_for(site["app"], agent=site["agent"], contact=grant_a.contact,
                     session=conversation) is None
