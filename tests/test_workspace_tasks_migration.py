"""The data migration that makes tasks and projects the workspace's (agents 0084),
and the one that makes a check-in's agents a many-to-many (huddles 0006).

Shaped on the fleet as it is: two agents of one workspace each numbering their
own `T1` and `P1`, a workspace project stewarded by one of them in a division,
a task filed into that project by the other, and a check-in naming two agents.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# needs_migrations: it walks the real migration graph, so it cannot run under CI's
# `--nomigrations` suite and has its own job (ci.yml, `Backend pytest (migrations)`).
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0083_workspace_tasks_schema"), ("huddles", "0005_drop_work_groups")]
AFTER = [("agents", "0085_workspace_tasks_constraints"), ("huddles", "0006_checkin_agents_m2m")]


def test_tasks_and_projects_become_the_workspaces_keeping_every_alias():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Workspace = old.get_model("workspaces", "Workspace")
    Agent = old.get_model("agents", "Agent")
    AgentProject = old.get_model("agents", "AgentProject")
    AgentTask = old.get_model("agents", "AgentTask")
    ProjectCheckin = old.get_model("huddles", "ProjectCheckin")

    jj = User.objects.create(username="jj", email="jjackson@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=dimagi)
    unga = AgentProject.objects.create(agent=eva, ext_id="P1", name="UNGA 2026", scope="agent", handle="")
    AgentProject.objects.create(agent=hal, ext_id="P1", name="UNGA 2026", scope="agent", handle="")
    kb = AgentProject.objects.create(agent=eva, ext_id="P2", name="GiveWell KB", scope="workspace",
                                     owner_workspace=connect, handle="givewell-kb")
    AgentTask.objects.create(agent=eva, ext_id="T1", title="Book the room", project=unga, position=3,
                             confidence="HIGH")
    AgentTask.objects.create(agent=hal, ext_id="T1", title="Hal's own", confidence="maybe")
    AgentTask.objects.create(agent=hal, ext_id="T2", title="Into the KB", project=kb)
    checkin = ProjectCheckin.objects.create(project=unga, name="Weekly", agents=["eva", "hal", "gone"])

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    AgentProject = new.get_model("agents", "AgentProject")
    AgentTask = new.get_model("agents", "AgentTask")
    TaskSettings = new.get_model("agents", "TaskSettings")
    ProjectCheckin = new.get_model("huddles", "ProjectCheckin")

    assert dict(TaskSettings.objects.values_list("workspace_id", "prefix")) == {"dimagi": "DIM", "connect": "CON"}
    projects = {(p.created_by_agent.slug, p.ext_id): p for p in AgentProject.objects.select_related("created_by_agent")}
    assert (projects[("eva", "P1")].workspace_id, projects[("eva", "P1")].handle) == ("dimagi", "unga-2026")
    assert projects[("hal", "P1")].handle == "unga-2026-2"  # same name, its own address
    assert (projects[("eva", "P2")].workspace_id, projects[("eva", "P2")].handle) == ("connect", "givewell-kb")
    assert [a.slug for a in projects[("eva", "P1")].member_agents.all()] == ["eva"]

    tasks = {(t.created_by_agent.slug, t.ext_id): t for t in AgentTask.objects.select_related("created_by_agent")}
    room, own, into_kb = tasks[("eva", "T1")], tasks[("hal", "T1")], tasks[("hal", "T2")]
    assert (room.key, room.workspace_id, room.agent_id, room.rank, room.confidence) == (
        "DIM-1", "dimagi", eva.pk, 3.0, "high")
    assert (own.key, own.confidence) == ("DIM-2", "")
    assert (into_kb.key, into_kb.workspace_id) == ("CON-1", "connect")  # a task lives where its project does
    assert TaskSettings.objects.get(workspace_id="dimagi").last_number == 2

    assert sorted(a.slug for a in ProjectCheckin.objects.get(pk=checkin.pk).agent_members.all()) == ["eva", "hal"]
