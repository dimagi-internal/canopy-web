"""Fleet brain v1.1 (canopy#804): project participants, the coverage metric, and
Contact.notes mirrored into a fact. (The per-agent digest opt-out went with the
digest turn, 2026-10-09.)

Privacy rules pinned here, beside v1's in `test_people_brain.py`:

* a person's projects are served per workspace, to its members, and only that
  workspace's projects (another workspace's are never listed);
* coverage is counts only, members of the workspace (an agent admin who is not
  a member sees only their own agents), and a stranger gets 404;
* mirroring notes is idempotent — the migration and the command can re-run.
"""
from __future__ import annotations

import datetime as dt
import importlib
import io

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone

from apps.agents import participants
from apps.agents import services as agent_services
from apps.agents.models import AgentProject, ProjectParticipant
from apps.contacts import coverage, people
from apps.contacts import services as contacts
from apps.contacts.models import Contact, PersonAccess, PersonFact
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_people_brain import (
    _agent,
    _client,
    _fact,
    _finish,
    _human_turn,
    _member,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    """The same world as `test_people_brain.py`: ace and hal in `connect`, eva in
    `dimagi`, Jonathan owning both, Lilianna an editor of `connect`."""
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    lili = _member(ws, "lili", first_name="Lilianna", last_name="Bagnoli")
    return {"owner": owner, "ws": ws, "other": other, "lili": lili,
            "ace": _agent(ws, "ace", owner), "hal": _agent(ws, "hal", owner),
            "eva": _agent(other, "eva", owner)}


def _project(agent, name="KC rollout", status=AgentProject.ACTIVE):
    return AgentProject.objects.create(agent=agent, ext_id=agent_services.next_project_ext_id(agent),
                                       name=name, status=status)


# --- 1. project participants ------------------------------------------------------------


def test_a_fact_with_a_project_makes_its_subject_a_participant_once(world):
    ws, lili, ace = world["ws"], world["lili"], world["ace"]
    p1 = _project(ace)
    person = contacts.person_for(user=lili)
    _fact(person, ws, "project", "Leads the KC rollout.", project=p1)
    _fact(person, ws, "instance", "Owns the KC bot.", project=p1)   # any kind links
    _fact(person, ws, "role", "Program lead.")                       # no project: nothing
    row = ProjectParticipant.objects.get()
    assert (row.project, row.person, row.source, row.role) == (p1, person, "fact", "")
    assert list(p1.participants.all()) == [person]


def test_a_task_raised_by_a_humans_turn_links_them_to_its_project(world):
    ace, lili = world["ace"], world["lili"]
    p1 = _project(ace)
    turn = _human_turn(ace, lili, "t1")
    agent_services.create_tasks(ace, [{"title": "Fix the bot", "project": p1.ext_id,
                                       "raised_by": str(turn.pk)}])
    row = ProjectParticipant.objects.get()
    assert (row.person, row.source) == (contacts.person_for(user=lili), "turn")


def test_a_task_raised_by_canopy_or_with_no_project_links_nobody(world):
    ace = world["ace"]
    p1 = _project(ace)
    sched, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="s",
                                     initiator=who.system(via="schedule"))
    agent_services.create_tasks(ace, [{"title": "a", "project": p1.ext_id, "raised_by": str(sched.pk)}])
    human = _human_turn(ace, world["lili"], "t1")
    [task] = agent_services.create_tasks(ace, [{"title": "b", "raised_by": str(human.pk)}])
    assert not ProjectParticipant.objects.exists()
    # ... until the human-raised task is filed into a project.
    agent_services.patch_task(task, {"project": p1.ext_id})
    assert ProjectParticipant.objects.get().source == "turn"


def test_the_first_source_wins(world):
    ws, ace, lili = world["ws"], world["ace"], world["lili"]
    p1 = _project(ace)
    turn = _human_turn(ace, lili, "t1")
    agent_services.create_tasks(ace, [{"title": "x", "project": p1.ext_id, "raised_by": str(turn.pk)}])
    _fact(contacts.person_for(user=lili), ws, "project", "On KC.", project=p1)
    assert list(ProjectParticipant.objects.values_list("source", flat=True)) == ["turn"]


def test_the_project_detail_lists_participants_to_members_only(world):
    ws, ace, lili = world["ws"], world["ace"], world["lili"]
    p1 = _project(ace)
    _fact(contacts.person_for(user=lili), ws, "project", "On KC.", project=p1)
    body = _client(world["owner"]).get(f"/api/agents/ace/projects/{p1.ext_id}/").json()
    [p] = body["participants"]
    assert (p["display_name"], p["email"], p["role"], p["source"]) == (
        "Lilianna Bagnoli", "lili@dimagi.com", "", "fact")
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    assert _client(stranger).get(f"/api/agents/ace/projects/{p1.ext_id}/").status_code == 404


