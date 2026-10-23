"""`Canopy-Execution`: canopy vouches for each call its gateway makes to a host, and
the host verifies it with the SDK (spec 2026-10-22-sensitive-data-agents §6).

Driven end to end: the MOUNTED canopy MCP server, a real caller token, the gateway,
a real FastMCP host (`tests/fixtures/site_gateway.py`), and the SDK's `verify_execution` on
the exact header the host received.
"""
from __future__ import annotations

import pytest
from canopy_sdk.host import ClientKeyResolver, ExecutionRefused, HostConfig, verify_execution
from canopy_sdk.keys import generate_private_key
from canopy_sdk.stores import MemoryJtiStore
from django.utils import timezone

from apps.agents.interface import parse
from apps.harness.models import Runner, RunnerFlag, Turn
from apps.host_grants import client_identity, host_gateway
from tests.fixtures import site_gateway as sg
from tests.fixtures.site_gateway import HOST_TOKEN, IFACE, RESOURCE
from tests.fixtures.site_gateway import call as _call
from tests.fixtures.site_gateway import grant as _grant
from tests.fixtures.site_gateway import visitor as _visitor

pytestmark = pytest.mark.django_db


@pytest.fixture()
def host(monkeypatch):
    """A real FastMCP server standing in for the host."""
    return sg.host_mcp(monkeypatch)


@pytest.fixture()
def w():
    """canopy's side: an agent, the site, a visitor and their conversation."""
    return sg.world()


def _sensitive_cap(w, *, sensitive=True):
    """`connect` handles sensitive data; the turn on a sensitive-data box."""
    caps = dict(IFACE["capabilities"])
    caps["connect"] = {**caps["connect"], "data": "sensitive"}
    w["agent"].interface = parse({"capabilities": caps})
    w["agent"].interface_published_by = w["owner"]
    w["agent"].save(update_fields=["interface", "interface_published_by"])
    box = Runner.objects.create(name="cloud-zdr-1", workspace=w["ws"], kind=Runner.CLOUD,
                                status=Runner.ONLINE, owner=w["owner"],
                                last_heartbeat_at=timezone.now(),
                                capabilities={"data_mode": "sensitive_data", "data_mode_hash": "abc123"})
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=w["owner"])
    if sensitive:
        w["session"].metadata = {**w["session"].metadata, "runner_requirements": ["sensitive_data"]}
        w["session"].save(update_fields=["metadata"])
    Turn.objects.filter(pk=w["turn"].pk).update(claimed_by=box)
    return box


def _host_side():
    """The host's verifier, configured as connect-labs would be."""
    config = HostConfig(signing_key=generate_private_key("EdDSA"), resource=RESOURCE,
                        canopy_client_id=client_identity.client_id())
    docs = {client_identity.client_id(): client_identity.client_metadata(),
            client_identity.jwks_uri(): client_identity.published_jwks()}
    return config, ClientKeyResolver(fetch_json=docs.__getitem__), MemoryJtiStore()


def _verify(headers, store=None, **kw):
    config, keys, jtis = _host_side()
    return verify_execution({k.title(): v for k, v in headers.items()}, config=config,
                            client_keys=keys, replay_store=store or jtis,
                            jkt=kw.pop("jkt", client_identity.dpop_jkt()), **kw)


def _calls(w, host):
    _grant(w)
    with _visitor(w):
        out = _call(host, "site_call", {"tool": "marketplace_orgs_get", "arguments": {}})
    return out.structured_content


def test_every_request_carries_a_statement_the_host_verifies(w, host):
    box = _sensitive_cap(w)
    assert _calls(w, host)["is_error"] is False
    seen = [h for h in host.seen if "canopy-execution" in h]
    assert seen and len(seen) == len(host.seen)  # every request of the MCP session
    ex = _verify(seen[-1])
    assert (ex.agent, ex.agent_sha, ex.turn) == ("ace", "", str(w["turn"].pk))
    assert ex.declaration["data"] == "sensitive" and ex.declaration["declared_by"] == "op@dimagi.com"
    assert "sha" not in ex.declaration and "agent_sha" not in ex.claims
    assert ex.runner["id"] == str(box.pk) and ex.runner_flags == {"sensitive_data"}
    assert ex.runner["flags"][0]["declared_by"] == "op@dimagi.com"
    assert ex.runner_posture == {"data_mode": "sensitive_data", "config_hash": "abc123"}
    assert ex.requirements == ("sensitive_data",) and ex.session == str(w["session"].pk)
    assert ex.sensitive_clearance(agents={"ace"}) is None
    # The statements are single-use, one per request.
    jtis = {_verify(h).claims["jti"] for h in seen}
    assert len(jtis) == len(seen)


