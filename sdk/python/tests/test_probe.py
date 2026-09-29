"""canopy's live probe: the host's probe endpoint, and the live-grant conformance steps.

Most of this file is refusals, because that is where the security lives: the
probe endpoint signs a REAL ID-JAG, so it must do so for canopy's client only,
for the one configured principal and scope only, whatever the request says.
"""
from __future__ import annotations

import json
import time
import uuid
from unittest import mock
from urllib.parse import urlsplit

import jwt
import pytest
from django.test import Client, override_settings

from canopy_sdk import conformance, contract
from canopy_sdk.host import (
    DPoPGate, GrantRefused, HostConfig, ProbeDisabled, ProbeHandler, ProbeIdentity,
    authorization_server_metadata, delegated_principal, presented_dpop_jkt,
)
from canopy_sdk.keys import generate_private_key, private_pem
from canopy_sdk.stores import MemoryJtiStore

from .helpers import CANOPY, CLIENT_ID, ISSUER, RESOURCE, SCOPE_TOOLS, TOKEN_ENDPOINT, World, call_asgi

PROBE_ENDPOINT = f"{ISSUER}/canopy-host/probe/"
PROBE = ProbeIdentity(endpoint=PROBE_ENDPOINT, subject="7", scope="marketplace:read",
                      tool="marketplace_rounds_list", arguments={"limit": 1},
                      denied_tool="admin_delete", page="marketplace:network")
TOOLS = {**SCOPE_TOOLS, "admin:write": {"admin_delete"}}


class ProbeWorld(World):
    """A host with a probe identity. Subject "7" is the probe principal."""

    def __init__(self, *, probe=PROBE, active=("42", "7")):
        super().__init__(active_subjects=active)
        self.config = HostConfig(
            signing_key=private_pem(self.host_key), canopy_base_url=CANOPY, app_name="connect-labs",
            issuer=ISSUER, resource=RESOURCE, token_endpoint=TOKEN_ENDPOINT,
            canopy_client_id=CLIENT_ID, scope_tools=TOOLS, probe=probe)
        from canopy_sdk.host import GrantHandler, ResourceVerifier

        self.handler = GrantHandler(self.config, jti_store=self.jti_store, token_store=self.token_store,
                                    client_keys=self.resolver, subject_active=self.active.__contains__)
        self.verifier = ResourceVerifier(self.config, token_store=self.token_store,
                                         replay_store=MemoryJtiStore(), subject_active=self.active.__contains__)
        self.probe_jtis = MemoryJtiStore()
        self.prober = ProbeHandler(self.config, jti_store=self.probe_jtis, client_keys=self.resolver,
                                   subject_active=self.active.__contains__)

    def probe_form(self, **extra) -> dict:
        form = {"client_id": CLIENT_ID, "client_assertion_type": contract.CLIENT_ASSERTION_TYPE,
                "client_assertion": self.client_assertion()}
        form.update(extra)
        return form

    def ask(self, proof="default", **extra):
        if proof == "default":
            proof = self.proof(htu=PROBE_ENDPOINT)
        return self.prober.handle(self.probe_form(**extra), proof)


@pytest.fixture
def world():
    return ProbeWorld()


def refused(world, **kwargs) -> GrantRefused:
    with pytest.raises(GrantRefused) as info:
        world.ask(**kwargs)
    return info.value


# --- configuration ----------------------------------------------------------------------


