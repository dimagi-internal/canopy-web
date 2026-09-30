"""The member widget sends every follow-up over the chat socket, so the site's
runner requirements (ZDR) must reach the session from there too — not only
from the REST send."""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.cache import cache

from apps.agents.models import Agent
from apps.canopy_sessions import services
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import Session
from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _seed():
    cache.clear()
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    mem = User.objects.create_user("mem", "mem@dimagi.com", "pw")
    EmailAddress.objects.create(user=mem, email=mem.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=mem, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    AppCredentialAgent.objects.create(app=app, agent=agent)
    # An older conversation, started through the site before it asked for ZDR.
    session = services.create_session(workspace=ws, created_by=mem, agent=agent,
                                      metadata={"embed_app": app.name})
    raw, _ = DelegatedToken.issue(app=app, user=mem, ttl_seconds=600,
                                  assurance=DelegatedToken.ASSURANCE_HOST_SIGNED,
                                  runner_requirements=("zdr",))
    return session, raw


async def test_a_later_zdr_token_stamps_an_older_session_over_the_socket():
    session, raw = await database_sync_to_async(_seed)()
    path = f"/ws/canopy-sessions/{session.id}/"
    comm = WebsocketCommunicator(RealtimeAuthMiddleware(SessionConsumer.as_asgi()),
                                 f"{path}?token={raw}")
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
    connected, code = await comm.connect()
    assert connected, code
    await comm.send_json_to({"action": "chat.send", "data": {"text": "hello", "client_id": "c1"}})
    for _ in range(20):
        frame = await comm.receive_json_from(timeout=3)
        if frame.get("event") == "draft.committed":
            break
    await comm.disconnect()
    meta = await database_sync_to_async(lambda: Session.objects.get(pk=session.pk).metadata)()
    assert meta["runner_requirements"] == ["zdr"]
