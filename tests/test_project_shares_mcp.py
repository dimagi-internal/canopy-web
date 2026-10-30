"""A shared project's toolbox is opened through the projects connection by the workspaces it is
shared with (and the ones below them), exactly as by its own workspace's members: hal, an agent
of `connect`, reads the skills of `dimagi/canopy-support` once it is shared with `connect`."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent, AgentProject, ProjectService, ProjectShare
from apps.mcp.project_gateway import _openable
from apps.mcp.projects import readable_project
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.project_gateway import _text, _token, run_sessions

# Committed, as the gateway tests are: a call runs on a thread with its own connection.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    team = Workspace.objects.create(slug="connect-team", display_name="Team", created_by=owner, parent=connect)
    labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=owner, parent=dimagi)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=dimagi, owner=owner)
    project = AgentProject.objects.create(agent=ada, ext_id="P1", name="Canopy support", workspace=dimagi,
                                          handle="canopy-support")
    ProjectService.objects.create(project=project, kind="skill", name="admin-who-may-ask",
                                  description="Who may ask", instructions="Check the roles first.")

    def person(ws, name, role=WorkspaceMembership.EDITOR):
        u = User.objects.create_user(name, f"{name}@agents.test", "pw")
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
        return u

    hal_login = person(connect, "hal")
    Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=owner, user=hal_login)
    return {"project": project, "connect": connect, "hal": hal_login, "below": person(team, "below"),
            "labs": person(labs, "labs-person"), "own": person(dimagi, "own")}


def _open_and_read(user):
    async def script(new_session):
        call = await new_session()
        opened, _ = await call("tools/call", {"name": "open_project",
                                              "arguments": {"project": "dimagi/canopy-support"}})
        skill, _ = await call("tools/call", {"name": "project_skill", "arguments": {
            "project": "dimagi/canopy-support", "skill": "admin-who-may-ask"}})
        return opened, skill

    return run_sessions(_token(user), script)


def test_an_agent_of_a_division_cannot_open_the_orgs_project_until_it_is_shared_with_it(world):
    opened, skill = _open_and_read(world["hal"])
    assert opened.get("isError") and skill.get("isError")  # before: nothing to open
    assert "Check the roles first." not in _text(skill)

    ProjectShare.objects.create(project=world["project"], workspace=world["connect"])
    opened, skill = _open_and_read(world["hal"])

    assert not opened.get("isError"), _text(opened)
    assert "admin-who-may-ask" in _text(opened)
    assert _text(skill).endswith("Check the roles first.")


def test_the_share_reaches_below_it_and_not_beside_it(world):
    ProjectShare.objects.create(project=world["project"], workspace=world["connect"])

    assert [p.address for p in _openable(world["below"])] == ["dimagi/canopy-support"]  # connect-team is below
    assert _openable(world["labs"]) == []  # labs is beside connect
    assert [p.address for p in _openable(world["own"])] == ["dimagi/canopy-support"]  # as always


def test_a_workspace_shared_with_reads_a_knowledge_base_as_its_own_members_do(world):
    project = world["project"]
    ProjectService.objects.create(project=project, kind="kb", name="kb", kb_key="kb/1.sqlite.gz")
    assert project.mcp_path  # it serves a knowledge base now
    assert readable_project(world["hal"], "dimagi", "canopy-support") is None
    assert readable_project(world["own"], "dimagi", "canopy-support") is not None

    ProjectShare.objects.create(project=project, workspace=world["connect"])

    assert readable_project(world["hal"], "dimagi", "canopy-support") is not None
    assert readable_project(world["below"], "dimagi", "canopy-support") is not None
    assert readable_project(world["labs"], "dimagi", "canopy-support") is None
