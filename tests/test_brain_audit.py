"""Defects the pre-launch audit of the Canopy Brain found, each pinned by the test that failed on main.

* An output is derived from everything its run had read when it was written, not only what it cites: a removal of a
  source the run read and did not cite still reaches it (as it reaches the run's artifacts and conversation).
* The graph's would-be links are what the associator would make: none on a session someone in it never agreed to
  project learning, or in a workspace whose "Learning reads" is off.
* A learner's operator and the person who answers for it are shown only inside their own tenant tree.
* Taking a session out of learning is recorded in that session's workspace, never in another tenant's log.
* A run abandoned as stale takes its unclaimed turn with it, so no turn runs later for a run that is over."""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import consent, emit, graph, reading, runs, sources, state
from apps.brain.models import BrainOutput, BrainRun, LearningConsent
from apps.canopy_sessions.models import Message, Session
from apps.events.models import Event
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.brain_world import brain_world, member, reads, user

pytestmark = pytest.mark.django_db


def _chat(ws, who, text):
    s = Session.objects.create(workspace=ws, created_by=who, origin=Session.ORIGIN_WEB, title=f"{who.username}: {text}")
    Message.objects.create(session=s, role=Message.USER, turn_index=0, plaintext=text, author={"user_id": who.pk})
    sources.for_session(s)
    return s


@pytest.fixture()
def w():
    out = brain_world()
    out.pop("session").delete()
    ws, owner = out["ws"], out["owner"]
    out["op"] = Agent.objects.create(slug="ada", name="Ada", owner=owner, workspace=ws, user=user("ada_login"))
    member(out["op"].user, ws)
    sys = out["sys"]
    sys.purposes, sys.output_kinds, sys.operator, sys.entry, sys.registered_by, sys.subject_type = (
        ["improve"], ["finding"], out["op"], "/canopy:brain-learn", owner, "system")
    sys.save()
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    out["s1"] = _chat(ws, out["alice"], "the runner idles")
    out["s2"] = _chat(ws, out["bob"], "the CLI times out")
    return out


def _run(w):
    return runs.start(w["sys"], scope="acme", accountable=w["owner"])


# ---- an output is derived from everything its run read --------------------------------------------------------

def test_removing_a_source_the_run_read_but_did_not_cite_reaches_the_output(w, django_capture_on_commit_callbacks):
    run = _run(w)
    page = reading.read_next(run, limit=5)
    assert {s["source"] for s in page["sources"]} == {w["s1"].brain_sources.get().pk, w["s2"].brain_sources.get().pk}
    out = emit.emit(run, kind="finding", title="CLI times out", body={"what": "timeouts"},
                    source_ids=[w["s2"].brain_sources.get().pk])
    with django_capture_on_commit_callbacks(execute=True):
        consent.exclude(w["alice"], w["s1"].brain_sources.get())             # read in the run, not cited
    out.refresh_from_db()
    assert out.state != BrainOutput.ACTIVE and out.body == {} and out.title == ""


def test_an_output_still_shows_only_the_sessions_it_cites(w):
    from apps.brain import api_admin

    run = _run(w)
    reading.read_next(run, limit=5)
    s2 = w["s2"].brain_sources.get()
    out = emit.emit(run, kind="finding", title="t", body={"x": 1}, source_ids=[s2.pk])
    cited, _named, _by_source = api_admin._cited(w["owner"], [out])
    assert cited == {str(out.pk): [w["s2"].pk]}


# ---- the graph's would-be links are the associator's -------------------------------------------------------------

def _proposed(viewer, slugs=("acme",)):
    g = graph.build(viewer, list(slugs), proposed=True, as_person=True)
    return [e for e in g["edges"] if e["proposed"]]


@pytest.fixture()
def linked(w):
    from apps.agents.models import AgentProject
    from apps.brain.models import ProjectSignature

    project = AgentProject.objects.create(workspace=w["ws"], name="Supply", handle="supply",
                                          visibility=AgentProject.WORKSPACE)
    ProjectSignature.objects.create(project=project, name_terms=["runner"])
    return project


