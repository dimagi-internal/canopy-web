"""Every work group becomes a project's check-in (spec 2026-10-17-project-check-ins-design.md §7).

* A single-mode group already has its project: it becomes that project's check-in.
* A multi-mode group gets a new ACTIVE project on its leader agent — the group's
  name, its topic as the outcome, its lead as the owner and a `manual`
  participant — and becomes that project's check-in.
* The group's schedule IS the check-in's schedule (same row, same id, same
  cadence and slot bookkeeping), so it keeps firing on time.
* The read allow-list entry is untouched: `added_allowance` carries over as is,
  so an entry a person added is still theirs and is never removed by the check-in.
* Each run moves across keeping its id (a turn, an engine and a Slack button
  name runs by id), and engagement rows keyed by the group's slug are rekeyed
  to the check-in's id.
"""

from django.db import migrations
from django.db.models import F


def _name(cron: str) -> str:
    parts = (cron or "").split()
    weekly = len(parts) == 5 and parts[2] == "*" and parts[4] != "*" and "," not in parts[4]
    return "Weekly check-in" if weekly else "Check-in"


def forward(apps, schema_editor):
    HuddleGroup = apps.get_model("huddles", "HuddleGroup")
    HuddleGroupRun = apps.get_model("huddles", "HuddleGroupRun")
    ProjectCheckin = apps.get_model("huddles", "ProjectCheckin")
    CheckinRun = apps.get_model("huddles", "CheckinRun")
    Agent = apps.get_model("agents", "Agent")
    AgentProject = apps.get_model("agents", "AgentProject")
    ProjectParticipant = apps.get_model("agents", "ProjectParticipant")
    EngagementEvent = apps.get_model("agents", "EngagementEvent")
    Person = apps.get_model("contacts", "Person")
    AgentSchedule = apps.get_model("harness", "AgentSchedule")

    moved_runs = False
    for g in HuddleGroup.objects.select_related("leader_agent", "lead", "schedule", "project").order_by("pk"):
        leader = g.leader_agent
        project = g.project
        if project is None or project.agent_id != leader.pk:
            Agent.objects.filter(pk=leader.pk).update(project_seq=F("project_seq") + 1)
            leader.refresh_from_db(fields=["project_seq"])
            project = AgentProject.objects.create(
                agent=leader, ext_id=f"P{leader.project_seq}", name=g.name[:200], outcome=g.topic,
                status="active", owner_user=g.lead,
                notes=(f"The {g.name} project: its weekly huddle in #{g.channel_name} is the project's "
                       "check-in." if g.channel_name else f"The {g.name} project."))
        agents = [leader.slug] + [s for s in (g.agents or []) if s != leader.slug]
        name = _name(g.schedule.cron if g.schedule_id else "")
        checkin = ProjectCheckin.objects.create(
            project=project, name=name, topic=g.topic, lead=g.lead, lead_slack_id=g.lead_slack_id,
            installation=g.installation, channel_id=g.channel_id, channel_name=g.channel_name,
            agents=agents, schedule=g.schedule, enabled=g.enabled, added_allowance=g.added_allowance,
            legacy_slug=g.slug, created_by=g.created_by)
        if g.schedule_id:
            AgentSchedule.objects.filter(pk=g.schedule_id).update(
                name=f"Check-in: {project.name} — {name}"[:200], agent=project.agent)
        # The lead is a manual participant; every other agent a `checkin` one.
        people = []
        if g.lead_id:
            people.append((Person.objects.filter(user_id=g.lead_id).first(), "manual", "check-in lead"))
        for other in Agent.objects.filter(slug__in=agents[1:], user__isnull=False):
            people.append((Person.objects.filter(user_id=other.user_id).first(), "checkin",
                           f"{other.slug}, {name}"))
        for person, source, role in people:
            if person is not None and not ProjectParticipant.objects.filter(project=project, person=person).exists():
                ProjectParticipant.objects.create(project=project, person=person, source=source, role=role[:80])
        lead_email = g.lead.email if g.lead_id else ""
        for r in HuddleGroupRun.objects.filter(group=g).order_by("pk"):
            CheckinRun.objects.create(
                id=r.id, checkin=checkin, project=project, checkin_name=name, slot=r.slot, manual=r.manual,
                status=r.status, note=r.note, participants={"agents": agents, "lead_email": lead_email},
                channel_id=g.channel_id, channel_name=g.channel_name, announce_ts=r.announce_ts,
                confirmed_by_id=r.confirmed_by_id, confirmed_by_slack=r.confirmed_by_slack,
                confirmed_at=r.confirmed_at, ingest_id=r.ingest_id, read_messages=r.read_messages,
                read_first_ts=r.read_first_ts, read_last_ts=r.read_last_ts, read_since=r.read_since,
                material=r.material, turn_id=r.turn_id, huddle_id=r.huddle_id, result_url=r.result_url,
                summary=r.summary, finished_at=r.finished_at)
            # auto_now_add / auto_now stamp "now" on create: keep the run's own dates.
            CheckinRun.objects.filter(pk=r.id).update(created_at=r.created_at, updated_at=r.updated_at)
            moved_runs = True
        EngagementEvent.objects.filter(workspace_id=g.workspace_id, checkin=g.slug).update(checkin=str(checkin.pk))

    if moved_runs and schema_editor.connection.vendor == "postgresql":
        # Runs were inserted with their old ids: move the sequence past them.
        table = CheckinRun._meta.db_table
        with schema_editor.connection.cursor() as cur:
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"(SELECT MAX(id) FROM {table}))")


class Migration(migrations.Migration):
    dependencies = [
        ("huddles", "0003_project_checkins"),
        ("agents", "0061_project_checkins"),
        ("contacts", "0023_memory_reach"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
