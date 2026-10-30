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
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures import passkey_confirm

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
    # The visitor: a member the site vouches for by verified email, so they arrive as
    # themselves — only a canopy user holds an act-as-you permission.
    visitor = _member(ws, "gillian")
    return {"app": app, "priv": priv, "ws": ws, "agent": agent, "owner": owner, "visitor": visitor,
            "passkey": passkey_confirm.give_passkey(visitor)}


def _member(ws, name):
    from allauth.account.models import EmailAddress

    user = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.EDITOR)
    return user


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


def _arrive(site, *, id_jag=None, sub="u-42", email="gillian@dimagi.com", **extra):
    """The site's arrival for its visitor, vouched for by verified email (`email=""`:
    a visitor canopy knows only as a contact)."""
    if email:
        extra = {"email": email, "email_verified": True, **extra}
    body = {"assertion": _assertion(site["priv"], sub=sub, **extra), "agent_slug": "ace"}
    if id_jag is not None:
        body["id_jag"] = id_jag
    return Client().post("/api/auth/contact-token", data=body, content_type="application/json")


def _page(site, *, sub="u-42", email="gillian@dimagi.com", passkey=None, confirm=True):
    """One page's arrival: its token, and the grant redeemed with it — confirmed with
    the visitor's passkey in the panel, unless the page's permission already is (a
    re-mint inherits it) or `confirm=False`."""
    r = _arrive(site, id_jag=_id_jag(site["priv"], sub=sub), sub=sub, email=email)
    assert r.status_code == 200 and r.json()["kind"] == "user", r.content
    token = r.json()["token"]
    row = DelegatedToken.lookup(token)
    assert row.host_grant_id is not None
    grant = row.host_grant
    if confirm and grant.confirmed_at is None:
        done = passkey_confirm.confirm(token, passkey or site["passkey"])
        assert done.status_code == 200, done.content
        grant.refresh_from_db()
    return token, grant


def _as(token):
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def _start(token) -> Session:
    r = _as(token).post("/api/canopy-sessions/", data={"agent_slug": "ace"},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(pk=r.json()["id"])


def _declare(token, session):
    r = _as(token).put(f"/api/canopy-sessions/{session.pk}/page-state",
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
    visitor = site["visitor"]

    assert grant_for(site["app"], agent=site["agent"], user=visitor).pk == grant_b.pk
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], user=visitor,
                     session=conversation).pk == grant_a.pk


def test_the_page_that_acts_in_a_conversation_moves_it_to_its_grant(site, host):
    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    token_b, grant_b = _page(site)
    _declare(token_b, conversation)
    conversation.refresh_from_db()
    visitor = site["visitor"]
    assert grant_for(site["app"], agent=site["agent"], user=visitor,
                     session=conversation).pk == grant_b.pk
    # An older arrival never moves it back (a socket opened before the page re-arrived).
    _declare(token_a, conversation)
    conversation.refresh_from_db()
    assert grant_for(site["app"], agent=site["agent"], user=visitor,
                     session=conversation).pk == grant_b.pk