def test_the_graph_proposes_no_link_for_a_session_someone_in_it_never_agreed_to(w, linked):
    LearningConsent.objects.filter(user=w["alice"]).update(classes=["system"])      # not project
    assert _proposed(w["alice"]) == []


def test_the_graph_proposes_no_link_where_learning_reads_are_off(w, linked):
    reads(w["ws"], False)
    assert _proposed(w["alice"]) == []


def test_the_graph_still_proposes_where_everyone_agreed_and_reads_are_on(w, linked):
    assert [(e["source"], e["target"]) for e in _proposed(w["alice"])] == [(f"session:{w['s1'].pk}",
                                                                            f"project:{linked.pk}")]


# ---- tenants never meet on the brain page -------------------------------------------------------------------------

def test_another_tenant_never_sees_who_operates_or_answers_for_a_learner(w):
    other_owner = user("zed")
    other = Workspace.objects.create(slug="zeta", display_name="Zeta", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=other, role=WorkspaceMembership.OWNER)
    row = next(r for r in state.learners(other_owner, "zeta") if r["slug"] == "sys")
    assert row["operator"] == "" and row["accountable"] == "" and row["entry"] == ""
    mine = next(r for r in state.learners(w["alice"], "acme") if r["slug"] == "sys")
    assert mine["operator"] == "ada" and mine["accountable"]
    r = Client()
    r.force_login(other_owner)
    caps = {c["slug"]: c for c in r.get("/api/brain/capabilities/").json()}
    assert caps["sys"]["operator"] == "" and caps["sys"]["entry"] == ""


# ---- a removal is recorded where the session lives ----------------------------------------------------------------

def test_taking_a_session_out_is_logged_only_in_its_own_workspace(w):
    other = Workspace.objects.create(slug="zeta", display_name="Zeta", created_by=w["owner"])
    member(w["alice"], other)
    consent.exclude(w["alice"], w["s1"].brain_sources.get())
    logged = set(Event.objects.filter(source=consent.SOURCE, kind=consent.SOURCE_WITHDRAWN)
                 .values_list("workspace_id", flat=True))
    assert logged == {"acme"}


# ---- a stale run takes its turn with it ---------------------------------------------------------------------------

def test_abandoning_a_stale_run_cancels_its_unclaimed_turn(w):
    run = _run(w)
    turn = Turn.objects.get(chat_session=run.session)
    BrainRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - dt.timedelta(days=1))
    assert runs.abandon_stale() == 1
    turn.refresh_from_db()
    assert turn.status in Turn.TERMINAL


# ---- a moved session takes nothing of its old workspace with it --------------------------------------------------

def test_a_session_moved_to_another_workspace_is_no_longer_read_through_its_old_project():
    from apps.brain import associate
    from apps.canopy_sessions import access, filing
    from tests.fixtures.brain_project_leg import project_leg_world

    pw = project_leg_world()
    s = pw["session"]
    other_owner = user("bowner")
    beta = Workspace.objects.create(slug="beta", display_name="Beta", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=beta, role=WorkspaceMembership.OWNER)
    reads(beta)
    for u in (pw["alice"], pw["bob"], pw["reader"]):
        member(u, beta)
    assert access.can_read(pw["reader"], s, brain=True)                 # tied to an acme project, in acme
    filing.move(s, "beta", by=pw["alice"])
    s.refresh_from_db()
    assert s.workspace_id == "beta" and not access.can_read(pw["reader"], s, brain=True)
    associate.associate(sources.for_session(s))
    assert list(pw["source"].links.values_list("subject", flat=True)) == []
    assert s.brain_sources.get().workspace_id == "beta"


# ---- the disclosure summary outlives the event log's pruning --------------------------------------------------------