def test_a_statement_is_spent_once_and_bound_to_its_call(w, host):
    _sensitive_cap(w)
    _calls(w, host)
    h = host.seen[-1]
    config, keys, jtis = _host_side()

    def check(headers, **kw):
        return verify_execution({k.title(): v for k, v in headers.items()}, config=config,
                                client_keys=keys, replay_store=jtis, **kw)

    check(h, jkt=client_identity.dpop_jkt())
    with pytest.raises(ExecutionRefused) as replay:
        check(h, jkt=client_identity.dpop_jkt())
    assert replay.value.code == "replayed"
    with pytest.raises(ExecutionRefused) as other_key:
        check(host.seen[0], jkt="someone-elses-key")
    assert other_key.value.code == "unbound"
    with pytest.raises(ExecutionRefused) as other_token:
        check({**host.seen[1], "authorization": "DPoP another-token"},
              jkt=client_identity.dpop_jkt())
    assert other_token.value.code == "unbound"
    with pytest.raises(ExecutionRefused) as none:
        check({"authorization": f"DPoP {HOST_TOKEN}"}, jkt=client_identity.dpop_jkt())
    assert none.value.code == "no_statement"


def test_a_host_that_trusts_another_canopy_refuses_it(w, host):
    _sensitive_cap(w)
    _calls(w, host)
    config = HostConfig(signing_key=generate_private_key("EdDSA"), resource=RESOURCE,
                        canopy_client_id="https://evil.test/oauth/client.json")
    docs = {"https://evil.test/oauth/client.json": {
        **client_identity.client_metadata(), "client_id": "https://evil.test/oauth/client.json"},
        client_identity.jwks_uri(): client_identity.published_jwks()}
    with pytest.raises(ExecutionRefused) as exc:
        verify_execution({k.title(): v for k, v in host.seen[-1].items()}, config=config,
                         client_keys=ClientKeyResolver(fetch_json=docs.__getitem__),
                         replay_store=MemoryJtiStore(), jkt=client_identity.dpop_jkt())
    assert exc.value.code == "bad_issuer"


def test_turning_sensitive_off_closes_the_door(w, host):
    _sensitive_cap(w)
    w["agent"].interface = parse(IFACE)
    w["agent"].save(update_fields=["interface"])
    # The sensitive turn's caller token itself now stands for nothing
    # (`caller_tokens._sensitive_grant`), before the gateway's own refusal.
    with pytest.raises(AssertionError):
        _calls(w, host)
    assert host.seen == []


def test_outside_sensitive_the_statement_says_there_is_no_declaration(w, host):
    _calls(w, host)  # IFACE's `connect`: not sensitive; no requirements; no runner
    ex = _verify(host.seen[-1])
    assert ex.declaration is None and ex.requirements == () and ex.agent_sha == ""
    assert ex.sensitive_clearance(agents={"ace"}) == "undeclared"


def test_sensitive_clearance_refuses_each_missing_fact(w, host):
    import dataclasses

    _sensitive_cap(w)
    _calls(w, host)
    ex = _verify(host.seen[-1])
    ok = {"agents": {"ace"}}
    assert ex.sensitive_clearance(agents={"echo"}) == "agent_not_allowed"
    assert dataclasses.replace(ex, runner={**ex.runner, "flags": []}).sensitive_clearance(**ok) == "runner_flags"
    assert dataclasses.replace(ex, runner_posture={"data_mode": ""}).sensitive_clearance(**ok) == "runner_posture"
    assert dataclasses.replace(ex, requirements=()).sensitive_clearance(**ok) == "requirements"
    assert dataclasses.replace(ex, declaration=None).sensitive_clearance(**ok) == "undeclared"


def test_a_view_call_carries_no_statement(w):
    """A person clicking in a View is not an agent's turn: canopy states nothing."""
    grant = _grant(w)
    ctx = host_gateway.context_for_grant(w["app"], grant, turn_id="", agent_slug="ace", ceiling=[])
    assert ctx.execution is None