def test_once_its_pages_grant_has_expired_a_conversation_falls_back_to_nothing(site, host):
    # No other page's grant stands in — not even a live one minted later: the
    # agent tells the person the page's permission ran out.
    from apps.host_grants.host_grants import EXPIRED, lookup

    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    _page(site)
    HostGrant.objects.filter(pk=grant_a.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    conversation.refresh_from_db()
    found = lookup(site["app"], agent=site["agent"], user=site["visitor"], session=conversation)
    assert found.grant is None and found.reason == EXPIRED


def test_a_pruned_page_grant_is_expired_not_replaced(site, host):
    from apps.host_grants.host_grants import EXPIRED, lookup

    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    HostGrant.objects.filter(pk=grant_a.pk).delete()
    _page(site)
    conversation.refresh_from_db()
    assert lookup(site["app"], agent=site["agent"], user=site["visitor"],
                  session=conversation).reason == EXPIRED


def test_a_conversation_no_page_has_acted_in_has_no_grant(site, host):
    from apps.canopy_sessions.services import HOST_GRANTS_KEY
    from apps.host_grants.host_grants import NONE, lookup

    token_a, _ = _page(site)
    conversation = _start(token_a)
    Session.objects.filter(pk=conversation.pk).update(metadata={})
    conversation.refresh_from_db()
    assert HOST_GRANTS_KEY not in conversation.metadata
    found = lookup(site["app"], agent=site["agent"], user=site["visitor"], session=conversation)
    assert found.grant is None and found.reason == NONE


def test_a_stopped_page_grant_is_revoked_and_no_other_stands_in(site, host):
    from apps.host_grants.host_grants import REVOKED, lookup, revoke

    token_a, grant_a = _page(site)
    conversation = _start(token_a)
    _page(site)
    assert revoke(grant_a) == 2, "the page's permission is every live row of it"
    grant_a.refresh_from_db()
    assert grant_a.revoked_at is not None and grant_a.access_token_enc == ""
    conversation.refresh_from_db()
    found = lookup(site["app"], agent=site["agent"], user=site["visitor"], session=conversation)
    assert found.grant is None and found.reason == REVOKED
    assert grant_for(site["app"], agent=site["agent"], user=site["visitor"]) is None


def test_outside_a_conversation_an_expired_bound_grant_never_sorts_ahead_of_a_live_one(site, host):
    _, unbound = _page(site)
    _, bound = _page(site)
    HostGrant.objects.filter(pk=bound.pk).update(agent=site["agent"],
                                                 expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert grant_for(site["app"], agent=site["agent"], user=site["visitor"]).pk == unbound.pk


def test_someone_elses_stamp_on_a_conversation_is_not_their_grant(site, host):
    token_a, _ = _page(site)
    conversation = _start(token_a)
    # Another visitor's arrival: their grant is never this visitor's, stamped or not.
    olga = _member(site["ws"], "olga")
    _, other = _page(site, sub="u-99", email=olga.email, passkey=passkey_confirm.give_passkey(olga))
    conversation.refresh_from_db()
    # Their page never acted in this conversation: nothing, and never the owner's.
    assert grant_for(site["app"], agent=site["agent"], user=olga, session=conversation) is None
    assert grant_for(site["app"], agent=site["agent"], user=olga).pk == other.pk


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
    note_host_grant(conversation, grant_a.pk, user=site["visitor"])
    assert grant_for(site["app"], agent=site["agent"], user=site["visitor"],
                     session=conversation) is None


# --- seeing and stopping one's own act-as-you permissions ------------------------------


def test_a_contact_holds_no_grant_and_has_no_grant_door(site, host):
    """A contact has no passkey, so never holds an act-as-you permission: the site's
    grant is never redeemed for them, and the contact surface has no grant route."""
    r = _arrive(site, id_jag=_id_jag(site["priv"]), email="")
    assert r.status_code == 200 and r.json().get("kind") != "user", r.content
    assert r.json()["host_grant"] is False and not HostGrant.objects.exists()
    token = r.json()["token"]
    assert _as(token).get("/api/contact/host-grant").status_code == 404
    assert _as(token).delete("/api/contact/host-grant").status_code == 404


def test_a_person_lists_and_stops_their_grants_in_settings(site, host):
    person = site["visitor"]
    _page(site)
    _page(site)
    browser = Client()
    browser.force_login(person)
    rows = browser.get("/api/me/host-grants/").json()
    assert len(rows) == 1, "one row per page, however often it re-minted"
    assert rows[0]["site"] == "connect-labs" and rows[0]["scope"] == "marketplace:read"
    assert rows[0]["confirmed"] is True

    # Someone else's grant is not theirs to see or stop.
    stranger = User.objects.create_user("stranger", "s@x.org", "pw")
    other = Client()
    other.force_login(stranger)
    assert other.get("/api/me/host-grants/").json() == []
    assert other.delete(f"/api/me/host-grants/{rows[0]['id']}").status_code == 404

    assert browser.delete(f"/api/me/host-grants/{rows[0]['id']}").status_code == 204
    assert browser.get("/api/me/host-grants/").json() == []
    assert not HostGrant.objects.filter(user=person, revoked_at__isnull=True).exists()
    from apps.events.models import SettingChange

    assert SettingChange.objects.filter(operation__icontains="revoke_my_host_grant").exists()


def test_the_users_panel_sees_and_stops_its_own_pages_grant(site, host):
    token, _ = _page(site)
    r = _as(token).get("/api/embed/host-grant")
    assert r.status_code == 200, r.content
    assert r.json()["grant"]["site"] == "connect-labs" and r.json()["grant"]["confirmed"] is True
    assert _as(token).delete("/api/embed/host-grant").json() == {"grant": None}
    assert _as(token).get("/api/embed/host-grant").json() == {"grant": None}


def test_a_turn_after_the_grant_ran_out_tells_the_person_to_send_again(site, host):
    from apps.harness.models import Turn
    from apps.host_grants import host_gateway

    token, grant = _page(site)
    conversation = _start(token)
    HostGrant.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    _page(site)  # a later page's grant never stands in
    from tests.access_helpers import publish

    publish(site["agent"], {"capabilities": {"connect": {"callers": ["member"],
                                                         "sites": ["connect-labs"]}}})
    turn = Turn.objects.create(chat_session=conversation, prompt="hi", initiator_user=site["visitor"],
                               initiator_kind="user", capability="connect")
    with pytest.raises(host_gateway.GatewayRefusal) as refused:
        host_gateway.resolve(str(turn.pk))
    assert refused.value.code == "expired"
    assert "act as you on this page has ended" in refused.value.message and "ACE" in refused.value.message
