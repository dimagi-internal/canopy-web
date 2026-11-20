"""The brain's project-leg world: a session two people agreed to project learning for, tied to a project a
third person (the reader) may read, carrying a secret in its title and words. Shared by the leg's tests."""
from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import Client

from apps.agents.models import Agent, AgentProject
from apps.brain import associate
from apps.brain.models import LearningConsent
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import Message, Session
from tests.fixtures.brain_world import brain_world, member, user

SECRET = "ghp_" + "0123456789abcdefABCDEF" + "0123456789abcd"          # joined, so the secret scan sees no token


def project_leg_world():
    out = brain_world()
    lead = Agent.objects.create(slug="eva", name="Eva", owner=out["owner"], workspace=out["ws"])
    out["project"] = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    out["lead"] = lead
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["project"])
    out["reader"] = user("reader")
    member(out["reader"], out["ws"])
    Message.objects.create(session=out["session"], turn_index=0, role="user", plaintext=f"use {SECRET} please",
                           content={"text": f"use {SECRET} please"})
    Session.objects.filter(pk=out["session"].pk).update(title=f"deploy with {SECRET}")
    associate.put(out["source"], out["project"], how="dispatched", tier="core")
    return out


def client(u):
    c = Client()
    c.force_login(u)
    return c


def socket_connects(user, session) -> bool:
    """Whether the chat socket lets `user` in."""
    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope["user"] = user
        comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
        connected, _ = await comm.connect(timeout=10)
        await comm.disconnect()
        return connected
    return async_to_sync(go)()
