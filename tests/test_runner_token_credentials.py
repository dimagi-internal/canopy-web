"""A runner's own token stages the agents that runner serves — no owner PAT.

On 2026-10-17 the laptop runner's owner PAT was revoked in a token cleanup. The box
kept heartbeating on its device-key runner token (`POST /api/runner-sessions/`), but
`canopy agent prepare` on it could no longer read Hal's or Eva's secrets: the token
reached `/credentials/resolve` but not the two reads that say what is being resolved
(`/runtime`, `/credentials/access`), and nothing tied it to its own box. Now it reads
exactly what its box serves, and nothing its owner merely administers.
"""
from __future__ import annotations

import pytest
from canopy_sdk.jose import make_dpop_proof
from canopy_sdk.keys import header_jwk
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredentialRead
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from tests.test_canopy_vault_phase1b import (  # noqa: F401 — fixtures
    HTU,
    RESOLVE,
    _active_role,
    _bind,
    _fresh_cache,
    _proof,
    fleet,
    sts,
)

pytestmark = pytest.mark.django_db

SESSIONS_HTU = "http://testserver/api/runner-sessions/"


def _runner_token(fleet) -> str:  # noqa: F811 — fixture
    res = Client().post("/api/runner-sessions/", data={"runner": str(fleet["runner"].pk)},
                        content_type="application/json",
                        HTTP_X_CANOPY_AWS_PROOF=_proof(SESSIONS_HTU))
    assert res.status_code == 201, res.content
    return res.json()["access_token"]


def _get(path, token, **headers):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {token}", **headers)


def test_the_token_stages_an_agent_its_box_serves(fleet, sts):  # noqa: F811 — fixtures
    _bind(fleet)
    _active_role(fleet, sts)
    raw = _runner_token(fleet)

    acc = _get("/api/agents/eva/credentials/access", raw)
    assert acc.status_code == 200, acc.content
    assert acc.json()["via"] == "runner"
    assert acc.json()["credential_source"] == Agent.CANOPY_WEB
    assert _get("/api/agents/eva/runtime", raw).status_code == 200

    res = _get(RESOLVE, raw, HTTP_X_CANOPY_AWS_PROOF=_proof(HTU), HTTP_X_CANOPY_TURN_ID="t-1")
    assert res.status_code == 200, res.content
    assert res.json()["values"]["CANOPY_WEB_PAT"] == "pat-value"
    assert AgentCredentialRead.objects.get(name="CANOPY_WEB_PAT").runner_id == fleet["runner"].pk


def test_the_token_reads_nothing_its_box_does_not_serve(fleet, sts):  # noqa: F811 — fixtures
    """The owner administers Ada too, but this box does not run her."""
    _active_role(fleet, sts)
    Agent.objects.create(slug="ada", name="Ada", workspace=fleet["ws"], owner=fleet["user"],
                         runtime_secrets=["CANOPY_WEB_PAT"], credential_source=Agent.CANOPY_WEB)
    raw = _runner_token(fleet)

    acc = _get("/api/agents/ada/credentials/access", raw)
    assert acc.status_code == 200 and acc.json()["via"] is None
    assert "does not run ada" in acc.json()["reason"]
    res = _get("/api/agents/ada/credentials/resolve", raw,
               HTTP_X_CANOPY_AWS_PROOF=_proof("http://testserver/api/agents/ada/credentials/resolve"))
    assert res.status_code == 403
    # The owner's PAT is unchanged: an admin still resolves for a local session.
    assert _get("/api/agents/ada/credentials/resolve", fleet["pat"]).status_code == 200


def test_another_boxs_proof_does_not_go_with_this_token(fleet, sts):  # noqa: F811 — fixtures
    _active_role(fleet, sts)
    other = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=fleet["user"],
                                  workspace=fleet["ws"], status=Runner.ONLINE,
                                  last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=fleet["agent"], runner=other, rank=1)
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = device_keys.normalize_public_jwk(header_jwk(key))
    RunnerDeviceKey.objects.create(runner=other, kind=RunnerDeviceKey.SECURE_ENCLAVE,
                                   public_jwk=jwk, jkt=device_keys.thumbprint(jwk),
                                   status=RunnerDeviceKey.ACTIVE)
    raw = _runner_token(fleet)

    res = _get(RESOLVE, raw, HTTP_DPOP=make_dpop_proof(key, "GET", HTU))
    assert res.status_code == 403, res.content


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/agents/eva/runtime", True),
    ("GET", "/api/agents/eva/credentials/access", True),
    ("PUT", "/api/agents/eva/runtime", False),
    ("GET", "/api/agents/eva/vault", False),
    ("GET", "/api/agents/eva/credentials/status", False),
])
def test_runner_auth_adds_only_the_two_prepare_reads(method, path, ok):
    from apps.harness import runner_auth

    assert runner_auth.reaches(method, path, "11111111-2222-3333-4444-555555555555") is ok
