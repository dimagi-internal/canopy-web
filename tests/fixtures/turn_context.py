"""The world the Turn Context's tests share: an agent with every kind of entry, and one person per kind of line."""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model

from apps.agents import roles
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

M = WorkspaceMembership
CAPS = {
    "ask": {"tools": ["Read"], "description": "Ask"},
    "connect": {"tools": ["Read"], "sites": ["connect-labs"]},
    "stock": {"tools": ["Read"], "pages": ["stock://*"]},
    "data": {"tools": ["Read"], "sites": ["data-site"], "data": "sensitive"},
    "bare": {"tools": ["Read"]},
}


def make_world():
    make = lambda name: get_user_model().objects.create_user(name, f"{name}@dimagi.com", "pw")  # noqa: E731
    owner, admin, editor, reader, site_user, stranger, member = map(
        make, ("owner", "admin", "editor", "reader", "site_user", "stranger", "member"))
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    for user in (owner, admin, editor, reader, site_user, member):
        M.objects.create(user=user, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="agent", name="Agent", workspace=ws, owner=owner, turn_mode="auto",
                                 interface={"capabilities": CAPS})
    AgentAdmin.objects.create(agent=agent, user=admin, granted_by=owner)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=editor, role=roles.EDITOR)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=site_user, capability="connect")
    return SimpleNamespace(ws=ws, agent=agent, owner=owner, admin=admin, editor=editor, reader=reader, site_user=site_user,
                           stranger=stranger, member=member)


def _chat(w, creator=None, **metadata):
    return Session.objects.create(agent=w.agent, workspace=w.ws, created_by=creator or w.owner, metadata=metadata)
