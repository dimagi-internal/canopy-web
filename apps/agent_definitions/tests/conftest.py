"""The world the stored-agent tests share: a workspace with its people, a stored agent
`scout` (owned by olive, administered by ada, its login `bot`) and a repo agent `ranger`."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent, AgentAdmin
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace
from tests.access_helpers import editors_run
from tests.fixtures.stored_agents import SLUG, _browser


@pytest.fixture
def w():
    ws = a_workspace("scouts")
    owner = a_member(ws, email="olive@dimagi.com", role=WorkspaceMembership.EDITOR)
    editor = a_member(ws, email="eddie@dimagi.com", role=WorkspaceMembership.EDITOR)
    viewer = a_member(ws, email="vera@dimagi.com", role=WorkspaceMembership.VIEWER)
    admin = a_member(ws, email="ada@dimagi.com", role=WorkspaceMembership.EDITOR)
    outsider_ws = a_workspace("elsewhere")
    outsider = a_member(outsider_ws, email="otto@dimagi.com", role=WorkspaceMembership.OWNER)
    bot = a_member(ws, email="scout@agents.test", role=WorkspaceMembership.EDITOR)
    return {"ws": ws, "owner": owner, "editor": editor, "viewer": viewer, "admin": admin,
            "outsider": outsider, "bot": bot}


@pytest.fixture
def agent(w):
    r = _browser(w["owner"]).post("/api/agents/stored", content_type="application/json", data={
        "name": "Scout", "slug": SLUG, "mandate": "Finds trails.", "workspace": "scouts"})
    assert r.status_code == 201, r.content
    a = Agent.objects.get(slug=SLUG)
    AgentAdmin.objects.create(agent=a, user=w["admin"], granted_by=w["owner"])
    a.user = w["bot"]
    a.save(update_fields=["user"])
    editors_run(a)
    return a


@pytest.fixture
def repo_agent(w):
    a = Agent.objects.create(slug="ranger", name="Ranger", workspace=w["ws"], owner=w["owner"],
                             repo_url="https://github.com/olive/ranger", user=None)
    return a