def test_a_probe_must_be_read_only_and_inside_its_scope():
    with pytest.raises(ValueError, match="read-only"):
        ProbeIdentity(endpoint=PROBE_ENDPOINT, subject="7", scope="admin:write", tool="admin_delete")
    base = dict(signing_key=generate_private_key(), issuer=ISSUER, resource=RESOURCE,
                token_endpoint=TOKEN_ENDPOINT, canopy_client_id=CLIENT_ID, scope_tools=TOOLS)
    with pytest.raises(ValueError, match="not one"):
        HostConfig(**base, probe=ProbeIdentity(endpoint=PROBE_ENDPOINT, subject="7",
                                               scope="marketplace:read", tool="admin_delete"))
    with pytest.raises(ValueError, match="not in scope_tools"):
        HostConfig(**base, probe=ProbeIdentity(endpoint=PROBE_ENDPOINT, subject="7",
                                               scope="other:read", tool="x"))
    with pytest.raises(ValueError, match="inside its own scope"):
        HostConfig(**base, probe=ProbeIdentity(endpoint=PROBE_ENDPOINT, subject="7", scope="marketplace:read",
                                               tool="marketplace_rounds_list",
                                               denied_tool="marketplace_orgs_get"))
    with pytest.raises(ValueError, match="needs a subject"):
        ProbeIdentity(endpoint=PROBE_ENDPOINT, subject=" ", scope="marketplace:read", tool="x")


def test_the_metadata_advertises_the_probe_only_while_it_is_configured(world):
    assert authorization_server_metadata(world.config)[contract.PROBE_ENDPOINT_METADATA_FIELD] == PROBE_ENDPOINT
    off = ProbeWorld(probe=None)
    assert contract.PROBE_ENDPOINT_METADATA_FIELD not in authorization_server_metadata(off.config)


def test_an_unconfigured_probe_is_disabled_not_a_refusal():
    off = ProbeWorld(probe=None)
    with pytest.raises(ProbeDisabled):
        off.ask()


# --- success ------------------------------------------------------------------------------


def test_the_probe_issues_a_real_short_single_use_id_jag_for_the_probe_principal(world):
    result = world.ask()
    body = result.body()
    assert body["subject"] == "7" and body["scope"] == "marketplace:read"
    assert body["tool"] == "marketplace_rounds_list" and body["arguments"] == {"limit": 1}
    assert body["denied_tool"] == "admin_delete" and body["resource"] == RESOURCE
    assert result.headers()["Cache-Control"] == "no-store"
    assert "id_jag" not in repr(result) and body["id_jag"] not in repr(result)

    header = jwt.get_unverified_header(body["id_jag"])
    claims = jwt.decode(body["id_jag"], options={"verify_signature": False})
    assert header["typ"] == contract.ID_JAG_TYP
    assert claims[contract.PROBE_CLAIM] is True
    assert claims["sub"] == "7" and claims["scope"] == "marketplace:read"
    assert claims["exp"] - claims["iat"] <= contract.ID_JAG_MAX_LIFETIME

    # It is a grant like any other: the normal jwt-bearer path redeems it, once.
    token = world.redeem(assertion=body["id_jag"])
    assert token.token.subject == "7" and token.token.scopes == ("marketplace:read",)
    with pytest.raises(GrantRefused) as replay:
        world.redeem(assertion=body["id_jag"])
    assert replay.value.reason == "replayed"


# --- refusals: who is asking -------------------------------------------------------------------


def test_a_different_client_is_refused(world):
    err = refused(world, client_id="https://evil.example/oauth/client.json")
    assert err.code == "invalid_client" and err.reason == "unknown_client"


def test_a_missing_client_assertion_is_refused(world):
    err = refused(world, client_assertion="")
    assert err.code == "invalid_client" and err.reason == "no_client_assertion"


