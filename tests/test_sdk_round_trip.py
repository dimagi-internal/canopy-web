"""The two halves of the host grant contract, against each other.

A host builds against `canopy_sdk.host` (sdk/python); canopy-web redeems with
its own code in `apps/tokens/`. Each side has its own suite, and both suites
passing has never proved the two AGREE — connect-labs' host shipped against a
doc while canopy's side was tested against hand-built fakes of it. This test
puts the SDK's REAL host half on the other end of canopy's REAL code:

1. **Arrival.** `canopy_sdk.host` signs the visitor assertion and an ID-JAG;
   they go to canopy's real `/api/auth/contact-token`.
2. **Redemption.** canopy discovers the host from metadata the SDK generated,
   and POSTs the jwt-bearer grant to the SDK's `GrantHandler`, served in
   process. The handler fetches canopy's client metadata and JWKS from
   canopy's REAL `/oauth/client.json` and `/oauth/jwks.json`, verifies the
   `private_key_jwt`, the DPoP proof and the ID-JAG, and issues a bound token.
3. **Use.** canopy's `site_call` (the mounted MCP tool) reaches the host's MCP
   through `host_gateway`; the SDK's `DPoPGate` + `ResourceVerifier` verify
   every proof and resolve the visitor, and the tool runs as them.

So a change to either side that breaks the wire fails HERE, in canopy-web's CI,
not in a host's production.
"""
from __future__ import annotations

import contextlib
from urllib.parse import urlsplit

import httpx2
import pytest
from asgiref.sync import async_to_sync
from canopy_sdk import contract
from canopy_sdk.host import (
    ClientKeyResolver, DPoPGate, DPoPRefused, GrantHandler, GrantRefused, HostConfig, ResourceVerifier,
    authorization_server_metadata, delegated_principal, issue_id_jag, sign_visitor_assertion,
)
from canopy_sdk.keys import generate_private_key, public_pem
from canopy_sdk.stores import MemoryCache, MemoryJtiStore, MemoryTokenStore
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.common.encryption import decrypt_secret
from apps.contacts.models import Contact
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.tokens import assertions, client_identity, host_gateway, outbound
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ISSUER = "https://host.test"
RESOURCE = "https://host.test/mcp/"
TOKEN_ENDPOINT = "https://host.test/o/token/"
SUBJECT = "u-42"
IFACE = {"capabilities": {
    "connect": {"callers": ["contact"], "sites": ["connect-labs"],
                "ceiling": ["mcp__*connect_labs__marketplace_*"],
                "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


class Host:
    """A host built ONLY from `canopy_sdk.host` — no canopy code on this side."""

    def __init__(self):
        self.key = generate_private_key("EdDSA")
        self.config = HostConfig(
            signing_key=self.key,
            canopy_base_url=assertions.audience(),
            app_name="connect-labs",
            issuer=ISSUER, resource=RESOURCE, token_endpoint=TOKEN_ENDPOINT,
            canopy_client_id=client_identity.client_id(),
            scope_tools={"marketplace:read": {"marketplace_orgs_get", "marketplace_rounds_list"}},
        )
        self.tokens = MemoryTokenStore()
        self.fetched: list[str] = []
        self.handler = GrantHandler(
            self.config, jti_store=MemoryJtiStore(), token_store=self.tokens,
            client_keys=ClientKeyResolver(fetch_json=self._fetch_canopy, cache=MemoryCache()),
            subject_active=lambda sub: sub == SUBJECT)
        self.verifier = ResourceVerifier(self.config, token_store=self.tokens, replay_store=MemoryJtiStore())
        self.proofs: list[str] = []
        self.ran_as: list[tuple[str, str]] = []

        server = FastMCP("host")

        @server.tool
        def marketplace_orgs_get() -> dict:
            principal = delegated_principal.get()
            self.ran_as.append(("marketplace_orgs_get", principal.subject if principal else ""))
            if principal is None or not principal.allows("marketplace_orgs_get"):
                raise PermissionError("not in scope")
            return {"orgs": ["llo-foo"]}

        self.inner = server.http_app(path="/mcp/", stateless_http=True, json_response=True)

    # canopy's CIMD and JWKS, fetched from canopy's REAL views.
    def _fetch_canopy(self, url: str) -> dict:
        self.fetched.append(url)
        assert url in (client_identity.client_id(), client_identity.jwks_uri())
        response = Client().get(urlsplit(url).path)
        assert response.status_code == 200
        return response.json()

    # The host's HTTP surface, as canopy's outbound door reaches it.
    def get_json(self, url, *, what="URL"):
        assert url == contract.metadata_url(ISSUER)
        return authorization_server_metadata(self.config)

    def post_form(self, url, data, *, headers, what="URL"):
        assert url == TOKEN_ENDPOINT
        try:
            result = self.handler.handle(dict(data), headers.get(contract.DPOP_HEADER))
        except GrantRefused as refused:
            return refused.status, refused.body(), refused.headers()
        return 200, result.body(), result.headers()


@pytest.fixture()
def host(monkeypatch):
    h = Host()
    monkeypatch.setattr(outbound, "get_json", h.get_json)
    monkeypatch.setattr(outbound, "post_form", h.post_form)
    monkeypatch.setattr(outbound, "check_url", lambda url, what="URL": url)
    # The gate records each request's proof, then verifies it.
    wrapped = DPoPGate(h.inner, h.verifier, require_principal=True)

    async def entry(scope, receive, send):
        if scope["type"] == "http":
            h.proofs.extend(v.decode() for k, v in scope["headers"] if k.lower() == b"dpop")
        await wrapped(scope, receive, send)

    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=entry))
    return h


@pytest.fixture()
def site(host):
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, interface=parse(IFACE))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    # What the host publishes, registered in canopy.
    app.public_keys = [public_pem(host.key)]
    app.host_issuer = ISSUER
    app.host_mcp_resource = RESOURCE
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return {"owner": owner, "ws": ws, "agent": agent, "app": app}


