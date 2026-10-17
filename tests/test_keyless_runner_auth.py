"""Keyless runners: a box's approved device key buys its API credential.

On 2026-10-17 cloud-ec2-2 ran on its owner's PAT; the PAT was revoked in a token
cleanup and the box silently lost every turn it claimed. A runner now proves its
device key (an EC2 instance role, or a key it holds) at `POST /api/runner-sessions/`
and gets an hour-long token that opens the runner protocol only.
"""
from __future__ import annotations

import pytest
from canopy_sdk.jose import make_dpop_proof
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_canopy_vault_phase1b import ROLE, _proof, sts  # noqa: F401 — sts is a fixture

# The fake STS answers every instance-role proof (tests/test_canopy_vault_phase1b.py).
pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("sts")]

SESSIONS = "/api/runner-sessions/"
HTU = f"http://testserver{SESSIONS}"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def box():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={})
    other = Runner.objects.create(name="someone-else", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                  capabilities={})
    aws_key = RunnerDeviceKey.objects.create(
        runner=runner, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
        jkt=aws_iam_proof.role_jkt(ROLE), status=RunnerDeviceKey.ACTIVE)
    return {"user": jj, "runner": runner, "other": other, "aws_key": aws_key}


def _exchange(runner, **headers):
    return Client().post(SESSIONS, data={"runner": str(runner.pk)},
                         content_type="application/json", **headers)


def _token(box) -> str:
    res = _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU))
    assert res.status_code == 201, res.content
    return res.json()["access_token"]


# ---- the exchange ---------------------------------------------------------------------

def test_an_instance_role_proof_buys_an_hour_long_runner_token(box):
    res = _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU))
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["access_token"].startswith(RunnerAccessToken.PREFIX)
    assert body["runner"] == str(box["runner"].pk)
    tok = RunnerAccessToken.lookup(body["access_token"])
    assert tok.runner == box["runner"] and tok.device_key == box["aws_key"]
    assert (tok.expires_at - timezone.now()).total_seconds() == pytest.approx(3600, abs=60)


def test_a_dpop_proof_from_a_held_key_buys_one_too(box):
    key = ec.generate_private_key(ec.SECP256R1())
    from canopy_sdk.keys import header_jwk

    jwk = device_keys.normalize_public_jwk(header_jwk(key))
    RunnerDeviceKey.objects.create(runner=box["runner"], kind=RunnerDeviceKey.SECURE_ENCLAVE,
                                   public_jwk=jwk, jkt=device_keys.thumbprint(jwk),
                                   status=RunnerDeviceKey.ACTIVE)
    res = _exchange(box["runner"], HTTP_DPOP=make_dpop_proof(key, "POST", HTU))
    assert res.status_code == 201, res.content


@pytest.mark.parametrize("status", [RunnerDeviceKey.PENDING, RunnerDeviceKey.REVOKED])
def test_only_an_approved_key_counts(box, status):
    RunnerDeviceKey.objects.filter(pk=box["aws_key"].pk).update(status=status)
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU)).status_code == 401


def test_no_proof_a_replayed_proof_or_another_runner_is_refused(box):
    assert _exchange(box["runner"]).status_code == 401
    proof = _proof(HTU)
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=proof).status_code == 201
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=proof).status_code == 401
    # The box's key proves the box, not some other runner its owner also has.
    assert _exchange(box["other"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU)).status_code == 401
    # A proof made for a different endpoint is no good here.
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(
        "http://testserver/api/agents/eva/credentials/resolve")).status_code == 401


def test_a_retired_runner_or_an_inactive_owner_gets_nothing(box):
    Runner.objects.filter(pk=box["runner"].pk).update(status=Runner.RETIRED)
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU)).status_code == 401
    Runner.objects.filter(pk=box["runner"].pk).update(status=Runner.ONLINE)
    get_user_model().objects.filter(pk=box["user"].pk).update(is_active=False)
    assert _exchange(box["runner"], HTTP_X_CANOPY_AWS_PROOF=_proof(HTU)).status_code == 401


