"""Host grant contract v1: canopy's client identity, and redeeming a host's ID-JAG.

Pinned to `docs/architecture/host-grant-contract.md` — a connect-labs agent builds
the host half against the same file, so every wire detail here is a promise.

Most of this file is refusals. A redemption is canopy spending a credential the
host just issued, so every check it makes before spending (sub, client_id,
resource, typ, lifetime, signature, issuer) is exercised on its failure path —
and every failure must leave the ARRIVAL working, because a chat that will not
open is worse than an agent that cannot reach the host.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from unittest import mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret
from apps.events.models import Event
from apps.tokens import assertions, client_identity, host_grants, outbound
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

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
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _assertion(priv, sub="u-42"):
    now = _now()
    return jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
                       "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()),
                       "name": "Gillian"}, priv, algorithm="EdDSA")


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


def _arrive(site, *, id_jag=None, sub="u-42"):
    body = {"assertion": _assertion(site["priv"], sub=sub), "agent_slug": "ace"}
    if id_jag is not None:
        body["id_jag"] = id_jag
    return Client().post("/api/auth/contact-token", data=body, content_type="application/json")


# --- canopy's client identity --------------------------------------------------


@override_settings(REQUIRE_AUTH=True)
def test_the_client_metadata_document_is_public_and_is_the_contract_shape():
    r = Client().get("/oauth/client.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc == {
        "client_id": client_identity.client_id(),
        "client_name": "canopy",
        "jwks_uri": client_identity.jwks_uri(),
        "token_endpoint_auth_method": "private_key_jwt",
        "grant_types": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
        "dpop_bound_access_tokens": True,
    }
    assert doc["client_id"].endswith("/oauth/client.json")


@override_settings(REQUIRE_AUTH=True)
def test_the_jwks_is_public_signing_keys_only_and_never_the_dpop_key():
    keys = Client().get("/oauth/jwks.json").json()["keys"]
    assert len(keys) == 1
    [k] = keys
    assert k["use"] == "sig" and k["alg"] in ("EdDSA", "ES256") and k["kid"]
    assert "d" not in k
    assert k["kid"] != client_identity.dpop_jkt(), "the DPoP key is not canopy's client key"


@override_settings(CANOPY_OAUTH_EPHEMERAL_KEYS=False, CANOPY_OAUTH_CLIENT_KEY="PLACEHOLDER",
                   CANOPY_OAUTH_DPOP_KEY="")
def test_an_unconfigured_canopy_says_so_rather_than_publishing_nothing():
    assert Client().get("/oauth/client.json").status_code == 503
    assert Client().get("/oauth/jwks.json").status_code == 503
    assert not client_identity.configured()


def test_a_symmetric_or_rsa_key_is_refused():
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    with override_settings(CANOPY_OAUTH_CLIENT_KEY=pem):
        with pytest.raises(client_identity.ClientIdentityError):
            client_identity.client_assertion(ISSUER)


def test_a_dpop_proof_is_the_rfc_9449_shape_and_binds_the_token():
    proof = client_identity.dpop_proof("post", "https://host.test/mcp/", access_token="tok")
    header = jwt.get_unverified_header(proof)
    assert header["typ"] == "dpop+jwt"
    assert "d" not in header["jwk"], "a proof must never carry the private key"
    pub = jwt.PyJWK.from_dict(header["jwk"]).key
    claims = jwt.decode(proof, pub, algorithms=["EdDSA", "ES256"])
    assert claims["htm"] == "POST" and claims["htu"] == "https://host.test/mcp/"
    assert claims["jti"] and abs(claims["iat"] - _now()) < 5
    want = base64.urlsafe_b64encode(hashlib.sha256(b"tok").digest()).decode().rstrip("=")
    assert claims["ath"] == want
    assert client_identity.thumbprint(header["jwk"]) == client_identity.dpop_jkt()


# --- arrival: redeeming the host's grant ---------------------------------------


def test_an_id_jag_is_redeemed_per_the_contract_and_stored_encrypted(site, host):
    r = _arrive(site, id_jag=_id_jag(site["priv"]))
    assert r.status_code == 200, r.content
    assert r.json()["host_grant"] is True

    [post] = host.posts
    assert post["url"] == TOKEN_ENDPOINT
    form = post["data"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["client_id"] == client_identity.client_id()
    assert form["client_assertion_type"] == \
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert form["resource"] == RESOURCE
    # private_key_jwt: verifies against canopy's PUBLISHED key, aud = the issuer.
    [jwk] = client_identity.published_jwks()["keys"]
    ca = jwt.decode(form["client_assertion"], jwt.PyJWK.from_dict(jwk).key,
                    algorithms=["EdDSA"], audience=ISSUER)
    assert ca["iss"] == ca["sub"] == client_identity.client_id()
    assert ca["exp"] - ca["iat"] <= 60 and ca["jti"]
    # DPoP on the token request: htm/htu, no ath (there is no token yet).
    proof = jwt.decode(post["headers"]["DPoP"], options={"verify_signature": False})
    assert proof["htm"] == "POST" and proof["htu"] == TOKEN_ENDPOINT and "ath" not in proof

    grant = HostGrant.objects.get()
    assert grant.subject == "u-42" and grant.contact_id == r.json()["contact_id"]
    assert grant.scope == "marketplace:read" and grant.resource == RESOURCE
    assert "host-at-SECRET" not in grant.access_token_enc
    assert decrypt_secret(grant.access_token_enc) == "host-at-SECRET"
    assert grant.dpop_jkt == client_identity.dpop_jkt()
    assert grant.expires_at <= timezone.now() + dt.timedelta(seconds=900)
    # The widget re-mints on this cadence, which is what keeps the grant fresh.
    expires = dt.datetime.fromisoformat(r.json()["expires_at"])
    assert expires <= timezone.now() + dt.timedelta(minutes=5, seconds=5)
    assert Event.objects.filter(kind="host_grant.redeemed").exists()


def test_no_id_jag_means_no_grant_and_the_arrival_is_unchanged(site, host):
    r = _arrive(site)
    assert r.status_code == 200 and r.json()["host_grant"] is False
    assert not HostGrant.objects.exists() and not host.posts
    expires = dt.datetime.fromisoformat(r.json()["expires_at"])
    assert expires > timezone.now() + dt.timedelta(minutes=25)


def test_a_refused_redemption_still_opens_the_chat_and_is_recorded(site, host):
    host.status, host.body = 400, {"error": "invalid_grant",
                                   "error_description": "leaky prose host-at-SECRET"}
    r = _arrive(site, id_jag=_id_jag(site["priv"]))
    assert r.status_code == 200 and r.json()["host_grant"] is False
    assert r.json()["token"]
    assert not HostGrant.objects.exists()
    ev = Event.objects.get(kind="host_grant.refused")
    assert ev.payload["code"] == "redeem_refused" and ev.level == "warn"
    assert "SECRET" not in str(ev.payload), "the host's prose is never recorded"


@pytest.mark.parametrize("change,code", [
    ({"sub": "someone-else"}, "wrong_subject"),
    ({"client_id": "https://evil.test/oauth/client.json"}, "wrong_client"),
    ({"resource": "https://host.test/other-mcp/"}, "wrong_resource"),
    ({"aud": "https://elsewhere.test"}, "wrong_audience"),
    ({"iss": "https://elsewhere.test"}, "wrong_issuer"),
    ({"typ": "JWT"}, "wrong_type"),
    ({"lifetime": 3600}, "too_long"),
    ({"lifetime": -120}, "expired"),
    ({"other_key": True}, "bad_signature"),
])
def test_a_bad_id_jag_is_refused_before_canopy_spends_it(site, host, change, code):
    change = dict(change)
    kw = {}
    if "typ" in change:
        kw["typ"] = change.pop("typ")
    if "lifetime" in change:
        kw["lifetime"] = change.pop("lifetime")
    if change.pop("other_key", False):
        kw["key"] = _keypair()[0]
    r = _arrive(site, id_jag=_id_jag(site["priv"], **kw, **change))
    assert r.status_code == 200 and r.json()["host_grant"] is False
    assert not host.posts, "nothing is sent to the host for a grant canopy already refused"
    assert Event.objects.get(kind="host_grant.refused").payload["code"] == code


def test_metadata_naming_another_issuer_is_refused(site, host):
    host.issuer = "https://evil.test"
    r = _arrive(site, id_jag=_id_jag(site["priv"]))
    assert r.json()["host_grant"] is False and not host.posts
    assert Event.objects.get(kind="host_grant.refused").payload["code"] == "issuer_mismatch"


def test_a_bearer_token_is_refused_rather_than_stored(site, host):
    host.body = {"access_token": "x", "token_type": "Bearer", "expires_in": 900}
    assert _arrive(site, id_jag=_id_jag(site["priv"])).json()["host_grant"] is False
    assert not HostGrant.objects.exists()


def test_a_host_claiming_a_long_life_is_clamped_to_fifteen_minutes(site, host):
    host.body = {"access_token": "x", "token_type": "DPoP", "expires_in": 86400}
    _arrive(site, id_jag=_id_jag(site["priv"]))
    assert HostGrant.objects.get().expires_at <= timezone.now() + dt.timedelta(seconds=901)


def test_a_dpop_nonce_challenge_is_answered_once(site, host):
    host.nonce_first = True
    assert _arrive(site, id_jag=_id_jag(site["priv"])).json()["host_grant"] is True
    assert len(host.posts) == 2
    second = jwt.decode(host.posts[1]["headers"]["DPoP"], options={"verify_signature": False})
    assert second["nonce"] == "n-1"
    # A fresh client assertion too: both are single use.
    assert host.posts[0]["data"]["client_assertion"] != host.posts[1]["data"]["client_assertion"]


def test_a_site_without_host_config_ignores_the_id_jag(site, host):
    app = site["app"]
    app.host_issuer = ""
    app.save()
    r = _arrive(site, id_jag=_id_jag(site["priv"]))
    assert r.status_code == 200 and r.json()["host_grant"] is False and not host.posts
    assert Event.objects.get(kind="host_grant.refused").payload["code"] == "not_configured"


# --- outbound: every fetch is SSRF-guarded --------------------------------------


@pytest.mark.parametrize("url,addr", [
    ("http://host.test/", "93.184.216.34"),
    ("https://host.test/", "169.254.169.254"),
    ("https://host.test/", "10.0.0.5"),
    ("https://user:pw@host.test/", "93.184.216.34"),
])
def test_outbound_refuses_plaintext_private_and_credentialed_urls(url, addr):
    with mock.patch("apps.tokens.jwks.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", (addr, 443))]):
        with pytest.raises(outbound.OutboundError):
            outbound.check_url(url)


def test_outbound_never_follows_a_redirect():
    resp = mock.Mock(status_code=302, headers={"Location": "https://10.0.0.5/"})
    with mock.patch("apps.tokens.jwks.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
            mock.patch("apps.tokens.outbound.requests.post", return_value=resp) as post:
        with pytest.raises(outbound.OutboundError, match="redirected"):
            outbound.post_form("https://host.test/o/token/", {}, headers={})
    assert post.call_args.kwargs["allow_redirects"] is False


def test_the_metadata_url_follows_rfc_8414_path_insertion():
    assert host_grants.metadata_url("https://host.test") == \
        "https://host.test/.well-known/oauth-authorization-server"
    assert host_grants.metadata_url("https://host.test/tenant/") == \
        "https://host.test/.well-known/oauth-authorization-server/tenant"


# --- site config -------------------------------------------------------------------


def test_host_config_is_editable_on_the_connected_site_and_checked(site, monkeypatch):
    monkeypatch.setattr(outbound, "refuse_private", lambda host: None)
    c = Client()
    c.force_login(site["owner"])
    url = f"/api/workspaces/w1/connected-apps/{site['app'].pk}"
    r = c.patch(url, data={"host_issuer": "https://labs.test", "host_mcp_resource":
                           "https://labs.test/mcp/"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["issues_host_grants"] is True
    assert r.json()["host_mcp_resource"] == "https://labs.test/mcp/"
    bad = c.patch(url, data={"host_issuer": "http://labs.test"}, content_type="application/json")
    assert bad.status_code == 422


def test_an_oversized_id_jag_is_refused_unread(site, host):
    r = _arrive(site, id_jag="x" * (host_grants.MAX_ID_JAG_BYTES + 1))
    assert r.status_code == 200 and r.json()["host_grant"] is False and not host.posts
    assert Event.objects.get(kind="host_grant.refused").payload["code"] == "too_large"