def test_a_persons_projects_route_is_per_workspace(world):
    ws, other, ace, eva, lili = world["ws"], world["other"], world["ace"], world["eva"], world["lili"]
    WorkspaceMembership.objects.create(user=lili, workspace=other, role=WorkspaceMembership.EDITOR)
    person = contacts.person_for(user=lili)
    p1 = _project(ace, "KC rollout")
    p_eva = _project(eva, "Gates trip")
    _fact(person, ws, "project", "On KC.", project=p1)
    _fact(person, other, "project", "On the trip.", project=p_eva)

    owner = _client(world["owner"])
    body = owner.get(f"/api/people/{person.pk}/projects/?workspace=connect").json()
    assert [(p["ext_id"], p["name"], p["agent"]) for p in body["projects"]] == [(p1.ext_id, "KC rollout", "ace")]
    body = owner.get(f"/api/people/{person.pk}/projects/?workspace=dimagi").json()
    assert [p["agent"] for p in body["projects"]] == ["eva"]
    assert PersonAccess.objects.filter(person=person, via="api").count() == 2

    # A member of connect only cannot ask about dimagi; a stranger gets nothing.
    bob = _member(ws, "bob")
    assert _client(bob).get(f"/api/people/{person.pk}/projects/?workspace=dimagi").status_code == 404
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    assert _client(stranger).get(f"/api/people/{person.pk}/projects/?workspace=connect").status_code == 404
    # A person the workspace does not deal with is not found there.
    unknown = contacts.person_for(email="nobody@else.org")
    assert owner.get(f"/api/people/{unknown.pk}/projects/?workspace=connect").status_code == 404


def test_the_envelope_lists_up_to_five_live_projects_most_recent_first(world):
    ws, ace, hal, lili = world["ws"], world["ace"], world["hal"], world["lili"]
    person = contacts.person_for(user=lili)
    made = []
    for i in range(7):
        p = _project(ace if i % 2 else hal, f"P{i}")
        _fact(person, ws, "project", f"On {i}.", project=p)
        made.append(p)
    archived = made[6]
    AgentProject.objects.filter(pk=archived.pk).update(status=AgentProject.ARCHIVED)
    base = timezone.now()
    for i, p in enumerate(made):
        AgentProject.objects.filter(pk=p.pk).update(updated_at=base + dt.timedelta(minutes=i))

    block = caller_context.build(_human_turn(ace, lili, "t1"))["person"]
    assert [p["name"] for p in block["projects"]] == ["P5", "P4", "P3", "P2", "P1"]
    assert set(block["projects"][0]) == {"id", "ext_id", "name", "agent"}


def test_the_envelope_never_lists_another_workspaces_projects(world):
    other, eva, ace, lili = world["other"], world["eva"], world["ace"], world["lili"]
    person = contacts.person_for(user=lili)
    _fact(person, other, "project", "On the trip.", project=_project(eva))
    assert caller_context.build(_human_turn(ace, lili, "t1"))["person"]["projects"] == []


def test_backfill_links_facts_and_raisers_and_is_idempotent(world):
    ws, ace, lili, owner = world["ws"], world["ace"], world["lili"], world["owner"]
    p1 = _project(ace)
    p2 = _project(ace, "Other")
    # Rows that predate v1.1: written without the hooks.
    PersonFact.objects.create(person=contacts.person_for(user=lili), workspace=ws, kind="project",
                              statement="On KC.", project=p1)
    turn = _human_turn(ace, owner, "t1")
    task = agent_services.create_tasks(ace, [{"title": "x"}])[0]
    type(task).objects.filter(pk=task.pk).update(project=p2, raised_by=turn)
    assert not ProjectParticipant.objects.exists()

    out = io.StringIO()
    call_command("backfill_project_participants", stdout=out)
    assert "participants added: 2" in out.getvalue()
    assert set(ProjectParticipant.objects.values_list("project_id", "source")) == {(p1.pk, "fact"), (p2.pk, "turn")}
    assert participants.backfill()["participants_added"] == 0


