"""A sensitive conversation and an ordinary one in the same workspace (`world`), and
the turn, transcript and socket helpers the sensitive-data suites share
(`tests/test_sensitive_storage.py`, `tests/test_sensitive_read_doors.py`).
"""
from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

SENSITIVE = {"runner_requirements": ["sensitive_data"]}
ROW = "patient Amina, HIV+, visit 2026-10-01"
ASKED = "how many visits did Amina have?"
REPLY = "Amina has had 12 visits"


def world() -> dict:
    """An owner, their workspace, agent and runner, a sensitive session (`hot`) and an
    ordinary one (`cold`), and a browser signed in as the owner (`c`)."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=user)
    runner = Runner.objects.create(name="box", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user)
    hot = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="hot",
                                 metadata=dict(SENSITIVE))
    cold = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="cold")
    c = Client()
    c.force_login(user)
    return {"user": user, "ws": ws, "agent": agent, "runner": runner, "hot": hot, "cold": cold,
            "c": c}


def _turn(session, **kw):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=uuid.uuid4().hex, **{"prompt": "q", **kw})


def _converse(session):
    """A turn's rows as the runner ships them (`persist_transcript_rows`)."""
    chat_transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": ASKED},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "tu1", "name": "site_call", "input": {"tool": "data_query",
                                                                   "arguments": {"q": "Amina"}}}},
        {"index": 65, "role": "tool_result", "text": ROW,
         "content": {"tool_use_id": "tu1", "is_error": False, "text": ROW}},
        {"index": 128, "role": "assistant", "text": REPLY},
    ])


def _socket(session, user, *, auth_method="session"):
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope.update(user=user, auth_method=auth_method, session={},
                          url_route={"kwargs": {"session_id": str(session.id)}})
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected
    return async_to_sync(go)()


def _unowned_thread(w):
    """A runner-discovered conversation nobody started: the agent's admins stand in
    as its owner (`access.role_for`) — they read and share it, but are not IN it."""
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], origin=Session.ORIGIN_RUNNER,
                               metadata=dict(SENSITIVE))
    RunnerBinding.objects.create(session=s, runner=w["runner"], session_key="unowned")
    return s
