"""Connected sites → "Test connection" (`POST /api/workspaces/{slug}/connected-apps/{id}/test`).

The SDK's conformance checks, run from canopy's server against a site's own
settings. Against: canopy-web itself (in-process), a host built only from
`canopy_sdk.host`, and hosts broken in the ways that actually happen.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from canopy_sdk import contract
from canopy_sdk.host import (
    ClientKeyResolver, GrantHandler, GrantRefused, HostConfig, authorization_server_metadata,
    protected_resource_metadata,
)
from canopy_sdk.keys import generate_private_key, public_pem
from canopy_sdk.stores import MemoryCache, MemoryJtiStore, MemoryTokenStore
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings

from apps.tokens import client_identity, outbound, self_host
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "https://canopy.test"
ISSUER = "https://host.test"
RESOURCE = "https://host.test/mcp/"
TOKEN_ENDPOINT = "https://host.test/o/token/"


@pytest.fixture(autouse=True)
def _base():
    cache.clear()
    with override_settings(CANOPY_PUBLIC_BASE_URL=BASE):
        yield
    cache.clear()


@pytest.fixture()
def tenant():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    stranger = User.objects.create_user("st", "st@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    return {"owner": owner, "editor": editor, "stranger": stranger, "ws": ws}


def _site(tenant, name, **fields):
    app = AppCredential.create_credential(name=name, created_by=tenant["owner"], workspace=tenant["ws"])
    for k, v in fields.items():
        setattr(app, k, v)
    app.save()
    return app


def _test(user, app):
    c = Client()
    c.force_login(user)
    return c.post(f"/api/workspaces/connect/connected-apps/{app.pk}/test")


def _by_name(body):
    return {c["name"]: c for c in body["checks"]}


class Host:
    """A host built only from the SDK, reachable only through canopy's outbound door."""

    def __init__(self, *, accept_client=True):
        self.key = generate_private_key("EdDSA")
        self.config = HostConfig(
            signing_key=self.key, issuer=ISSUER, resource=RESOURCE, token_endpoint=TOKEN_ENDPOINT,
            canopy_client_id=client_identity.client_id() if accept_client else "https://other/client.json",
            scope_tools={"marketplace:read": ["marketplace_orgs_get"]})
        self.tokens = MemoryTokenStore()
        self.handler = GrantHandler(self.config, jti_store=MemoryJtiStore(), token_store=self.tokens,
                                    client_keys=ClientKeyResolver(fetch_json=self._canopy, cache=MemoryCache()))
        self.documents = {
            contract.metadata_url(ISSUER): authorization_server_metadata(self.config),
            contract.protected_resource_metadata_url(RESOURCE): protected_resource_metadata(self.config),
            f"{ISSUER}/jwks.json": self.config.jwks(),
        }
        self.requests: list[str] = []

    def _canopy(self, url):
        return Client().get(urlsplit(url).path).json()

    def get_json(self, url, *, what="URL"):
        self.requests.append(url)
        if url not in self.documents:
            raise outbound.OutboundError(f"the {what} answered 404")
        return self.documents[url]

    def post_form(self, url, data, *, headers, what="URL"):
        self.requests.append(url)
        assert url == TOKEN_ENDPOINT
        try:
            result = self.handler.handle(dict(data), headers.get(contract.DPOP_HEADER))
        except GrantRefused as refused:
            return refused.status, refused.body(), refused.headers()
        return 200, result.body(), result.headers()


def _wire(monkeypatch, host):
    monkeypatch.setattr(outbound, "get_json", host.get_json)
    monkeypatch.setattr(outbound, "post_form", host.post_form)


# --- passing ------------------------------------------------------------------------------


def test_canopy_web_itself_passes_every_check_in_process(tenant):
    app = _site(tenant, "canopy-web", jwks_url=self_host.jwks_url(), host_issuer=self_host.issuer(),
                host_mcp_resource=self_host.resource())
    r = _test(tenant["owner"], app)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["ok"] is True, body
    checks = _by_name(body)
    assert {"in_process", "jwks_reachable", "key[0]_kid_is_thumbprint", "as_metadata_issuer",
            "prm_authorization_server", "client_accepted", "client_refuses_foreign_grant"} <= set(checks)
    assert all(c["status"] == "pass" for c in body["checks"]), body
    assert checks["client_accepted"]["label"] == "Accepts canopy as its client"
    # The probe spent nothing: no token issued by canopy-as-host.
    from canopy_sdk.django.models import DelegatedToken

    assert not DelegatedToken.objects.exists()


