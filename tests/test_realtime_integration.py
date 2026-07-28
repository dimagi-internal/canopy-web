"""SP1 polish — authenticated end-to-end through the ASSEMBLED websocket stack.

The consumer unit tests inject scope['user'] directly; the uvicorn smoke test
proves anon rejection over the wire. This closes the gap: it drives the real
RealtimeAuthMiddleware -> URLRouter -> consumer composition (the websocket branch
of config.asgi) with a genuine session cookie, exercising cookie->user auth +
routing + consumer accept + snapshot/replay/live-tail together, in-process.
"""
from __future__ import annotations

from importlib import import_module

import pytest
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.realtime.routing import websocket_urlpatterns
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _ws_app():
    # The exact websocket composition config.asgi mounts, minus the origin
    # validator (Channels' own code; the uvicorn smoke test covers it live).
    return RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))


def _make_session(user) -> str:
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY

    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session.save()
    return session.session_key


def _seed():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    return user, agent


def _cookie_header(session_key: str):
    return [(b"cookie", f"{settings.SESSION_COOKIE_NAME}={session_key}".encode())]


async def test_authenticated_supervisor_connect_and_snapshot():
    def setup():
        user, _agent = _seed()
        return _make_session(user)

    key = await database_sync_to_async(setup)()
    comm = WebsocketCommunicator(_ws_app(), "/ws/supervisor/", headers=_cookie_header(key))
    connected, _ = await comm.connect()
    assert connected is True
    snap = await comm.receive_json_from(timeout=2)
    assert snap["type"] == "supervisor.snapshot"
    assert "echo" in snap["waiting"]
    await comm.disconnect()


async def test_authenticated_turn_tail_replay_then_live():
    def setup():
        user, agent = _seed()
        turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k1")
        services.append_events(turn, [{"kind": "assistant", "payload": {"text": "old"}}])
        return turn, _make_session(user)

    turn, key = await database_sync_to_async(setup)()
    comm = WebsocketCommunicator(_ws_app(), f"/ws/turns/{turn.id}/", headers=_cookie_header(key))
    connected, _ = await comm.connect()
    assert connected is True

    replayed = await comm.receive_json_from(timeout=2)
    assert replayed["event"]["seq"] == 1
    assert replayed["event"]["payload"]["text"] == "old"

    await database_sync_to_async(services.append_events)(
        turn, [{"kind": "assistant", "payload": {"text": "new"}}]
    )
    live = await comm.receive_json_from(timeout=2)
    assert live["event"]["seq"] == 2
    assert live["event"]["payload"]["text"] == "new"
    await comm.disconnect()


async def test_anonymous_rejected_through_full_stack():
    comm = WebsocketCommunicator(_ws_app(), "/ws/supervisor/")
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4001
