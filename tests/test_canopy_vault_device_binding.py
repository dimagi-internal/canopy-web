"""Canopy Vault Phase 1a (canopy-web#1538): runner device keys, the DPoP device
proof on `/credentials/resolve`, and the credential read log.

The contract these pin is the one canopy's CLI/runner half builds against
(canopy#946) — endpoint shapes here are not free to drift.
"""
from __future__ import annotations

import time

import pytest
from canopy_sdk.jose import make_dpop_proof
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredentialRead
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

RESOLVE = "/api/agents/eva/credentials/resolve"
HTU = f"http://testserver{RESOLVE}"


def _jwk(key) -> dict:
    from canopy_sdk.keys import header_jwk

    return header_jwk(key)


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return raw


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj,
                                 runtime_secrets=["canopy-pat"],
                                 credential_source=Agent.CANOPY_WEB)
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE, workspace=ws,
                                   last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    client.force_login(jj)
    # Browser credential writes and device-key approvals need a passkey step-up
    # (apps/passkeys); tests/test_canopy_vault_device_binding.py's own step-up
    # cases below expire or omit it on purpose.
    grant_step_up(client)
    client.put("/api/agents/eva/credentials", data={"values": {"canopy-pat": "pat-value"}},
               content_type="application/json")
    key = ec.generate_private_key(ec.SECP256R1())
    return {"client": client, "agent": agent, "runner": runner, "user": jj, "ws": ws,
            "pat": _pat(jj), "key": key}


def _register(fleet, key=None, runner=None, kind="secure-enclave"):
    return Client().post(
        "/api/runner-device-keys/",
        data={"runner": str((runner or fleet["runner"]).pk), "public_jwk": _jwk(key or fleet["key"]),
              "kind": kind, "label": "jj-mbp"},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")


def _approve(fleet, key_id):
    return fleet["client"].post(f"/api/runner-device-keys/{key_id}/approve")


def _active_key(fleet, key=None):
    body = _register(fleet, key).json()
    assert _approve(fleet, body["id"]).status_code == 200
    return body


def _resolve(fleet, proof=None, path=RESOLVE):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {fleet['pat']}"}
    if proof:
        headers["HTTP_DPOP"] = proof
    return Client().get(path, **headers)


def _bind(fleet, on=True):
    Agent.objects.filter(pk=fleet["agent"].pk).update(require_device_binding=on)


# ---- registration ----------------------------------------------------------------

def test_register_returns_the_contract_shape_and_is_idempotent(fleet):
    res = _register(fleet)
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "jkt", "status", "approve_url", "ecdh_jkt"}
    assert body["status"] == "pending"
    assert body["approve_url"].endswith(f"/settings?device_key={body['id']}#device-keys")
    from canopy_sdk.contract import jwk_thumbprint

    assert body["jkt"] == jwk_thumbprint(_jwk(fleet["key"]))  # RFC 7638

    again = _register(fleet)
    assert again.status_code == 200 and again.json()["id"] == body["id"]
    assert RunnerDeviceKey.objects.count() == 1