def test_the_migration_links_existing_fact_subjects_idempotently(world):
    ws, ace, lili = world["ws"], world["ace"], world["lili"]
    p1 = _project(ace)
    PersonFact.objects.create(person=contacts.person_for(user=lili), workspace=ws, kind="role",
                              statement="On KC.", project=p1)
    mig = importlib.import_module("apps.agents.migrations.0041_link_fact_subjects_to_projects")
    mig.link_fact_subjects(django_apps, None)
    mig.link_fact_subjects(django_apps, None)
    assert ProjectParticipant.objects.get().source == "fact"


# --- 2. coverage ---------------------------------------------------------------------------


def _report(ws="connect", **kw):
    return coverage.workspace_coverage(ws, **kw)


def _row(report, slug):
    return next(r for r in report["agents"] if r["agent"] == slug)


def test_coverage_counts_human_turns_context_and_in_session_facts(world):
    ws, ace, lili = world["ws"], world["ace"], world["lili"]
    person = contacts.person_for(user=lili)
    # No context yet: the envelope is built empty and recorded as such.
    t1 = _human_turn(ace, lili, "t1")
    caller_context.build(t1)
    _finish(t1)
    # Now the brain knows something: the next envelope has context.
    people.record_fact(person=person, workspace=ws, kind="role", statement="Lead.", by_agent=ace,
                       source_turn=t1)
    t2 = _human_turn(ace, lili, "t2")
    caller_context.build(t2)
    # Not human: canopy, and another agent's login.
    sched, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="s",
                                     initiator=who.system(via="schedule"))
    # Written from a turn no human started: recorded, but not counted as in-session.
    people.record_fact(person=person, workspace=ws, kind="project", statement="KC.", by_agent=ace,
                       source_turn=sched)
    _human_turn(ace, world["hal"].user, "from-hal")

    row = _row(_report(), "ace")
    assert row["human_turns"] == 2
    assert row["human_turns_with_context"] == 1
    assert row["context_rate"] == 0.5
    assert row["facts_written"] == 1
    assert row["people"] == 1
    assert set(row) == {"agent", "human_turns", "human_turns_with_context", "context_rate",
                        "facts_written", "people", "healthy", "reasons"}
    assert row["healthy"] is True
    # Envelope reads record whether there was context; API reads never claim it.
    assert list(PersonAccess.objects.filter(via="envelope").order_by("created_at", "pk")
                .values_list("had_context", flat=True)) == [False, True]


def test_a_dead_brain_is_loud_ten_human_turns_and_no_facts(world):
    ace, lili = world["ace"], world["lili"]
    for i in range(coverage.MIN_TURNS_FOR_FACTS):
        _human_turn(ace, lili, f"t{i}")
    report = _report()
    row = _row(report, "ace")
    assert row["healthy"] is False
    assert "no facts recorded in-session" in row["reasons"][0]
    assert report["healthy"] is False


def test_coverage_window_excludes_old_turns(world):
    t = _human_turn(world["ace"], world["lili"], "old")
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(days=8))
    assert _row(_report(days=7), "ace")["human_turns"] == 0
    assert _row(_report(days=9), "ace")["human_turns"] == 1


def test_coverage_api_is_for_members_of_the_workspace(world):
    ws = world["ws"]
    viewer = _member(ws, "vic", role=WorkspaceMembership.VIEWER)
    body = _client(viewer).get("/api/people/coverage/?workspace=connect&days=7").json()
    assert body["workspace"] == "connect" and body["days"] == 7
    assert {r["agent"] for r in body["agents"]} == {"ace", "hal"}       # not eva (dimagi)
    assert "rule" in body and isinstance(body["healthy"], bool)
    # A member of another workspace only, or a stranger: 404, never a hint.
    outsider = _member(world["other"], "out")
    assert _client(outsider).get("/api/people/coverage/?workspace=connect").status_code == 404
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    assert _client(stranger).get("/api/people/coverage/?workspace=connect").status_code == 404
    assert _client(viewer).get("/api/people/coverage/?workspace=connect&days=0").status_code == 400
    assert _client(viewer).get("/api/people/coverage/?workspace=nope").status_code == 404


def test_an_agent_admin_who_is_not_a_member_sees_only_their_agents(world):
    # A child workspace's agents are administered by the parent's owner, who
    # holds no membership row in the child.
    owner = world["owner"]
    parent = Workspace.objects.create(slug="org", display_name="Org", created_by=owner)
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=boss, workspace=parent, role=WorkspaceMembership.OWNER)
    ws = world["ws"]
    ws.parent = parent
    ws.save()
    assert world["ace"].is_admin(boss)
    body = _client(boss).get("/api/people/coverage/?workspace=connect").json()
    assert {r["agent"] for r in body["agents"]} == {"ace", "hal"}


