"""REST and the WebSocket must show the SAME transcript.

The panel reads the `session.state` WS snapshot; `getSession` serves the REST
detail. When the binding-tail fallback was added to REST only, `GET` returned 8
rows while the panel rendered "Start the conversation" — a silent divergence
that every unit test passed through, because each transport was tested against
itself.

Both now go through `services.visible_transcript`. These tests are the guard:
they compare the two transports' OUTPUT for the same session, so re-introducing
a transport-specific code path fails here rather than on prod.
"""
import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import Message, RunnerBinding
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _seed(*, with_tail: bool, with_rows: bool):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)
    session = chat.create_session(workspace=ws, created_by=owner, agent=agent)
    if with_rows:
        for i, (role, text) in enumerate([(Message.USER, "real-q"), (Message.ASSISTANT, "real-a")]):
            Message.objects.create(session=session, turn_index=i, role=role, plaintext=text)
    if with_tail:
        r = Runner.objects.create(
            name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=owner,
            host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        )
        RunnerBinding.objects.create(
            session=session, runner=r, session_key="ace-demo", thread_key="emdash:ace-demo",
            host=r.host, last_interacted_at=timezone.now(),
            tail=[{"role": "user", "text": "tail-q"}, {"role": "assistant", "text": "tail-a"}],
        )
    return owner, session


async def _ws_messages(session, user):
    comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
    comm.scope["user"] = user
    connected, _ = await comm.connect()
    assert connected is True
    for _ in range(14):
        frame = await comm.receive_json_from(timeout=5)
        if frame.get("event") == "session.state":
            await comm.disconnect()
            return frame["data"]["messages"]
    await comm.disconnect()
    raise AssertionError("no session.state frame")


def _rest_messages(owner, session):
    c = Client(); c.force_login(owner)
    return c.get(f"/api/canopy-sessions/{session.id}").json()["messages"]


def _key(rows):
    return [(m["turn_index"], m["plaintext"]) for m in rows]


@pytest.mark.asyncio
async def test_parity_when_only_the_binding_tail_exists():
    """The exact prod divergence: REST served the tail, the WS snapshot did not."""
    owner, session = await database_sync_to_async(_seed)(with_tail=True, with_rows=False)
    ws_rows = await _ws_messages(session, owner)
    rest_rows = await database_sync_to_async(_rest_messages)(owner, session)
    assert _key(ws_rows) == _key(rest_rows) == [(-2, "tail-q"), (-1, "tail-a")]


@pytest.mark.asyncio
async def test_parity_when_real_rows_exist():
    """Real rows win on BOTH transports — the tail must not leak in alongside."""
    owner, session = await database_sync_to_async(_seed)(with_tail=True, with_rows=True)
    ws_rows = await _ws_messages(session, owner)
    rest_rows = await database_sync_to_async(_rest_messages)(owner, session)
    assert _key(ws_rows) == _key(rest_rows) == [(0, "real-q"), (1, "real-a")]


@pytest.mark.asyncio
async def test_parity_for_a_plain_web_session():
    owner, session = await database_sync_to_async(_seed)(with_tail=False, with_rows=True)
    ws_rows = await _ws_messages(session, owner)
    rest_rows = await database_sync_to_async(_rest_messages)(owner, session)
    assert _key(ws_rows) == _key(rest_rows) == [(0, "real-q"), (1, "real-a")]