def test_register_rejects_a_private_or_non_p256_key(fleet):
    jwk = _jwk(fleet["key"]) | {"d": "c2VjcmV0"}
    res = Client().post("/api/runner-device-keys/",
                        data={"runner": str(fleet["runner"].pk), "public_jwk": jwk, "kind": "tpm"},
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")
    assert res.status_code == 422
    res = Client().post("/api/runner-device-keys/",
                        data={"runner": str(fleet["runner"].pk),
                              "public_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "abc"},
                              "kind": "software"},
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")
    assert res.status_code == 422


def test_someone_else_cannot_register_a_key_on_your_runner(fleet):
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    res = Client().post("/api/runner-device-keys/",
                        data={"runner": str(fleet["runner"].pk), "public_jwk": _jwk(fleet["key"]),
                              "kind": "software"},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_pat(other)}")
    assert res.status_code == 404


def test_the_same_key_cannot_belong_to_two_runners(fleet):
    _register(fleet)
    second = Runner.objects.create(name="box2", kind=Runner.CLOUD, owner=fleet["user"],
                                   status=Runner.ONLINE, capabilities={})
    assert _register(fleet, runner=second).status_code == 409


def test_list_by_runner_shows_status(fleet):
    body = _register(fleet).json()
    rows = Client().get(f"/api/runner-device-keys/?runner={fleet['runner'].pk}",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}").json()
    assert [(r["id"], r["status"], r["kind"]) for r in rows] == [(body["id"], "pending", "secure-enclave")]


# ---- approval and revocation --------------------------------------------------------

def test_approve_needs_a_browser_session_not_a_token(fleet):
    body = _register(fleet).json()
    res = Client().post(f"/api/runner-device-keys/{body['id']}/approve",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")
    assert res.status_code == 403
    assert RunnerDeviceKey.objects.get().status == "pending"


def test_approve_without_a_fresh_passkey_step_up_is_refused(fleet):
    """A signed-in browser session alone is not enough (canopy-web#1538)."""
    body = _register(fleet).json()
    grant_step_up(fleet["client"], seconds=-1)  # expired
    res = _approve(fleet, body["id"])
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    assert RunnerDeviceKey.objects.get().status == "pending"
    grant_step_up(fleet["client"])
    assert _approve(fleet, body["id"]).json()["status"] == "active"


def test_owner_approves_from_the_browser(fleet):
    body = _register(fleet).json()
    res = _approve(fleet, body["id"])
    assert res.status_code == 200
    assert res.json()["status"] == "active" and res.json()["approved_by_email"] == "jj@dimagi.com"


def test_a_runner_admin_may_approve_but_a_stranger_may_not(fleet):
    body = _register(fleet).json()
    stranger = get_user_model().objects.create_user(username="s", email="s@dimagi.com")
    c = Client()
    c.force_login(stranger)
    grant_step_up(c)
    assert c.post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 404
    RunnerAdmin.objects.create(runner=fleet["runner"], user=stranger)
    assert c.post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 200


def test_revoke_is_immediate_and_a_revoked_key_cannot_be_revived(fleet):
    body = _active_key(fleet)
    res = Client().delete(f"/api/runner-device-keys/{body['id']}",
                          HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")
    assert res.status_code == 200 and res.json()["status"] == "revoked"
    assert _approve(fleet, body["id"]).status_code == 409
    assert _register(fleet).json()["status"] == "revoked"  # re-register does not resurrect


# ---- the device proof on resolve -----------------------------------------------------

def test_without_binding_resolve_needs_no_proof(fleet):
    assert _resolve(fleet).status_code == 200


def test_binding_on_and_no_proof_is_the_contract_401(fleet):
    _bind(fleet)
    res = _resolve(fleet)
    assert res.status_code == 401
    assert res.json() == {"error": "device_proof_required", "reason": "no_valid_proof",
                          "detail": "no device proof"}
    assert "pat-value" not in res.content.decode()


def test_binding_only_applies_to_canopy_web_agents(fleet):
    _bind(fleet)
    Agent.objects.filter(pk=fleet["agent"].pk).update(credential_source=Agent.ONEPASSWORD)
    assert _resolve(fleet).status_code == 200


def test_a_valid_proof_from_an_active_key_resolves_and_names_the_runner(fleet):
    _bind(fleet)
    _active_key(fleet)
    res = _resolve(fleet, make_dpop_proof(fleet["key"], "GET", HTU))
    assert res.status_code == 200 and res.json()["values"]["canopy-pat"] == "pat-value"
    read = AgentCredentialRead.objects.get(name="canopy-pat")
    assert read.runner_id == fleet["runner"].pk
    assert RunnerDeviceKey.objects.get().last_used_at is not None


def test_a_pending_or_revoked_key_does_not_count(fleet):
    _bind(fleet)
    body = _register(fleet).json()
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", HTU)).status_code == 401
    _approve(fleet, body["id"])
    Client().delete(f"/api/runner-device-keys/{body['id']}",
                    HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}")
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", HTU)).status_code == 401


def test_a_replayed_proof_is_refused(fleet):
    _bind(fleet)
    _active_key(fleet)
    proof = make_dpop_proof(fleet["key"], "GET", HTU)
    assert _resolve(fleet, proof).status_code == 200
    assert _resolve(fleet, proof).status_code == 401


def test_a_stale_proof_is_refused_and_120s_skew_is_allowed(fleet):
    _bind(fleet)
    _active_key(fleet)
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", HTU,
                                           now=time.time() - 200)).status_code == 401
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", HTU,
                                           now=time.time() - 100)).status_code == 200


