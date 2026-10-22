"""SP2a Task 6 — the capstone: one ledger append fans out to BOTH SP1's live
turn socket AND the SP2 Message projection.

This is the whole point of "unify on the ledger": chat streaming and the durable
transcript are two consumers of the same TurnEvent append, not two engines.
"""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions.executor import execute_turn_stub
from apps.realtime.consumers import TurnConsumer
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _seed_and_send():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    session = chat.create_session(workspace=ws, created_by=user, agent=agent)
    _msg, turn = session_send.send_message(session=session, text="hi", user=user)  # enqueue only
    return user, session, turn


async def test_chat_turn_streams_live_and_projects_from_one_append():
    user, session, turn = await database_sync_to_async(_seed_and_send)()

    comm = WebsocketCommunicator(TurnConsumer.as_asgi(), f"/ws/turns/{turn.id}/")
    comm.scope["user"] = user
    comm.scope["url_route"] = {"kwargs": {"turn_id": str(turn.id)}}
    connected, _ = await comm.connect()
    assert connected is True

    # Execute (the SP2b cloud runner's job) — appends status/assistant/status.
    await database_sync_to_async(execute_turn_stub)(turn, reply="hello live")

    # 1) SP1: the assistant event streams live over the turn socket.
    got_assistant = False
    for _ in range(8):
        frame = await comm.receive_json_from(timeout=2)
        if frame["event"]["kind"] == "assistant":
            assert frame["event"]["payload"]["text"] == "hello live"
            got_assistant = True
            break
    assert got_assistant, "assistant event did not stream live"
    await comm.disconnect()

    # 2) SP2: the same append projected into the durable transcript.
    roles = await database_sync_to_async(
        lambda: [m.role for m in session.messages.order_by("turn_index")]
    )()
    assert roles == ["user", "assistant"]
