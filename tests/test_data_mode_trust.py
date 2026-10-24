"""A box's data mode is believed only from a cloud runner signed in as itself.

`sensitive_data` is in force when a person declares it AND the box reports it
(`runner_requirements.in_force`). The declaring person is the box's own admin, so a
report sent with that admin's token — or by a laptop, whose code its owner runs and
can change — would let one hand make both halves. The report counts only from a
CLOUD runner's beat signed in with its device key (`runner_protocol.data_mode_trusted`).
"""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.harness import runner_protocol
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey, RunnerFlag
from apps.harness.schemas import HeartbeatIn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BEAT = {"active_turn_ids": [], "data_mode": "sensitive_data", "data_mode_hash": "abc123"}


@pytest.fixture
def world():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)

    def runner(name, kind):
        box = Runner.objects.create(name=name, kind=kind, owner=jj, workspace=ws,
                                    status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                    capabilities={})
        RunnerFlag.objects.create(runner=box, flag=rr.SENSITIVE_DATA, declared_by=jj)
        return box

    return {"jj": jj, "cloud": runner("cloud-zdr-1", Runner.CLOUD),
            "laptop": runner("jj-laptop", Runner.EMDASH)}


def _key_token(runner) -> str:
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=f"jkt-{runner.pk}", status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return raw


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _beat(runner, raw):
    res = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").post(
        f"/api/harness/runners/{runner.pk}/heartbeat", data=BEAT, content_type="application/json")
    assert res.status_code == 200, res.content
    runner.refresh_from_db()
    caps = runner.capabilities
    return caps.get(rr.DATA_MODE_KEY) or "", caps.get(rr.DATA_MODE_HASH_KEY) or ""


def test_a_cloud_box_signed_in_with_its_device_key_is_believed(world):
    assert _beat(world["cloud"], _key_token(world["cloud"])) == ("sensitive_data", "abc123")
    assert rr.runner_satisfies(world["cloud"], frozenset({rr.SENSITIVE_DATA}))


def test_the_same_report_with_its_admins_token_is_not(world):
    assert _beat(world["cloud"], _pat(world["jj"])) == ("", "")
    assert not rr.runner_satisfies(world["cloud"], frozenset({rr.SENSITIVE_DATA}))


def test_a_cloud_box_that_drops_to_a_persons_token_stops_counting(world):
    cloud = world["cloud"]
    _beat(cloud, _key_token(cloud))
    assert _beat(cloud, _pat(world["jj"])) == ("", "")


def test_a_laptop_is_never_believed_even_with_its_own_key(world):
    assert _beat(world["laptop"], _key_token(world["laptop"])) == ("", "")
    assert _beat(world["laptop"], _pat(world["jj"])) == ("", "")
    assert not rr.runner_satisfies(world["laptop"], frozenset({rr.SENSITIVE_DATA}))


def test_the_rule(world):
    assert runner_protocol.data_mode_trusted(world["cloud"], device_key=True)
    assert not runner_protocol.data_mode_trusted(world["cloud"], device_key=False)
    assert not runner_protocol.data_mode_trusted(world["laptop"], device_key=True)
    # Called directly with no say on how it signed in: not believed.
    runner_protocol.heartbeat(world["cloud"], HeartbeatIn(**BEAT))
    assert not world["cloud"].capabilities.get(rr.DATA_MODE_KEY)


def _socket(runner, raw):
    from apps.realtime.channels_auth import RealtimeAuthMiddleware
    from apps.realtime.routing import websocket_urlpatterns

    app = RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))
    return WebsocketCommunicator(app, f"/ws/runner/{runner.pk}/",
                                 headers=[(b"authorization", f"Bearer {raw}".encode())])


async def _socket_beat(runner, raw):
    comm = _socket(runner, raw)
    connected, _ = await comm.connect()
    assert connected
    await comm.send_json_to({"action": "heartbeat", **BEAT})
    assert (await comm.receive_json_from(timeout=2))["type"] == "heartbeat.ack"
    await comm.disconnect()
    await database_sync_to_async(runner.refresh_from_db)()
    return runner.capabilities.get(rr.DATA_MODE_KEY) or ""


@pytest.mark.django_db(transaction=True)
async def test_the_control_channel_follows_the_same_rule(world):
    cloud = world["cloud"]
    assert await _socket_beat(cloud, await database_sync_to_async(_key_token)(cloud)) == "sensitive_data"
    assert await _socket_beat(cloud, await database_sync_to_async(_pat)(world["jj"])) == ""