def test_a_person_s_disclosure_summary_outlives_the_event_log_s_pruning():
    from apps.brain import disclosure, readers
    from apps.events import services as events

    bw = brain_world()
    readers.record_read(reader=bw["carol"], capability="project-readers", purpose="read", session=bw["session"])
    assert disclosure.summary(bw["alice"])
    Event.objects.filter(source="brain.read").update(last_seen_at=timezone.now() - dt.timedelta(days=31))
    events.prune(older_than=dt.timedelta(days=30))                       # the daily `events.prune` tick
    assert disclosure.summary(bw["alice"])


def test_who_once_agreed_to_a_class_outlives_the_event_log_s_pruning():
    from apps.brain import cascade
    from apps.events import services as events
    from tests.fixtures.brain_world import agree

    bw = brain_world()
    agree(bw["alice"], "memory", "project")
    agree(bw["alice"], "project")                                        # drops memory
    Event.objects.all().update(last_seen_at=timezone.now() - dt.timedelta(days=31))
    events.prune(older_than=dt.timedelta(days=30))
    assert cascade.ever_agreed(bw["alice"].pk, "memory")


# ---- a session skipped before people agreed is looked at again once they do -------------------------------------

def _dispatched(bw):
    import uuid

    from apps.agents.models import AgentProject, AgentTask

    lead = Agent.objects.create(slug="eva", name="Eva", owner=bw["owner"], workspace=bw["ws"])
    proj = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    task = AgentTask.objects.create(agent=lead, ext_id="T1", title="x", project=proj)
    Turn.objects.create(agent=lead, origin=Turn.ORIGIN_API, prompt="p", idempotency_key=uuid.uuid4().hex,
                        raised_from_task=task, canopy_session=bw["session"])
    return proj


@pytest.mark.parametrize("last", ["reads", "consent"])
def test_a_session_looked_at_before_reads_or_consent_is_looked_at_again_once_both_hold(last):
    from apps.brain import associate
    from apps.brain.models import SourceLink
    from tests.fixtures.brain_world import agree

    bw = brain_world()
    _dispatched(bw)
    reads(bw["ws"], False)
    associate.run_due()                                                  # nobody agreed, reads off: nothing
    assert not SourceLink.objects.exists()
    if last == "consent":
        reads(bw["ws"], True)
        associate.run_due()
    for u in (bw["alice"], bw["bob"]):
        agree(u, "project")
    if last == "reads":
        associate.run_due()
        reads(bw["ws"], True)
    associate.run_due()
    assert SourceLink.objects.filter(source=bw["source"], tier="core").exists()


# ---- every person's rejection sticks ---------------------------------------------------------------------------------

def test_a_second_person_s_rejection_sticks_when_the_first_takes_theirs_back():
    from apps.brain import associate
    from apps.brain.models import SourceLink
    from tests.fixtures.brain_world import agree

    bw = brain_world()
    for u in (bw["alice"], bw["bob"]):
        agree(u, "project")
    proj = _dispatched(bw)
    src = bw["source"]
    associate.declare(src, proj, tier="rejected", by=bw["alice"])
    associate.declare(src, proj, tier="rejected", by=bw["bob"])
    associate.undeclare(src, proj, by=bw["alice"])
    link = SourceLink.objects.get(source=src)
    assert (link.how, link.tier, link.by_id) == ("declared", "rejected", bw["bob"].pk)
    associate.undeclare(src, proj, by=bw["bob"])
    assert SourceLink.objects.get(source=src).tier == "core"           # nobody rejects it now: the layers again


def test_the_route_accepts_a_second_person_s_rejection():
    from tests.fixtures.brain_world import agree

    bw = brain_world()
    for u in (bw["alice"], bw["bob"]):
        agree(u, "project")
    _dispatched(bw)
    url = f"/api/me/learning/sessions/{bw['session'].pk}/projects/supply/"
    for u in (bw["alice"], bw["bob"]):
        c = Client()
        c.force_login(u)
        r = c.put(url, data={"tier": "rejected"}, content_type="application/json")
        assert r.status_code == 200, (u.username, r.content)
