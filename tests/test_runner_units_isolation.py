"""A unit's token reaches only its unit's agents (plan Task 6.4, Review Focus 2).

Two isolation units on one box — the `connect` tree and the agent unit for eva —
each paired by the box's one approval, each with its own software key and its own
runner token. The rule is per runner and already exists; this pins that a unit IS
a runner for every door a secret could leave by:

* unit A's proof for an agent routed only to B is `401 not_routed`;
* unit A's runner token is refused for that agent, and cannot open B's runner routes;
* B's vault wraps are never addressed to A's ECDH key.
"""
from __future__ import annotations

import pytest
from canopy_sdk.jose import make_dpop_proof
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client

from apps.agents.models import Agent
from apps.harness import device_keys, runner_auth
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey
from tests.fixtures.runner_invitations import make_org
from tests.fixtures.runner_units import BOX, _approve, _invite, _pair_units

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("sts", "fresh_cache")]


@pytest.fixture
def org():
    return make_org()

RESOLVE = "/api/agents/eva/credentials/resolve"
HTU = f"http://testserver{RESOLVE}"
A, B = BOX, f"{BOX}-agent-eva"


def _jwk(key) -> dict:
    from canopy_sdk.keys import header_jwk

    return device_keys.normalize_public_jwk(header_jwk(key))


@pytest.fixture
def box(org):
    """Units A (`tree:connect`) and B (`agent:eva`) on one machine, paired by one
    approval; eva routed to B only; its secret stored in canopy-web and bound to an
    approved device."""
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=org["ws"], owner=org["user"],
                               runtime_secrets=["canopy-pat"], credential_source=Agent.CANOPY_WEB,
                               require_device_binding=True)
    org["browser"].put("/api/agents/eva/credentials", data={"values": {"canopy-pat": "eva-pat"}},
                       content_type="application/json")
    units = [{"name": A, "workspace": "connect", "kind": "tree"},
             {"name": B, "workspace": "connect", "kind": "agent", "slug": "eva"}]
    signing = {A: ec.generate_private_key(ec.SECP256R1()), B: ec.generate_private_key(ec.SECP256R1())}
    ecdh = {A: _jwk(ec.generate_private_key(ec.SECP256R1())), B: _jwk(ec.generate_private_key(ec.SECP256R1()))}
    inv = _invite(org, units=[{**u, "ecdh_public_jwk": ecdh[u["name"]]} for u in units]).json()
    _approve(org, inv["id"])
    res = _pair_units({n: {"public_jwk": _jwk(signing[n]), "ecdh_public_jwk": ecdh[n]} for n in signing})
    assert res.status_code == 201, res.content
    ids = res.json()["units"]
    runners = {n: Runner.objects.get(pk=ids[n]) for n in ids}
    Runner.objects.filter(pk__in=ids.values()).update(status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=eva, runner=runners[B], rank=0)
    tokens = {n: RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner=runners[n]))[0] for n in ids}
    return {**org, "eva": eva, "runners": runners, "signing": signing, "tokens": tokens}


def _resolve(bearer: str, proof: str | None = None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {bearer}"}
    if proof:
        headers["HTTP_DPOP"] = proof
    return Client().get(RESOLVE, **headers)


def test_unit_a_proof_for_an_agent_routed_only_to_b_is_not_routed(box):
    res = _resolve(box["pat"], make_dpop_proof(box["signing"][A], "GET", HTU))
    assert res.status_code == 401, res.content
    assert res.json()["reason"] == "not_routed"
    assert "eva-pat" not in res.content.decode()
    # B's own proof is let through.
    ok = _resolve(box["pat"], make_dpop_proof(box["signing"][B], "GET", HTU))
    assert ok.status_code == 200 and ok.json()["values"] == {"canopy-pat": "eva-pat"}


def test_even_a_routing_row_to_unit_a_does_not_let_a_read_eva(box):
    """An assignment that predates B (or a write that slipped past) does not make
    A an eva runner: eva has its own unit on this machine (`routing.unit_refusal`)."""
    RunnerAssignment.objects.create(agent=box["eva"], runner=box["runners"][A], rank=1)
    res = _resolve(box["pat"], make_dpop_proof(box["signing"][A], "GET", HTU))
    assert res.status_code == 401 and res.json()["reason"] == "not_routed"


def test_unit_a_token_is_refused_for_bs_agent(box):
    res = _resolve(box["tokens"][A])
    assert res.status_code == 403 and "eva-pat" not in res.content.decode()
    res = _resolve(box["tokens"][A], make_dpop_proof(box["signing"][A], "GET", HTU))
    assert res.status_code == 403
    # …and its token with B's proof is not B's either: a proof goes with its own box.
    res = _resolve(box["tokens"][A], make_dpop_proof(box["signing"][B], "GET", HTU))
    assert res.status_code == 403
    ok = _resolve(box["tokens"][B], make_dpop_proof(box["signing"][B], "GET", HTU))
    assert ok.status_code == 200


def test_unit_a_token_cannot_open_bs_runner_routes(box):
    a, b = box["runners"][A], box["runners"][B]
    assert runner_auth.reaches("POST", f"/api/harness/runners/{a.pk}/heartbeat", a.pk)
    assert not runner_auth.reaches("POST", f"/api/harness/runners/{b.pk}/heartbeat", a.pk)
    assert not runner_auth.reaches("POST", f"/api/harness/runners/{b.pk}/claim", a.pk)
    assert not runner_auth.reaches_ws(f"/ws/runner/{b.pk}/", a.pk)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {box['tokens'][A]}")
    res = client.post(f"/api/harness/runners/{b.pk}/claim")
    assert res.status_code in (401, 403, 404), res.status_code
    assert client.post(f"/api/harness/runners/{a.pk}/claim").status_code in (200, 204)


def test_bs_wraps_are_never_addressed_to_as_ecdh_key(box):
    from apps.agents.credentials import zk

    eligible = {k.runner_id for k in zk.eligible_device_keys(box["eva"])}
    assert box["runners"][B].pk in eligible
    assert box["runners"][A].pk not in eligible
    # Who a member is asked to grant eva's vault key to: B's ECDH key, never A's.
    targets = {t["runner"] for t in zk.pending_targets(box["eva"]) if t["kind"] == "device"}
    assert targets == {str(box["runners"][B].pk)}