def test_a_client_assertion_signed_by_another_key_is_refused(world):
    forged = jwt.encode({"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": ISSUER, "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "jti": str(uuid.uuid4())},
                        generate_private_key("EdDSA"), algorithm="EdDSA",
                        headers={"kid": world.client_jwk["kid"]})
    err = refused(world, client_assertion=forged)
    assert err.code == "invalid_client"


def test_a_client_assertion_for_another_audience_is_refused(world):
    err = refused(world, client_assertion=world.client_assertion(aud="https://elsewhere.example"))
    assert err.code == "invalid_client"


def test_the_probe_endpoint_itself_is_an_accepted_audience(world):
    assert world.ask(client_assertion=world.client_assertion(aud=PROBE_ENDPOINT)).subject == "7"


def test_no_dpop_proof_is_refused(world):
    err = refused(world, proof=None)
    assert err.code == "invalid_dpop_proof"


def test_a_proof_for_another_url_is_refused(world):
    err = refused(world, proof=world.proof(htu=TOKEN_ENDPOINT))
    assert err.code == "invalid_dpop_proof"


def test_a_dpop_key_that_is_the_client_key_is_refused(world):
    from canopy_sdk.jose import make_dpop_proof

    err = refused(world, proof=make_dpop_proof(world.client.client_key, "POST", PROBE_ENDPOINT))
    assert err.reason == "dpop_is_client_key"


def test_a_replayed_client_assertion_or_proof_is_refused(world):
    assertion = world.client_assertion()
    proof = world.proof(htu=PROBE_ENDPOINT)
    world.ask(client_assertion=assertion)
    err = refused(world, client_assertion=assertion)
    assert err.reason == "replayed"
    world.ask(proof=proof)
    err = refused(world, proof=proof)
    assert err.reason == "replayed"


def test_an_inactive_probe_principal_gets_nothing(world):
    world.active.discard("7")
    err = refused(world)
    assert err.code == "invalid_grant" and err.reason == "probe_subject_inactive"


# --- refusals: what is being asked for ----------------------------------------------------------


@pytest.mark.parametrize("field", ["sub", "subject", "login_hint", "user_id", "username", "assertion",
                                   "tool", "arguments"])
def test_a_request_naming_its_own_principal_or_tool_is_refused(world, field):
    err = refused(world, **{field: "42"})
    assert err.code == "invalid_request" and err.reason == f"probe_names_{field}"


def test_a_request_for_another_scope_is_refused(world):
    for scope in ("admin:write", "marketplace:read admin:write", ""):
        assert refused(world, scope=scope).code == "invalid_scope"
    assert world.ask(scope="marketplace:read").scope == "marketplace:read"


def test_a_request_for_another_resource_is_refused(world):
    assert refused(world, resource="https://labs.example.org/other/").code == "invalid_target"


def test_a_refused_request_spends_nothing(world):
    """Every jti is consumed only after every check passed, so a refusal does
    not burn the statements it carried."""
    assertion = world.client_assertion()
    proof = world.proof(htu=PROBE_ENDPOINT)
    refused(world, client_assertion=assertion, proof=proof, sub="42")
    assert world.ask(client_assertion=assertion, proof=proof).subject == "7"


def test_no_request_ever_gets_an_id_jag_for_anyone_but_the_probe_principal(world):
    for extra in ({}, {"scope": "marketplace:read"}, {"resource": RESOURCE}):
        claims = jwt.decode(world.ask(**extra).id_jag, options={"verify_signature": False})
        assert claims["sub"] == "7" and claims["scope"] == "marketplace:read"


# --- the Django view -------------------------------------------------------------------------------


@pytest.fixture
def django_host(world):
    from django.core.cache import cache

    settings = {
        "SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY, "APP_NAME": "connect-labs",
        "CLIENT_ID": CLIENT_ID, "ISSUER": ISSUER, "RESOURCE": RESOURCE, "TOKEN_ENDPOINT": TOKEN_ENDPOINT,
        "SCOPE_TOOLS": {k: sorted(v) for k, v in TOOLS.items()},
        "PROBE": {"ENDPOINT": "http://testserver/canopy/probe/", "SUBJECT_RESOLVER": "tests.test_probe.probe_user",
                  "SCOPE": "marketplace:read", "TOOL": "marketplace_rounds_list", "ARGUMENTS": {"limit": 1},
                  "DENIED_TOOL": "admin_delete"},
    }
    cache.clear()
    with override_settings(CANOPY_HOST=settings), \
            mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        yield world
    cache.clear()


def probe_user() -> str:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(username="canopy-probe").first()
    return str(user.pk) if user else ""


def _post_probe(world, **extra):
    return Client().post("/canopy/probe/", world.probe_form(**extra),
                         headers={"DPoP": world.proof(htu="http://testserver/canopy/probe/")})


@pytest.mark.django_db
def test_the_django_probe_view_issues_for_the_resolved_probe_user(django_host):
    from django.contrib.auth import get_user_model

    probe = get_user_model().objects.create_user("canopy-probe")
    r = _post_probe(django_host)
    assert r.status_code == 200, r.content
    assert r.json()["subject"] == str(probe.pk) and r["Cache-Control"] == "no-store"
    from django.test import RequestFactory

    from canopy_sdk.django import views

    doc = views.authorization_server_metadata_view(RequestFactory().get("/"))
    assert json.loads(doc.content)[contract.PROBE_ENDPOINT_METADATA_FIELD] == "http://testserver/canopy/probe/"


@pytest.mark.django_db
def test_the_django_probe_view_404s_until_the_probe_user_exists(django_host):
    r = _post_probe(django_host)
    assert r.status_code == 404


@pytest.mark.django_db
def test_the_django_probe_view_404s_when_unconfigured(world):
    with override_settings(CANOPY_HOST={}):
        r = Client().post("/canopy/probe/", world.probe_form(),
                          headers={"DPoP": world.proof(htu="http://testserver/canopy/probe/")})
    assert r.status_code == 404


@pytest.mark.django_db
def test_the_django_probe_view_refuses_a_request_naming_a_principal(django_host):
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_user("canopy-probe")
    r = _post_probe(django_host, subject="1")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


@pytest.mark.django_db
def test_a_broken_probe_block_turns_the_probe_off_not_the_grant(world, caplog):
    from canopy_sdk.django import conf

    settings = {"SIGNING_KEY": private_pem(world.host_key), "CLIENT_ID": CLIENT_ID, "ISSUER": ISSUER,
                "RESOURCE": RESOURCE, "TOKEN_ENDPOINT": TOKEN_ENDPOINT,
                "SCOPE_TOOLS": {"marketplace:read": ["x"]},
                "PROBE": {"ENDPOINT": PROBE_ENDPOINT, "SUBJECT": "7", "SCOPE": "marketplace:read",
                          "TOOL": "not_in_scope"}}
    with override_settings(CANOPY_HOST=settings):
        config = conf.get_host_config()
    assert config.grant_enabled and config.probe is None


# --- conformance: the live grant, end to end -------------------------------------------------------


def mcp_app(verifier, ran: list, *, enforce_scope=True):
    """A Streamable-HTTP JSON-RPC endpoint: lists and calls only the principal's
    tools — or, with ``enforce_scope=False``, a BROKEN host that authenticates
    the token but runs any tool it is asked for."""

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
        method = request["method"]
        if method == "initialize":
            out = {"result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "host", "version": "1"}}}
        elif method == "tools/list":
            names = ["marketplace_orgs_get", "marketplace_rounds_list", "admin_delete"]
            out = {"result": {"tools": principal.filter_tools([{"name": n} for n in names])}}
        else:
            name = request["params"]["name"]
            if enforce_scope and not principal.allows(name):
                out = {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            else:
                ran.append((name, principal.subject, request["params"].get("arguments")))
                out = {"result": {"content": [{"type": "text", "text": "[]"}], "isError": False}}
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], **out}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return app


class LiveTransport:
    def __init__(self, world: ProbeWorld, *, lenient_mcp=False):
        self.world = world
        self.ran: list = []
        inner = mcp_app(world.verifier, self.ran, enforce_scope=not lenient_mcp)
        self.gate = DPoPGate(inner, world.verifier)

    def fetch_json(self, url):
        assert url == contract.metadata_url(ISSUER)
        return authorization_server_metadata(self.world.config)

    def post_form(self, url, data, headers, what=""):
        handler = {TOKEN_ENDPOINT: self.world.handler.handle, PROBE_ENDPOINT: self.world.prober.handle}[url]
        try:
            return 200, handler(data, headers.get("DPoP")).body(), {}
        except GrantRefused as refused_:
            return refused_.status, refused_.body(), {}

    def post_json(self, url, payload, headers=None):
        status, resp_headers, body = call_asgi(self.gate, "POST", urlsplit(url).path, headers=headers,
                                               body=json.dumps(payload).encode())
        return status, body, resp_headers


def test_the_live_chain_passes_against_a_conforming_host(world):
    t = LiveTransport(world)
    report = conformance.run_live(ISSUER, RESOURCE, world.client, fetch_json=t.fetch_json,
                                  post_form=t.post_form, post_json=t.post_json)
    assert report.ok, str(report)
    names = {c.name for c in report.checks}
    assert {"probe_issued", "live_grant_redeemed", "live_grant_scope", "probe_tool_succeeds",
            "out_of_scope_not_listed", "out_of_scope_refused", "dpop_required_no_proof",
            "dpop_required_wrong_key", "dpop_required_not_bearer"} <= names
    assert t.ran == [("marketplace_rounds_list", "7", {"limit": 1})], "only the probe tool ran, as the probe user"


def test_check_live_grant_returns_the_token_material(world):
    t = LiveTransport(world)
    report, live = conformance.check_live_grant(ISSUER, RESOURCE, world.client, fetch_json=t.fetch_json,
                                                post_form=t.post_form)
    assert report.ok and live.probe.subject == "7"
    assert live.token.access_token and live.token.scope == "marketplace:read"
    assert live.probe.id_jag not in repr(live.probe), "the grant is never printed"


def test_a_host_whose_mcp_runs_out_of_scope_tools_fails_b(world):
    t = LiveTransport(world, lenient_mcp=True)
    report = conformance.run_live(ISSUER, RESOURCE, world.client, fetch_json=t.fetch_json,
                                  post_form=t.post_form, post_json=t.post_json)
    assert {c.name for c in report.failures()} == {"out_of_scope_refused"}


def test_a_host_without_a_probe_says_so(world):
    off = ProbeWorld(probe=None)
    t = LiveTransport(off)
    with pytest.raises(conformance.ProbeError) as info:
        conformance.request_probe(ISSUER, off.client, fetch_json=t.fetch_json, post_form=t.post_form)
    assert info.value.code == "no_probe_endpoint"
    report = conformance.run_live(ISSUER, RESOURCE, off.client, fetch_json=t.fetch_json, post_form=t.post_form)
    assert not report.ok and report.checks[0].detail.startswith("no_probe_endpoint")


def test_a_client_the_host_does_not_accept_gets_no_probe(world):
    t = LiveTransport(world)
    from canopy_sdk import consumer

    stranger = consumer.ClientCredentials.generate(CLIENT_ID)
    with pytest.raises(conformance.ProbeError) as info:
        conformance.request_probe(ISSUER, stranger, fetch_json=t.fetch_json, post_form=t.post_form)
    assert info.value.code == "probe_refused" and "invalid_client" in info.value.message


def test_a_probe_answer_that_is_not_marked_as_a_probe_is_refused(world):
    t = LiveTransport(world)
    real = t.post_form

    def unmarked(url, data, headers, what=""):
        status, body, h = real(url, data, headers, what)
        if url == PROBE_ENDPOINT and status == 200:
            from canopy_sdk.host import issue_id_jag

            body["id_jag"] = issue_id_jag(world.config, "7", ["marketplace:read"])
        return status, body, h

    with pytest.raises(conformance.ProbeError) as info:
        conformance.request_probe(ISSUER, world.client, fetch_json=t.fetch_json, post_form=unmarked)
    assert info.value.code == "bad_probe_response"