def _arrive(host, *, scopes=("marketplace:read",)):
    body = {
        "assertion": sign_visitor_assertion(host.config, SUBJECT, name="Gillian"),
        "agent_slug": "ace",
    }
    if scopes:
        body["id_jag"] = issue_id_jag(host.config, SUBJECT, scopes)
    return Client().post(contract.ARRIVAL_PATH, data=body, content_type="application/json")


@contextlib.contextmanager
def _as_visitor(turn):
    access = async_to_sync(CanopyPATVerifier().verify_token)(caller_tokens.mint(turn))
    assert access is not None
    token = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _call(host, name, args):
    async def main():
        async with host.inner.router.lifespan_context(host.inner):
            try:
                return True, await mcp.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - re-raised outside the task group
                return False, exc
    ok, out = async_to_sync(main)()
    if not ok:
        raise out
    return out


def _turn_for_the_visitor(site, contact):
    session = Session.objects.create(workspace=site["ws"], agent=site["agent"], contact=contact,
                                     metadata={"embed_app": "connect-labs"},
                                     page_state={"resource": "labs-marketplace://orgs",
                                                 "backing_tool": "marketplace_orgs_get"})
    return Turn.objects.create(chat_session=session, prompt="which orgs?", initiator_contact=contact,
                               initiator_kind="contact", capability="connect")


def test_the_sdk_host_and_canopy_complete_the_whole_contract(site, host):
    # 1 + 2: arrival and redemption, across the real seam.
    r = _arrive(host)
    assert r.status_code == 200, r.content
    assert r.json()["host_grant"] is True, "canopy redeemed the SDK host's grant"

    # The host fetched canopy's REAL client metadata and JWKS to authenticate it.
    assert host.fetched == [client_identity.client_id(), client_identity.jwks_uri()]
    [issued] = list(host.tokens._tokens.values())
    assert issued.subject == SUBJECT and issued.client_id == client_identity.client_id()
    assert issued.cnf_jkt == client_identity.dpop_jkt(), "bound to canopy's DPoP key, not its client key"
    assert issued.scopes == ("marketplace:read",)

    grant = HostGrant.objects.get()
    raw = decrypt_secret(grant.access_token_enc)
    assert contract.token_checksum(raw) == issued.token_checksum, "canopy stored the token the host issued"
    assert grant.dpop_jkt == issued.cnf_jkt and grant.scope == "marketplace:read"

    # 3: the agent's call, through canopy's mounted MCP, verified by the SDK.
    contact = Contact.objects.get(external_id=SUBJECT)
    turn = _turn_for_the_visitor(site, contact)
    with _as_visitor(turn):
        out = _call(host, "site_call", {"tool": "marketplace_orgs_get", "arguments": {}})
    body = out.structured_content
    assert body["is_error"] is False, body
    assert host.ran_as == [("marketplace_orgs_get", SUBJECT)], "the host ran the tool AS the visitor"
    assert host.proofs, "every request carried a proof the SDK verified"

    # Everything the SDK refuses, it refuses for canopy's statements too.
    with pytest.raises(DPoPRefused) as replay:
        host.verifier.check_proof([host.proofs[0]], "POST", raw)
    assert replay.value.code == "replayed"
    assert host.verifier.resolve(raw, None) is None, "the bound token is useless as a bearer"