def test_people_coverage_command_prints_and_fails_loud(world):
    ace, lili = world["ace"], world["lili"]
    out = io.StringIO()
    call_command("people_coverage", "--workspace", "connect", "--days", "7", stdout=out)
    assert "HEALTHY" in out.getvalue() and "ace" in out.getvalue()
    for i in range(coverage.MIN_TURNS_FOR_FACTS):
        _human_turn(ace, lili, f"t{i}")
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("people_coverage", "--workspace", "connect", "--json", stdout=out)
    assert exc.value.code == 1
    assert '"healthy": false' in out.getvalue()


# --- 4. Contact.notes → a fact ----------------------------------------------------------


def _correspondent(ws, address="fatima@llo.org", notes=""):
    c = contacts.record_inbound_sender(workspace=ws, address=address)
    if notes:
        Contact.objects.filter(pk=c.pk).update(notes=notes)
        c.refresh_from_db()
    return c


def test_mirroring_notes_is_one_role_fact_and_idempotent(world):
    c = _correspondent(world["ws"], notes="Program officer at LLO Foo.\n  Prefers   email.  " + "x" * 600)
    f1 = people.mirror_contact_notes(c)
    # attested (HCP issuer-attested): someone other than the person wrote the notes.
    assert (f1.kind, f1.basis, f1.source_contact, f1.workspace_id) == ("role", "attested", c, "connect")
    assert f1.asserted_by_user is None and f1.asserted_by_agent is None
    assert f1.statement.startswith("Program officer at LLO Foo. Prefers email. xxx")
    assert "\n" not in f1.statement and len(f1.statement) == PersonFact.STATEMENT_MAX
    assert people.mirror_contact_notes(c) == f1
    assert PersonFact.objects.count() == 1
    out = io.StringIO()
    call_command("mirror_contact_notes", stdout=out)
    assert "contacts with notes: 1" in out.getvalue()
    assert PersonFact.objects.count() == 1


def test_the_migration_mirrors_notes_idempotently_and_supersedes_on_change(world):
    c = _correspondent(world["ws"], notes="Program officer.")
    _correspondent(world["ws"], address="empty@llo.org")         # no notes: nothing
    mig = importlib.import_module("apps.contacts.migrations.0011_mirror_contact_notes")
    mig.mirror_notes(django_apps, None)
    mig.mirror_notes(django_apps, None)
    first = PersonFact.objects.get()
    assert (first.statement, first.source_contact_id, first.person_id) == (
        "Program officer.", c.pk, c.person_id)
    Contact.objects.filter(pk=c.pk).update(notes="Now a director.")
    mig.mirror_notes(django_apps, None)
    first.refresh_from_db()
    live = people.live_facts(c.person, "connect").get()
    assert live.statement == "Now a director." and live.supersedes == first
    assert first.superseded_at is not None


def test_patching_notes_mirrors_supersedes_and_clearing_retracts(world):
    owner = _client(world["owner"])
    c = _correspondent(world["ws"])
    url = f"/api/contacts/{c.pk}/"
    assert owner.patch(url, {"notes": "Program officer."}, content_type="application/json").status_code == 200
    c.refresh_from_db()
    [f1] = people.live_facts(c.person, "connect")
    assert f1.statement == "Program officer."

    owner.patch(url, {"notes": "Director of programs."}, content_type="application/json")
    [f2] = people.live_facts(c.person, "connect")
    assert f2.statement == "Director of programs." and f2.supersedes_id == f1.pk

    owner.patch(url, {"display_name": "Fatima"}, content_type="application/json")  # notes untouched
    assert list(people.live_facts(c.person, "connect")) == [f2]

    owner.patch(url, {"notes": ""}, content_type="application/json")
    assert not people.live_facts(c.person, "connect").exists()
    f2.refresh_from_db()
    assert f2.retracted_by == world["owner"]
    # The notes field itself is kept.
    c.refresh_from_db()
    assert c.notes == ""


def test_mirrored_notes_reach_the_envelope(world):
    from apps.agents.testing import admit_contacts

    ace = world["ace"]
    admit_contacts(ace)
    c = _correspondent(world["ws"], notes="Program officer at LLO Foo.")
    people.mirror_contact_notes(c)
    turn, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                                    origin_ref={"from": "fatima@llo.org", "subject": "hi"})
    block = caller_context.build(turn)["person"]
    assert [f["statement"] for f in block["facts"]] == ["Program officer at LLO Foo."]