@pytest.mark.parametrize("htm,htu", [
    ("POST", HTU),
    ("GET", "http://testserver/api/agents/other/credentials/resolve"),
    ("GET", HTU + "?refs=a"),
    ("GET", "http://evil.example/api/agents/eva/credentials/resolve"),
])
def test_a_proof_for_another_request_is_refused(fleet, htm, htu):
    _bind(fleet)
    _active_key(fleet)
    assert _resolve(fleet, make_dpop_proof(fleet["key"], htm, htu)).status_code == 401


def test_htu_tolerates_the_old_address_prefix_and_the_tenant_url(fleet):
    _bind(fleet)
    _active_key(fleet)
    legacy = "http://testserver/canopy/api/agents/eva/credentials/resolve"
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", legacy)).status_code == 200
    tenant = "http://testserver/api/w/connect/agents/eva/credentials/resolve"
    assert _resolve(fleet, make_dpop_proof(fleet["key"], "GET", tenant),
                    path="/api/w/connect/agents/eva/credentials/resolve").status_code == 200


def test_an_es256_proof_from_an_unregistered_key_is_refused(fleet):
    _bind(fleet)
    _active_key(fleet)
    rogue = ec.generate_private_key(ec.SECP256R1())
    assert _resolve(fleet, make_dpop_proof(rogue, "GET", HTU)).status_code == 401


def test_an_eddsa_proof_is_refused(fleet):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    _bind(fleet)
    _active_key(fleet)
    assert _resolve(fleet, make_dpop_proof(ed25519.Ed25519PrivateKey.generate(), "GET",
                                           HTU)).status_code == 401


