"""The listen relay socket, `/ws/listen/<id>/?role=phone|laptop`.

Owner-only, one socket per role, binary frames passed on exactly and never kept,
limits, and the server's own `peer`/`ended` notices. Design:
docs/architecture/listen-relay.md."""
from __future__ import annotations

import base64
import json
import logging

import pytest
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey
from apps.listen import services
from apps.listen.consumers import ListenConsumer
from apps.listen.models import ListenSession
from apps.listen.routing import websocket_urlpatterns
from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

KEY = base64.urlsafe_b64encode(b"\x04" + b"\x11" * 64).rstrip(b"=").decode()
FRAME = bytes(range(256)) * 8  # every byte value, as a real ciphertext frame would carry


def _setup(name="jj"):
    user = get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")
    ws = Workspace.objects.create(slug=f"ws-{name}", display_name=name, created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name=f"{name}-mbp", kind=Runner.EMDASH, owner=user, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    session = ListenSession.objects.create(owner=user, runner=runner, kind="meeting",
                                           hello={"phone_eph": KEY, "phone_key": KEY, "label": ""})
    return user, runner, session


setup = database_sync_to_async(_setup)


def _comm(session, role, user, **scope):
    comm = WebsocketCommunicator(ListenConsumer.as_asgi(), f"/ws/listen/{session.pk}/?role={role}")
    comm.scope["user"] = user
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.pk)}}
    comm.scope.update(scope)
    return comm


async def _note(comm) -> dict:
    return json.loads(await comm.receive_from(timeout=2))


async def _both(session, user):
    phone = _comm(session, "phone", user)
    assert (await phone.connect())[0]
    assert await _note(phone) == {"type": "peer", "role": "laptop", "connected": False}
    laptop = _comm(session, "laptop", user)
    assert (await laptop.connect())[0]
    assert await _note(laptop) == {"type": "peer", "role": "phone", "connected": True}
    assert await _note(phone) == {"type": "peer", "role": "laptop", "connected": True}
    return phone, laptop


@database_sync_to_async
def _row(session) -> ListenSession:
    return ListenSession.objects.get(pk=session.pk)


# -- who may connect ------------------------------------------------------------
async def test_anonymous_is_refused():
    _user, _r, session = await setup()
    connected, code = await _comm(session, "phone", AnonymousUser()).connect()
    assert (connected, code) == (False, 4001)


async def test_only_the_owner_connects():
    _user, _r, session = await setup()
    stranger, _r2, _s2 = await setup("x")
    connected, code = await _comm(session, "phone", stranger).connect()
    assert (connected, code) == (False, 4003)


async def test_a_role_is_phone_or_laptop():
    user, _r, session = await setup()
    connected, code = await _comm(session, "server", user).connect()
    assert (connected, code) == (False, 4000)


async def test_both_roles_make_it_live_and_one_socket_per_role():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    row = await _row(session)
    assert row.state == "live" and row.connected_at is not None
    second = _comm(session, "phone", user)
    connected, _ = await second.connect()
    assert connected  # accepted, so the client can read why it closed
    assert (await second.receive_output(timeout=2))["code"] == 4009
    # The first phone still holds the role and still relays.
    await phone.send_to(bytes_data=FRAME)
    assert await laptop.receive_from(timeout=2) == FRAME
    await phone.disconnect()
    await laptop.disconnect()


# -- what gets relayed ----------------------------------------------------------
async def test_binary_is_relayed_verbatim_both_ways_and_never_kept(caplog):
    caplog.set_level(logging.DEBUG)
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    up, down = FRAME, bytes(reversed(FRAME))[:1000]
    await phone.send_to(bytes_data=up)
    assert await laptop.receive_from(timeout=2) == up
    await laptop.send_to(bytes_data=down)
    assert await phone.receive_from(timeout=2) == down
    assert await phone.receive_nothing(timeout=0.2)  # the sender hears nothing back
    await phone.disconnect()
    assert await _note(laptop) == {"type": "peer", "role": "phone", "connected": False}
    await laptop.disconnect()
    row = await _row(session)
    assert (row.bytes_up, row.bytes_down) == (len(up), len(down))  # totals only
    stored = json.dumps({f.name: str(getattr(row, f.attname)) for f in row._meta.fields})
    for needle in (up[:64].hex(), base64.b64encode(up[:48]).decode(), repr(up[:32])):
        assert needle not in stored
        assert needle not in caplog.text


async def test_text_from_a_client_is_not_relayed():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    await phone.send_to(text_data='{"type":"peer","role":"phone","connected":false}')
    assert await laptop.receive_nothing(timeout=0.2)
    await phone.disconnect()
    await laptop.disconnect()


