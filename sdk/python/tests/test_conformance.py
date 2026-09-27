"""The conformance checks, run against an in-process host (and against broken ones).

Also exercises the opt-in pytest plugin's fixtures, the way a host's CI would.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from canopy_sdk import conformance, contract
from canopy_sdk.host import (
    DPoPGate, GrantRefused, authorization_server_metadata, delegated_principal, issue_id_jag,
    presented_dpop_jkt, protected_resource_metadata,
)

from .helpers import ISSUER, RESOURCE, TOKEN_ENDPOINT, World, call_asgi

pytest_plugins = ["canopy_sdk.conformance.pytest_plugin"]


def mcp_app(verifier):
    """A minimal Streamable-HTTP JSON-RPC endpoint that lists only the tools the
    delegated principal may use. Behind the gate it sees a plain bearer, and
    authenticates it the way a host's own token verifier would: a delegated
    token resolves only with the key the gate saw proved."""

    async def app(scope, receive, send):
        message = await receive()
        request = json.loads(message.get("body") or b"{}")
        principal = delegated_principal.get()
        if principal is None:
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            principal = verifier.resolve(auth.removeprefix("Bearer "), presented_dpop_jkt.get())
        if principal is None:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if "id" not in request:
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if request["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "host", "version": "1"}}
        else:
            all_tools = [{"name": n} for n in ("marketplace_orgs_get", "marketplace_rounds_list", "admin")]
            result = {"tools": principal.filter_tools(all_tools)}
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"mcp-session-id", b"s-1")]})
        await send({"type": "http.response.body", "body": body})

    return app


class Transport:
    """The host, reachable only through the three transports the checks take."""

    def __init__(self, world: World):
        self.world = world
        self.documents = {
            contract.metadata_url(ISSUER): authorization_server_metadata(world.config),
            contract.protected_resource_metadata_url(RESOURCE): protected_resource_metadata(world.config),
            f"{ISSUER}/jwks.json": world.config.jwks(),
        }
        self.gate = DPoPGate(mcp_app(world.verifier), world.verifier)

    def fetch_json(self, url):
        return self.documents[url]

    def post_form(self, url, data, headers, what=""):
        assert url == TOKEN_ENDPOINT
        try:
            return 200, self.world.handler.handle(data, headers.get("DPoP")).body(), {}
        except GrantRefused as refused:
            return refused.status, refused.body(), {}

    def post_json(self, url, payload, headers=None):
        status, resp_headers, body = call_asgi(self.gate, "POST", urlsplit(url).path, headers=headers,
                                               body=json.dumps(payload).encode())
        return status, body, resp_headers


@pytest.fixture
def world():
    return World()


@pytest.fixture
def transport(world):
    return Transport(world)


def test_a_conforming_host_passes_every_check(world, transport):
    report = conformance.run(
        ISSUER, RESOURCE, jwks_url=f"{ISSUER}/jwks.json",
        id_jag=issue_id_jag(world.config, "42", ["marketplace:read"]), credentials=world.client,
        fetch_json=transport.fetch_json, post_form=transport.post_form, post_json=transport.post_json)
    assert report.ok, str(report)
    names = {c.name for c in report.checks}
    assert {"grant_redeemed", "grant_single_use", "mcp_tools_list", "mcp_refuses_bound_token_as_bearer",
            "mcp_refuses_replayed_proof", "as_metadata_issuer", "prm_authorization_server"} <= names
    tools = next(c for c in report.checks if c.name == "mcp_tools_list")
    assert "2 tools" in tools.detail, "only the scope's tools are visible"


def test_metadata_that_does_not_advertise_the_grant_fails(world, transport):
    url = contract.metadata_url(ISSUER)
    transport.documents[url] = {"issuer": "https://evil", "token_endpoint": "http://plain/token",
                                "dpop_signing_alg_values_supported": ["RS256"]}
    report = conformance.check_metadata(ISSUER, RESOURCE, fetch_json=transport.fetch_json)
    failed = {c.name for c in report.failures()}
    assert {"as_metadata_issuer", "token_endpoint_https", "grant_type_jwt_bearer",
            "auth_method_private_key_jwt", "dpop_algs"} <= failed
    with pytest.raises(AssertionError):
        report.raise_for_failures()


def test_an_unreachable_host_is_a_failure_not_a_crash():
    from canopy_sdk.fetch import FetchError

    def down(url):
        raise FetchError("unreachable")

    report = conformance.check_metadata(ISSUER, RESOURCE, fetch_json=down)
    assert not report.ok and "as_metadata_reachable" in {c.name for c in report.failures()}


def test_a_jwks_with_a_private_or_symmetric_key_fails(transport):
    transport.documents["https://bad/jwks.json"] = {"keys": [{"kty": "oct", "k": "c2VjcmV0"},
                                                             {"kty": "OKP", "crv": "Ed25519", "x": "a",
                                                              "d": "b", "kid": "x"}]}
    report = conformance.check_jwks("https://bad/jwks.json", fetch_json=transport.fetch_json)
    failed = {c.name for c in report.failures()}
    assert {"key[0]_asymmetric", "key[0]_public_only", "key[1]_public_only", "key[1]_kid_is_thumbprint"} <= failed


def test_a_host_that_accepts_a_replayed_grant_fails(world, transport):
    class Forgetful:
        def consume(self, entries):
            pass

        def prune(self):
            pass

    world.handler.jti_store = Forgetful()
    report, token = conformance.check_grant(
        ISSUER, RESOURCE, id_jag=issue_id_jag(world.config, "42", ["marketplace:read"]),
        credentials=world.client, fetch_json=transport.fetch_json, post_form=transport.post_form)
    assert token is not None
    assert [c.name for c in report.failures()] == ["grant_single_use"]


# --- the plugin's fixtures, as a host's CI uses them ----------------------------------------


def test_the_plugin_fixtures_drive_a_host_grant_handler(canopy_client, canopy_client_documents, canopy_redeem,
                                                        canopy_mcp_headers):
    from canopy_sdk.host import ClientKeyResolver, GrantHandler, HostConfig, ResourceVerifier
    from canopy_sdk.keys import generate_private_key
    from canopy_sdk.stores import MemoryCache, MemoryJtiStore, MemoryTokenStore

    config = HostConfig(signing_key=generate_private_key(), issuer=ISSUER, resource=RESOURCE,
                        token_endpoint=TOKEN_ENDPOINT, canopy_client_id=canopy_client.client_id,
                        scope_tools={"marketplace:read": ["marketplace_orgs_get"]})
    tokens = MemoryTokenStore()
    handler = GrantHandler(config, jti_store=MemoryJtiStore(), token_store=tokens,
                           client_keys=ClientKeyResolver(fetch_json=canopy_client_documents.__getitem__,
                                                         cache=MemoryCache()))
    form, proof = canopy_redeem(issue_id_jag(config, "u-1", ["marketplace:read"]), TOKEN_ENDPOINT, RESOURCE, ISSUER)
    access = handler.handle(form, proof).access_token

    headers = canopy_mcp_headers(access, RESOURCE)
    verifier = ResourceVerifier(config, token_store=tokens, replay_store=MemoryJtiStore())
    principal = verifier.authenticate(headers["Authorization"], [headers["DPoP"]], "POST")
    assert principal.subject == "u-1" and principal.allowed_tools == {"marketplace_orgs_get"}


def test_live_checks_are_skipped_without_options(canopy_live):
    raise AssertionError("unreachable: canopy_live skips when no host is named")  # pragma: no cover