# ---- what the token opens -------------------------------------------------------------

def test_the_token_runs_the_runner_protocol_as_the_owner(box):
    raw = _token(box)
    res = Client().post(f"/api/harness/runners/{box['runner'].pk}/heartbeat",
                        data={}, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 200, res.content


@pytest.mark.parametrize("method,path", [
    ("get", "/api/tokens/"),                   # the owner's PATs
    ("get", "/api/me"),                         # anything personal
    ("post", "/api/harness/runners/{other}/heartbeat"),  # another runner of the same owner
    ("post", "/api/agents/eva/credentials"),   # a write outside the protocol
])
def test_the_token_opens_nothing_else(box, method, path):
    raw = _token(box)
    res = getattr(Client(), method)(path.format(other=box["other"].pk), data={},
                                    content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 403, (path, res.status_code)


def test_revoking_the_device_key_kills_the_token_at_once(box):
    raw = _token(box)
    RunnerDeviceKey.objects.filter(pk=box["aws_key"].pk).update(
        status=RunnerDeviceKey.REVOKED, revoked_at=timezone.now())
    res = Client().post(f"/api/harness/runners/{box['runner'].pk}/heartbeat",
                        data={}, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 401


def test_an_expired_token_is_refused(box):
    raw = _token(box)
    RunnerAccessToken.objects.update(expires_at=timezone.now())
    assert RunnerAccessToken.lookup(raw) is None


# ---- the control channel --------------------------------------------------------------

def _socket(path, raw):
    from apps.realtime.channels_auth import RealtimeAuthMiddleware
    from apps.realtime.routing import websocket_urlpatterns

    app = RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))
    return WebsocketCommunicator(app, path, headers=[(b"authorization", f"Bearer {raw}".encode())])


@pytest.mark.django_db(transaction=True)
async def test_the_token_opens_its_own_runner_socket_and_closes_when_the_key_is_revoked(box):
    raw = await database_sync_to_async(_token)(box)

    other = _socket(f"/ws/runner/{box['other'].pk}/", raw)
    connected, _ = await other.connect()
    assert connected is False

    comm = _socket(f"/ws/runner/{box['runner'].pk}/", raw)
    connected, _ = await comm.connect()
    assert connected is True
    await comm.send_json_to({"action": "claim"})
    assert (await comm.receive_json_from(timeout=2))["type"] == "claim.result"

    await database_sync_to_async(RunnerDeviceKey.objects.filter(pk=box["aws_key"].pk).update)(
        status=RunnerDeviceKey.REVOKED)
    await comm.send_json_to({"action": "claim"})
    assert await comm.receive_output(timeout=2) == {"type": "websocket.close", "code": 4001}


@pytest.mark.parametrize("method,path,ok", [
    # Every route the laptop runner calls (runner/canopy_runner/client.py) …
    ("GET", "/api/harness/runners/", True),
    ("GET", "/api/harness/schedules/", True),
    ("POST", "/api/harness/schedules/abc/fire", True),
    ("GET", "/api/inbound/runner-mailboxes", True),
    ("POST", "/api/inbound/watch/", True),
    ("PUT", "/api/harness/runners/{rid}/engine", True),
    ("POST", "/api/w/connect/harness/turns/x/finish", True),   # tenant URL, flattened
    ("POST", "/canopy/api/harness/runners/{rid}/heartbeat", True),  # the old address
    # … and nothing beyond the protocol.
    ("POST", "/api/harness/schedules/", False),
    ("DELETE", "/api/harness/schedules/abc", False),
    ("POST", "/api/inbound/runner-mailboxes", False),
    ("GET", "/api/sessions/", False),
])
def test_runner_auth_covers_the_laptop_runners_calls_and_no_more(method, path, ok):
    from apps.harness import runner_auth

    rid = "11111111-2222-3333-4444-555555555555"
    assert runner_auth.reaches(method, path.format(rid=rid), rid) is ok
