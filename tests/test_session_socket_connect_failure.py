"""A chat socket whose handshake raises names the conversation and who opened it.

Raised before accept(), the failure is a handshake 500, and the ASGI server's own
traceback carries neither the session id nor the user. The consumer logs both and
re-raises, so the status is unchanged and the one broken session can be found.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator

from apps.canopy_sessions.consumers import SessionConsumer
from tests.fixtures.session_world import make_user

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_handshake_that_raises_names_the_session_and_the_user(monkeypatch, caplog):
    user = make_user("knocker")
    session_id = str(uuid.uuid4())

    async def boom(self, raw_id):
        raise RuntimeError("one session's state")

    monkeypatch.setattr(SessionConsumer, "_get_session", boom)

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session_id}/")
        comm.scope["user"] = user
        comm.scope["url_route"] = {"kwargs": {"session_id": session_id}}
        with pytest.raises(RuntimeError, match="one session's state"):
            await comm.connect()

    with caplog.at_level(logging.ERROR, logger="apps.canopy_sessions.consumers"):
        async_to_sync(go)()

    failed = [r for r in caplog.records if "session socket connect failed" in r.getMessage()]
    assert len(failed) == 1
    message = failed[0].getMessage()
    assert f"session={session_id}" in message
    assert f"user={user.pk}" in message
    assert failed[0].exc_info is not None
