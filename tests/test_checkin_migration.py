"""The data migration that turns work groups into project check-ins (huddles 0004).

Shaped on the live group (2026-10-17): a MULTI-mode group (ada leads, hal takes
part), lead Jonathan, #canopy-support whose allow-list entry a PERSON added (so
`added_allowance` is False and the entry must stay theirs), a weekly schedule,
and two runs. It must come out as a new active "Canopy support" project on ada
with a "Weekly check-in", the SAME schedule row and the SAME run ids.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("huddles", "0002_huddle_group_added_allowance"), ("agents", "0060_credential_alert_no_turn_check")]


def _latest(executor):
    executor.loader.build_graph()
    return executor.loader.graph.leaf_nodes()


def test_the_live_work_group_becomes_a_projects_check_in():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Workspace = old.get_model("workspaces", "Workspace")
    Agent = old.get_model("agents", "Agent")
    AgentSchedule = old.get_model("harness", "AgentSchedule")
    SlackInstallation = old.get_model("slack", "SlackInstallation")
    Person = old.get_model("contacts", "Person")
    HuddleGroup = old.get_model("huddles", "HuddleGroup")
    HuddleGroupRun = old.get_model("huddles", "HuddleGroupRun")
    EngagementEvent = old.get_model("agents", "EngagementEvent")

    jj = User.objects.create(username="jj", email="jjackson@dimagi.com")
    hal_user = User.objects.create(username="hal", email="hal@dimagi-ai.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, project_seq=6)
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, user=hal_user)
    jj_person = Person.objects.create(user=jj, email="jjackson@dimagi.com")
    hal_person = Person.objects.create(user=hal_user, email="hal@dimagi-ai.com")
    sched = AgentSchedule.objects.create(agent=ada, name="Work group: Canopy support (canopy-support)",
                                         prompt="/canopy:huddle", cron="0 9 * * 2", timezone="America/Denver")
    inst = SlackInstallation.objects.create(team_id="T1", team_name="Dimagi", bot_user_id="UBOT")
    g = HuddleGroup.objects.create(
        workspace=ws, slug="canopy-support", name="Canopy support", topic="Help people use canopy.",
        lead=jj, lead_slack_id="UJJ", installation=inst, channel_id="C0C2WKVCY4B", channel_name="canopy-support",
        agents=["ada", "hal"], leader_agent=ada, schedule=sched, added_allowance=False, created_by=jj)
    t0 = dt.datetime(2026, 10, 16, 15, tzinfo=dt.UTC)
    r1 = HuddleGroupRun.objects.create(group=g, slot=t0, status="done", announce_ts="1.1", confirmed_by=jj,
                                       read_messages=12, read_last_ts="1760000000.000100", huddle_id="h-one",
                                       summary="two projects")
    r2 = HuddleGroupRun.objects.create(group=g, slot=t0 + dt.timedelta(days=1), status="done", read_messages=1)
    EngagementEvent.objects.create(workspace=ws, kind="run_announced", surface="slack", group="canopy-support",
                                   meta={"run": r1.pk})

    executor = MigrationExecutor(connection)
    executor.migrate(_latest(executor))

    from apps.agents.models import AgentProject, EngagementEvent as Ev, ProjectParticipant
    from apps.harness.models import AgentSchedule as Sched
    from apps.huddles.models import CheckinRun, ProjectCheckin

    c = ProjectCheckin.objects.get()
    p = c.project
    assert (p.agent.slug, p.ext_id, p.name, p.status, p.owner_user_id) == ("ada", "P7", "Canopy support", "active",
                                                                           jj.pk)
    assert p.outcome == "Help people use canopy."
    assert (c.name, c.agents, c.lead_id, c.channel_id, c.legacy_slug) == (
        "Weekly check-in", ["ada", "hal"], jj.pk, "C0C2WKVCY4B", "canopy-support")
    assert c.added_allowance is False and c.format == "huddle"
    s = Sched.objects.get()
    assert s.pk == sched.pk and c.schedule_id == sched.pk
    assert (s.cron, s.timezone, s.agent.slug) == ("0 9 * * 2", "America/Denver", "ada")
    assert s.name == "Check-in: Canopy support — Weekly check-in"
    runs = list(CheckinRun.objects.order_by("pk"))
    assert [r.pk for r in runs] == [r1.pk, r2.pk]
    assert all(r.project_id == p.pk and r.checkin_id == c.pk for r in runs)
    assert runs[0].huddle_id == "h-one" and runs[0].read_messages == 12 and runs[0].confirmed_by_id == jj.pk
    assert runs[0].participants == {"agents": ["ada", "hal"], "lead_email": "jjackson@dimagi.com"}
    assert runs[0].channel_name == "canopy-support"
    assert set(ProjectParticipant.objects.filter(project=p).values_list("person_id", "source")) == {
        (jj_person.pk, "manual"), (hal_person.pk, "checkin")}
    assert Ev.objects.get().checkin == str(c.pk)
    assert AgentProject.objects.get(pk=p.pk).agent.project_seq == 7
    # A new run gets a fresh id past the moved ones.
    new = CheckinRun.objects.create(checkin=c, project=p, slot=t0 + dt.timedelta(days=7))
    assert new.pk > r2.pk