async def test_a_frame_sent_before_the_peer_arrives_is_passed_on_when_it_does():
    user, _r, session = await setup()
    phone = _comm(session, "phone", user)
    await phone.connect()
    await _note(phone)
    await phone.send_to(bytes_data=b"\x01first")
    laptop = _comm(session, "laptop", user)
    await laptop.connect()
    assert await _note(laptop) == {"type": "peer", "role": "phone", "connected": True}
    assert await laptop.receive_from(timeout=2) == b"\x01first"
    await phone.disconnect()
    await laptop.disconnect()


# -- limits ---------------------------------------------------------------------
async def test_a_frame_over_64_kib_closes_the_socket_4008():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    await phone.send_to(bytes_data=b"\x00" * (64 * 1024 + 1))
    assert (await phone.receive_output(timeout=2))["code"] == 4008
    await phone.disconnect()  # the server's websocket.disconnect after its close
    assert await _note(laptop) == {"type": "peer", "role": "phone", "connected": False}
    assert await laptop.receive_nothing(timeout=0.2)
    await laptop.disconnect()


async def test_more_than_60_a_second_closes_the_socket_4008():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    for _ in range(61):
        await phone.send_to(bytes_data=b"\x01x")
    for _ in range(60):
        assert await laptop.receive_from(timeout=2) == b"\x01x"
    assert (await phone.receive_output(timeout=2))["code"] == 4008
    await phone.disconnect()
    await laptop.disconnect()


# -- notices and ending ---------------------------------------------------------
async def test_a_role_leaving_a_live_session_starts_the_clock_and_can_come_back():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    await laptop.disconnect()
    assert await _note(phone) == {"type": "peer", "role": "laptop", "connected": False}
    row = await _row(session)
    assert row.absent_since is not None and row.laptop_channel == ""
    again = _comm(session, "laptop", user)
    assert (await again.connect())[0]
    assert await _note(again) == {"type": "peer", "role": "phone", "connected": True}
    assert await _note(phone) == {"type": "peer", "role": "laptop", "connected": True}
    assert (await _row(session)).absent_since is None
    await phone.disconnect()
    await again.disconnect()


async def test_ending_tells_both_sides_and_closes_them():
    user, _r, session = await setup()
    phone, laptop = await _both(session, user)
    await database_sync_to_async(services.end)(session.pk, services.ENDED_BY_OWNER)
    for comm in (phone, laptop):
        assert await _note(comm) == {"type": "ended", "reason": "ended_by_owner"}
        assert (await comm.receive_output(timeout=2))["type"] == "websocket.close"


async def test_connecting_to_an_ended_session_says_so():
    user, _r, session = await setup()
    await database_sync_to_async(services.end)(session.pk, "not_connected")
    phone = _comm(session, "phone", user)
    assert (await phone.connect())[0]
    assert await _note(phone) == {"type": "ended", "reason": "not_connected"}
    assert (await phone.receive_output(timeout=2))["type"] == "websocket.close"


# -- the laptop signs in with the runner's own key --------------------------------
@database_sync_to_async
def _runner_key(runner) -> str:
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.AWS_IAM,
                                         role_arn=f"arn:aws:iam::1:role/{runner.name}",
                                         jkt=f"jkt-{runner.pk.hex[:8]}", status=RunnerDeviceKey.ACTIVE)
    return RunnerAccessToken.mint(key)[0]


def _keyed(session, role, raw):
    app = RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))
    return WebsocketCommunicator(app, f"/ws/listen/{session.pk}/?role={role}",
                                 headers=[(b"authorization", f"Bearer {raw}".encode())])


async def test_the_runners_key_opens_the_laptop_side_of_its_own_sessions_only():
    user, runner, session = await setup()
    other_runner = await database_sync_to_async(Runner.objects.create)(
        name="jj-mini", kind=Runner.EMDASH, owner=user, workspace_id=runner.workspace_id,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={})
    raw = await _runner_key(runner)
    laptop = _keyed(session, "laptop", raw)
    assert (await laptop.connect())[0]
    assert await _note(laptop) == {"type": "peer", "role": "phone", "connected": False}
    await laptop.disconnect()
    # Never the phone, and never a session on another of the owner's runners.
    assert (await _keyed(session, "phone", raw).connect()) == (False, 4003)
    elsewhere = await database_sync_to_async(ListenSession.objects.create)(
        owner=user, runner=other_runner, kind="pair", hello={"phone_eph": KEY, "phone_key": None, "label": ""})
    assert (await _keyed(elsewhere, "laptop", raw).connect()) == (False, 4003)
