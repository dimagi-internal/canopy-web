"""The chat composer's `/` autocomplete: a runner reports what its sessions can
run, and a session's composer is offered that plus its agent's launchable skills
— only to someone who can send to it."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentSkill
from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner, RunnerSlashCommands
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    owner = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    viewer = User.objects.create_user("viewer", "viewer@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    AgentSkill.objects.create(agent=agent, name="turn", description="Do a turn.",
                              launchable=True, args_hint="scope (optional)")
    AgentSkill.objects.create(agent=agent, name="setup", description="One-time setup.")
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), owner=owner,
    )
    session = Session.objects.create(workspace=ws, agent=agent, created_by=owner,
                                     origin=Session.ORIGIN_WEB, title="chat")
    return {"owner": owner, "viewer": viewer, "agent": agent, "runner": runner, "session": session}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _report(setup, commands):
    return _client(setup["owner"]).put(
        f"/api/harness/runners/{setup['runner'].id}/slash-commands",
        {"commands": commands}, content_type="application/json")


def _commands(user, session):
    resp = _client(user).get(f"/api/canopy-sessions/{session.id}/slash-commands")
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_a_report_replaces_the_runner_list(setup):
    assert _report(setup, [{"name": "a", "source": "user"}]).status_code == 204
    assert _report(setup, [{"name": "b:c", "description": "d", "source": "plugin"}]).status_code == 204
    held = RunnerSlashCommands.objects.get(runner=setup["runner"]).commands
    assert [c["name"] for c in held] == ["b:c"]


def test_a_runner_row_never_carries_the_list(setup):
    # The list runs to ~120 KB per box, and a Runner row is loaded on every poll
    # and by every select_related that reaches one. Keep it off that row.
    _report(setup, [{"name": f"cmd{i}", "description": "x" * 200, "source": "plugin"}
                    for i in range(500)])
    columns = {f.column for f in Runner._meta.concrete_fields}
    assert not any("slash" in c or c == "commands" for c in columns)
    assert "commands" not in str(Runner.objects.all().query)


def test_a_report_refuses_a_name_that_is_not_a_command(setup):
    assert _report(setup, [{"name": "has space"}]).status_code == 422


def test_an_unbound_session_offers_only_the_agents_launchable_skills(setup):
    rows = _commands(setup["owner"], setup["session"])
    assert rows == [{"name": "hal:turn", "description": "Do a turn.",
                     "args_hint": "scope (optional)", "source": "agent"}]


def test_a_bound_session_merges_the_runner_list_agent_skills_first(setup):
    _report(setup, [
        {"name": "zeta", "description": "user skill", "source": "user"},
        {"name": "hal:turn", "description": "from disk", "source": "plugin"},
        {"name": "hal:setup", "description": "setup", "source": "plugin"},
        {"name": "ace:run", "description": "run", "source": "plugin"},
    ])
    RunnerBinding.objects.create(session=setup["session"], runner=setup["runner"], session_key="k")
    rows = _commands(setup["owner"], setup["session"])
    assert [r["name"] for r in rows] == ["hal:turn", "hal:setup", "ace:run", "zeta"]
    assert rows[0]["source"] == "agent" and rows[0]["args_hint"] == "scope (optional)"
    assert rows[0]["description"] == "from disk"


def test_someone_who_can_only_read_gets_nothing(setup):
    SessionParticipant.objects.create(session=setup["session"], user=setup["viewer"],
                                      role=SessionParticipant.VIEWER)
    assert _commands(setup["viewer"], setup["session"]) == []