def test_a_key_on_a_runner_the_agent_does_not_use_is_refused(fleet):
    """Authorized runner = one that runs the agent (`routing.runner_routes_agent`).
    An approved box that does not is refused as `not_routed`, with no owner alert:
    routing said no, not the key (fizzy, canopy-web#1563)."""
    from apps.agents.models import AgentCredentialAlert

    _bind(fleet)
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=other,
                                       role=WorkspaceMembership.OWNER)
    theirs = Runner.objects.create(name="theirs", kind=Runner.CLOUD, owner=other,
                                   status=Runner.ONLINE, capabilities={}, workspace=fleet["ws"])
    key = ec.generate_private_key(ec.SECP256R1())
    c = Client()
    c.force_login(other)
    grant_step_up(c)
    body = Client().post("/api/runner-device-keys/",
                         data={"runner": str(theirs.pk), "public_jwk": _jwk(key), "kind": "software"},
                         content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {_pat(other)}").json()
    assert c.post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 200
    res = _resolve(fleet, make_dpop_proof(key, "GET", HTU))
    assert res.status_code == 401
    assert res.json()["reason"] == "not_routed" and "theirs does not run eva" in res.json()["detail"]
    assert not AgentCredentialAlert.objects.filter(agent=fleet["agent"], kind="device-proof").exists()
    # Route the agent to it and the same key is accepted.
    RunnerAssignment.objects.create(agent=fleet["agent"], runner=theirs, rank=1)
    assert _resolve(fleet, make_dpop_proof(key, "GET", HTU)).status_code == 200


def test_the_owners_own_laptop_counts_without_a_route(fleet):
    _bind(fleet)
    laptop = Runner.objects.create(name="jj-laptop", kind=Runner.EMDASH, owner=fleet["user"],
                                   status=Runner.ONLINE, capabilities={})
    key = ec.generate_private_key(ec.SECP256R1())
    body = _register(fleet, key=key, runner=laptop).json()
    _approve(fleet, body["id"])
    assert _resolve(fleet, make_dpop_proof(key, "GET", HTU)).status_code == 200


def _laptop(fleet, owner):
    laptop = Runner.objects.create(name=f"{owner.username}-laptop", kind=Runner.EMDASH,
                                   owner=owner, status=Runner.ONLINE, capabilities={},
                                   workspace=fleet["ws"])
    key = ec.generate_private_key(ec.SECP256R1())
    return laptop, key


def test_the_owner_opening_a_session_on_their_own_laptop_raises_no_alert(fleet):
    """2026-10-18: a ZK agent always needs a device proof, so every emdash session
    the owner opened named the laptop and read as "a box outside a turn" — an email
    each time. Their own token from their own approved laptop is them."""
    from apps.agents.models import AgentCredentialAlert

    _bind(fleet)
    laptop, key = _laptop(fleet, fleet["user"])
    _approve(fleet, _register(fleet, key=key, runner=laptop).json()["id"])
    assert _resolve(fleet, make_dpop_proof(key, "GET", HTU)).status_code == 200
    assert not AgentCredentialAlert.objects.filter(kind="no-turn").exists()
    read = AgentCredentialRead.objects.filter(agent=fleet["agent"]).latest("pk")
    assert read.via == "admin" and read.runner_id == laptop.pk  # still names the laptop


def test_someone_elses_laptop_reading_outside_a_turn_still_alerts(fleet):
    from apps.agents.models import AgentCredentialAlert

    _bind(fleet)
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=other,
                                       role=WorkspaceMembership.OWNER)
    laptop, key = _laptop(fleet, other)
    RunnerAssignment.objects.create(agent=fleet["agent"], runner=laptop, rank=1)
    c = Client()
    c.force_login(other)
    grant_step_up(c)
    body = Client().post("/api/runner-device-keys/",
                         data={"runner": str(laptop.pk), "public_jwk": _jwk(key), "kind": "software"},
                         content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {_pat(other)}").json()
    assert c.post(f"/api/runner-device-keys/{body['id']}/approve").status_code == 200
    # The OWNER's token, but the proof is a laptop the owner does not own.
    assert _resolve(fleet, make_dpop_proof(key, "GET", HTU)).status_code == 200
    assert AgentCredentialAlert.objects.filter(kind="no-turn").count() == 1


# ---- the agent setting, read log and agent views --------------------------------------

def test_device_binding_is_admin_settable_and_shown(fleet):
    res = fleet["client"].patch("/api/agents/eva/device-binding",
                                data={"require_device_binding": True},
                                content_type="application/json")
    assert res.status_code == 200 and res.json()["require_device_binding"] is True
    viewer = get_user_model().objects.create_user(username="v", email="v@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=viewer,
                                       role=WorkspaceMembership.VIEWER)
    c = Client()
    c.force_login(viewer)
    assert c.patch("/api/agents/eva/device-binding", data={"require_device_binding": False},
                   content_type="application/json").status_code in (403, 404)


def test_read_history_and_last_read_on_status(fleet):
    _resolve(fleet)
    Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}", HTTP_X_CANOPY_TURN_ID="t-9")
    reads = fleet["client"].get("/api/agents/eva/credentials/canopy-pat/reads").json()
    assert len(reads) == 2 and reads[0]["turn_id"] == "t-9"
    assert reads[0]["user_email"] == "jj@dimagi.com"
    row = next(r for r in fleet["client"].get("/api/agents/eva/credentials/status").json()
               if r["name"] == "canopy-pat")
    assert row["read_count"] == 2 and row["last_read_at"]


def test_agent_device_keys_lists_keys_on_its_runners(fleet):
    body = _register(fleet).json()
    rows = fleet["client"].get("/api/agents/eva/device-keys").json()
    assert [(r["id"], r["status"], r["can_manage"]) for r in rows] == [(body["id"], "pending", True)]