def test_a_replayed_arrival_grant_is_refused_by_the_sdk_host_and_the_chat_still_opens(site, host):
    id_jag = issue_id_jag(host.config, SUBJECT, ["marketplace:read"])
    for expected in (True, False):
        body = {"assertion": sign_visitor_assertion(host.config, SUBJECT), "agent_slug": "ace",
                "id_jag": id_jag}
        r = Client().post(contract.ARRIVAL_PATH, data=body, content_type="application/json")
        assert r.status_code == 200 and r.json()["token"]
        assert r.json()["host_grant"] is expected
    assert len(host.tokens._tokens) == 1


def test_a_grant_canopy_refuses_never_reaches_the_sdk_host(site, host):
    other = HostConfig(signing_key=host.key, issuer=ISSUER, resource="https://host.test/other/",
                       token_endpoint=TOKEN_ENDPOINT, canopy_client_id=client_identity.client_id(),
                       scope_tools={"marketplace:read": ["x"]})
    body = {"assertion": sign_visitor_assertion(host.config, SUBJECT), "agent_slug": "ace",
            "id_jag": issue_id_jag(other, SUBJECT, ["marketplace:read"])}
    r = Client().post(contract.ARRIVAL_PATH, data=body, content_type="application/json")
    assert r.json()["host_grant"] is False
    assert host.fetched == [] and not host.tokens._tokens, "canopy checked first and sent nothing"


def test_the_sdk_host_refuses_a_canopy_whose_keys_it_cannot_match(site, host, monkeypatch):
    """If canopy's published JWKS stopped matching the key it signs with, the
    host must refuse — proven against canopy's real views."""
    monkeypatch.setattr(client_identity, "published_jwks",
                        lambda: {"keys": [{**client_identity.public_jwk(generate_private_key().public_key()),
                                           "use": "sig"}]})
    assert _arrive(host).json()["host_grant"] is False
    assert not host.tokens._tokens


def test_a_host_requiring_zdr_keeps_its_visitor_off_a_non_zdr_runner(site, host, settings):
    """The whole path: the SDK's real signer, canopy's real arrival, a real session
    start and send over the contact API, then the claim on two real runners."""
    import dataclasses

    from django.utils import timezone

    from apps.harness.models import Runner, RunnerAssignment, RunnerFlag
    from apps.harness.services import PROFILES_VERSION, claim_next_turn

    settings.CHAT_STUB_EXECUTOR = False   # leave the turn queued for a real runner
    host.config = dataclasses.replace(host.config, runner_requirements=("zdr",))
    r = _arrive(host, scopes=())
    assert r.status_code == 200, r.content
    auth = {"HTTP_AUTHORIZATION": f"Bearer {r.json()['token']}"}
    c = Client()
    started = c.post("/api/contact/sessions", data={"agent_slug": "ace"},
                     content_type="application/json", **auth)
    assert started.status_code == 200, started.content
    sid = started.json()["id"]
    assert Session.objects.get(pk=sid).metadata["runner_requirements"] == ["zdr"]
    sent = c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hello"},
                  content_type="application/json", **auth)
    assert sent.status_code == 200, sent.content
    turn = Turn.objects.get(chat_session_id=sid)

    def runner(name, rank):
        rn = Runner.objects.create(name=name, workspace=site["ws"], kind=Runner.CLOUD,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   owner=site["owner"], host=name,
                                   capabilities={"sessions": True, "profiles": PROFILES_VERSION, "envelope": 2})
        RunnerAssignment.objects.create(agent=site["agent"], runner=rn, rank=rank)
        return rn

    laptop = runner("laptop", 0)
    cloud = runner("cloud", 1)
    RunnerFlag.objects.create(runner=cloud, flag="zdr", declared_by=site["owner"])

    assert claim_next_turn(laptop) is None, "a non-ZDR runner must not get the visitor's turn"
    claimed = claim_next_turn(cloud)
    assert claimed is not None and claimed.id == turn.id
