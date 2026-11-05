"""The chat composer's `/` autocomplete: a runner reports what its sessions can
run, and a session's composer is offered that plus its agent's launchable skills
— only to someone who can send to it."""

import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents import skill_catalog
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


def test_an_internal_skill_is_not_offered_and_a_report_without_the_field_means_not_internal(setup):
    RunnerBinding.objects.create(session=setup["session"], runner=setup["runner"], session_key="k")
    assert _report(setup, [
        {"name": "hal:shipping", "description": "ships", "source": "plugin", "internal": True},
        {"name": "hal:ask", "description": "asks", "source": "acp", "internal": False},
        {"name": "hal:old-runner", "description": "no field at all", "source": "plugin"},
    ]).status_code == 204
    names = [r["name"] for r in _commands(setup["owner"], setup["session"])]
    assert names == ["hal:turn", "hal:ask", "hal:old-runner"]


def test_being_launchable_does_not_put_an_internal_skill_on_the_menu(setup):
    # Marked on the agent's row: a session with no runner yet has no report to ask.
    AgentSkill.objects.filter(agent=setup["agent"], name="turn").update(internal=True)
    assert _commands(setup["owner"], setup["session"]) == []
    # Marked on the report: the row has not caught up yet.
    AgentSkill.objects.filter(agent=setup["agent"], name="turn").update(internal=False)
    RunnerBinding.objects.create(session=setup["session"], runner=setup["runner"], session_key="k")
    _report(setup, [{"name": "hal:turn", "source": "plugin", "internal": True}])
    assert _commands(setup["owner"], setup["session"]) == []


def test_skill_files_on_a_box_to_the_menu_and_the_catalog(setup, tmp_path, monkeypatch):
    """The whole way: real files, the runner's real scan, its report over the API,
    the catalog tick, then both reads. The marker is written as Trellis writes it."""
    from canopy_runner import slash_commands as runner_scan

    monkeypatch.setattr(runner_scan, "_acp_seen", {})
    plugin = tmp_path / "plugins" / "hal"
    files = {
        "turn": "description: Do a turn.",
        "shipping": "user-invocable: false   # internal: Claude invokes it when a turn is about to ship\n"
                    "description: How Hal ships.",
    }
    for name, header in files.items():
        path = plugin / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n{header}\n---\nbody")
    (tmp_path / ".claude" / "plugins").mkdir(parents=True)
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"hal@hal": [{"installPath": str(plugin)}]}}))

    setup["agent"].skills.all().delete()
    RunnerBinding.objects.create(session=setup["session"], runner=setup["runner"], session_key="k")
    assert _report(setup, runner_scan.scan(tmp_path)).status_code == 204
    assert skill_catalog.refresh(setup["agent"]) == 2

    assert [r["name"] for r in _commands(setup["owner"], setup["session"])] == ["hal:turn"]
    catalog = _client(setup["owner"]).get("/api/agents/hal/skills/").json()
    assert {s["name"]: s["internal"] for s in catalog} == {"turn": False, "shipping": True}
    detail = _client(setup["owner"]).get("/api/agents/hal/").json()
    assert detail["skill_count"] == 2


def test_a_published_catalog_follows_what_a_box_installed(setup, tmp_path, monkeypatch):
    """The whole way, for a catalog published through its route and then left to go
    stale: real files, the runner's real scan, its report, the tick, the read."""
    from canopy_runner import slash_commands as runner_scan

    monkeypatch.setattr(runner_scan, "_acp_seen", {})
    owner = _client(setup["owner"])
    resp = owner.put("/api/agents/hal/skills/", {"skills": [
        {"name": "turn", "description": "old", "launchable": True, "improvement_note": "keep me",
         "url": "https://example.com/turn"},
        {"name": "retired", "improvement_note": "the skill is gone"},
    ]}, content_type="application/json")
    assert resp.status_code == 200, resp.content

    plugin = tmp_path / "plugins" / "hal"
    for name in ("turn", "added"):
        path = plugin / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: {name} today\n---\nbody")
    (tmp_path / ".claude" / "plugins").mkdir(parents=True)
    installed = (timezone.now() + timedelta(minutes=5)).isoformat()
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"hal@hal": [{"installPath": str(plugin), "lastUpdated": installed}]}}))

    assert _report(setup, runner_scan.scan(tmp_path)).status_code == 204
    assert skill_catalog.refresh_all() == {"hal": 2}

    rows = {s["name"]: s for s in owner.get("/api/agents/hal/skills/").json()}
    assert set(rows) == {"turn", "added"}
    assert rows["turn"]["launchable"] is True and rows["turn"]["improvement_note"] == "keep me"
    assert rows["turn"]["url"] == "https://example.com/turn" and rows["turn"]["description"] == "turn today"
    assert rows["added"]["description"] == "added today" and rows["added"]["launchable"] is False


def test_someone_who_can_only_read_gets_nothing(setup):
    SessionParticipant.objects.create(session=setup["session"], user=setup["viewer"],
                                      role=SessionParticipant.VIEWER)
    assert _commands(setup["viewer"], setup["session"]) == []
