"""What one read of a workspace vault costs as the fleet grows.

Shared (dimagi) is attached to a whole workspace and held by dozens of machines; its
page took 19-45 s on labs (2026-11-06) with no new code on the path. Each machine
holding a copy re-read the vault's members, its runners and every agent it serves, and
asked "does this box serve this agent" with a fresh read of the fleet each time. These
pin the read's query count to the vault, not to machines x agents.
"""
from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent, Vault, VaultKeyWrap
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from cryptography.hazmat.primitives.asymmetric import ec
from tests.fixtures.vault_zk import ref
from tests import test_canopy_vault_zk as _zk
from tests.test_canopy_vault_zk import _ws_enabled

fleet = _zk.fleet   # the fixture, shared with the vault suite

pytestmark = pytest.mark.django_db


def _grow(fleet, vault: Vault, *, machines: int, agents: int, tag: str, wrapped: bool = True) -> None:
    """`agents` more agents in the vault's workspace, and `machines` more boxes running
    eva that each hold a copy of the vault's key."""
    for i in range(agents):
        Agent.objects.create(slug=f"a-{tag}-{i}", name=f"A{i}", workspace=fleet.ws, owner=fleet.user)
    now = timezone.now()
    for i in range(machines):
        box = Runner.objects.create(name=f"box-{tag}-{i}", kind=Runner.CLOUD, owner=fleet.user,
                                    status=Runner.ONLINE, workspace=fleet.ws, last_heartbeat_at=now, capabilities={})
        RunnerAssignment.objects.create(agent=fleet.agent, runner=box, rank=0)
        jwk = ref.jwk(ec.generate_private_key(ec.SECP256R1()).public_key())
        key = RunnerDeviceKey.objects.create(
            runner=box, jkt=uuid.uuid4().hex, kind="secure-enclave", status=RunnerDeviceKey.ACTIVE,
            approved_by=fleet.user, approved_at=now, ecdh_jkt=ref.jwk_thumbprint(jwk) if hasattr(ref, "jwk_thumbprint")
            else uuid.uuid4().hex, ecdh_public_jwk=jwk, ecdh_approved_at=now, ecdh_approved_by=fleet.user)
        if wrapped:
            VaultKeyWrap.objects.create(vault=vault, kv=vault.kv, kind="device", device_key=key, wrap={"x": 1})


def _queries(fleet, path: str) -> int:
    with CaptureQueriesContext(connection) as q:
        res = fleet.client.get(path)
    assert res.status_code == 200, res.content
    return len(q.captured_queries)


def _shared(fleet) -> Vault:
    vid, _, _ = _ws_enabled(fleet, {"gog-oauth-client-web": "x"})
    assert fleet.client.post(f"/api/vaults/{vid}/attachments", data={"workspace": "connect"},
                             content_type="application/json").status_code == 200
    return Vault.objects.get(pk=vid)


@pytest.mark.parametrize("path", ["/api/vaults/{vid}", "/api/workspaces/connect/vaults", "/api/vaults/{vid}/zk"])
def test_a_vault_read_does_not_grow_with_machines_and_agents(fleet, path):
    vault = _shared(fleet)
    url = path.format(vid=vault.pk)
    _grow(fleet, vault, machines=2, agents=2, tag="s")
    small = _queries(fleet, url)
    _grow(fleet, vault, machines=6, agents=6, tag="l")
    large = _queries(fleet, url)
    print(f"{path}: {small} -> {large} queries")
    assert large - small <= 6, f"{path}: {small} queries at 3 boxes/3 agents, {large} at 9/9"


def test_a_keepers_work_does_not_grow_with_machines_and_agents(fleet):
    # The keeper's read (`/vault-keeper`) timed out on labs: for a workspace vault it lists
    # every served agent's endorsements, each with a fresh read of the fleet.
    vault = _shared(fleet)
    url = f"/api/harness/runners/{fleet.runner.pk}/vault-keeper"
    _grow(fleet, vault, machines=2, agents=2, tag="s", wrapped=False)
    small = _queries(fleet, url)
    _grow(fleet, vault, machines=6, agents=6, tag="l", wrapped=False)
    large = _queries(fleet, url)
    print(f"keeper: {small} -> {large} queries")
    assert large - small <= 6, f"keeper work: {small} queries at 3 boxes/3 agents, {large} at 9/9"