def test_an_sdk_host_passes_and_nothing_is_issued(tenant, monkeypatch):
    host = Host()
    _wire(monkeypatch, host)
    app = _site(tenant, "connect-labs", jwks_url=f"{ISSUER}/jwks.json", host_issuer=ISSUER,
                host_mcp_resource=RESOURCE)
    body = _test(tenant["owner"], app).json()
    assert body["ok"] is True, body
    assert TOKEN_ENDPOINT in host.requests, "canopy really asked the host's token endpoint"
    assert not host.tokens._tokens


# --- failing, with the reason --------------------------------------------------------------


def test_a_host_that_does_not_accept_canopy_says_so(tenant, monkeypatch):
    _wire(monkeypatch, Host(accept_client=False))
    app = _site(tenant, "connect-labs", jwks_url=f"{ISSUER}/jwks.json", host_issuer=ISSUER,
                host_mcp_resource=RESOURCE)
    body = _test(tenant["owner"], app).json()
    assert body["ok"] is False
    accepted = _by_name(body)["client_accepted"]
    assert accepted["status"] == "fail" and "invalid_client" in accepted["detail"]


def test_metadata_naming_another_issuer_fails_that_check(tenant, monkeypatch):
    host = Host()
    host.documents[contract.metadata_url(ISSUER)] = {**host.documents[contract.metadata_url(ISSUER)],
                                                     "issuer": "https://evil.test"}
    _wire(monkeypatch, host)
    app = _site(tenant, "connect-labs", jwks_url=f"{ISSUER}/jwks.json", host_issuer=ISSUER,
                host_mcp_resource=RESOURCE)
    checks = _by_name(_test(tenant["owner"], app).json())
    assert checks["as_metadata_issuer"]["status"] == "fail"
    assert checks["client_token_endpoint"]["status"] == "fail"


def test_a_site_with_no_keys_and_no_grants(tenant):
    app = _site(tenant, "bare")
    body = _test(tenant["owner"], app).json()
    checks = _by_name(body)
    assert checks["signing_keys_registered"]["status"] == "fail"
    assert checks["host_grants"]["status"] == "skip"
    assert body["ok"] is False


def test_half_configured_grants_are_a_failure(tenant):
    app = _site(tenant, "half", public_keys=[public_pem(generate_private_key())], host_issuer=ISSUER)
    checks = _by_name(_test(tenant["owner"], app).json())
    assert checks["pasted_key[0]_type"]["status"] == "pass"
    assert checks["host_grants"]["status"] == "fail"


def test_an_rsa_key_is_fine_for_assertions_but_not_for_grants(tenant, monkeypatch):
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    plain = _site(tenant, "plain", public_keys=[rsa_pem])
    assert _by_name(_test(tenant["owner"], plain).json())["pasted_key[0]_type"]["status"] == "pass"

    _wire(monkeypatch, Host())
    granting = _site(tenant, "granting", public_keys=[rsa_pem], host_issuer=ISSUER, host_mcp_resource=RESOURCE)
    row = _by_name(_test(tenant["owner"], granting).json())["pasted_key[0]_type"]
    assert row["status"] == "fail" and "ID-JAG" in row["detail"]


def test_a_private_address_is_never_fetched(tenant, monkeypatch):
    import requests

    def boom(*a, **k):  # pragma: no cover - the point is that it is not reached
        raise AssertionError("canopy made a request to a private address")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    app = _site(tenant, "inside", jwks_url="https://10.0.0.7/jwks.json",
                host_issuer="https://169.254.169.254", host_mcp_resource="https://169.254.169.254/mcp/")
    checks = _by_name(_test(tenant["owner"], app).json())
    assert checks["jwks_reachable"]["status"] == "fail"
    assert checks["as_metadata_reachable"]["status"] == "fail"
    assert "private" in checks["jwks_reachable"]["detail"].lower() or \
        "address" in checks["jwks_reachable"]["detail"].lower()


# --- who may run it -------------------------------------------------------------------------


def test_only_an_owner_may_test(tenant):
    app = _site(tenant, "canopy-web")
    assert _test(tenant["editor"], app).status_code == 403
    assert _test(tenant["stranger"], app).status_code == 404
    assert Client().post(f"/api/workspaces/connect/connected-apps/{app.pk}/test").status_code in (401, 403)


def test_another_workspaces_site_is_not_found(tenant):
    other_owner = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=other, role=WorkspaceMembership.OWNER)
    theirs = AppCredential.create_credential(name="theirs", created_by=other_owner, workspace=other)
    assert _test(tenant["owner"], theirs).status_code == 404
