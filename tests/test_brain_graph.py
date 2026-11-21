"""`GET /api/workspaces/{slug}/graph`: typed nodes and edges, each one the viewer already sees elsewhere.

Who sees what (a non-member, a member in the session, a leg-7-only project reader, an agent's login, a coach, a
workspace owner), sealed sessions drawn as the network page draws a thread, the brain's would-be links storing
nothing, and the cost staying flat as sessions grow."""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.brain import associate, graph, sources
from apps.brain.models import BrainCapability, BrainOutput, OutputLineage, ProjectSignature, ReportingLine, SourceLink
from apps.canopy_sessions import access, coaching
from apps.canopy_sessions.models import CoachingOptIn, Session, SessionParticipant
from apps.contacts.models import Contact
from apps.harness import runner_requirements as rr
from apps.harness.models import Turn
from tests.fixtures.brain_project_leg import client, project_leg_world
from tests.fixtures.brain_world import member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

URL = "/api/workspaces/acme/graph"


@pytest.fixture()
def w(settings):
    out = project_leg_world()
    ws, alice, bob, carol = out["ws"], out["alice"], out["bob"], out["carol"]
    Session.objects.filter(pk=out["session"].pk).update(title="deploy supply")
    # A task of the project that dispatched a turn in the session.
    out["task"] = AgentTask.objects.create(workspace=ws, number=1, key="ACM-1", title="Ship it", project=out["project"])
    Turn.objects.create(agent=out["lead"], canopy_session=out["session"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                        prompt="x", idempotency_key="k1", initiator_user=alice, raised_from_task=out["task"])
    # A sensitive session of alice's, declared to the project anyway.
    out["sensitive"] = Session.objects.create(workspace=ws, created_by=alice, origin=Session.ORIGIN_WEB,
                                              title="payroll", metadata={rr.METADATA_KEY: ["sensitive_data"]})
    associate.put(sources.for_session(out["sensitive"]), out["project"], how="declared", tier="core", by=alice)
    # A contact's conversation with the lead agent, which alice was added to.
    contact = Contact.objects.create(workspace=ws, email="llo@example.org")
    out["contact_session"] = Session.objects.create(workspace=ws, contact=contact, agent=out["lead"],
                                                    origin=Session.ORIGIN_WEB, title="llo asks")
    SessionParticipant.objects.create(session=out["contact_session"], user=alice)
    # A personal project carol leads, with a (hand-written) link from alice's session.
    out["personal"] = AgentProject.objects.create(workspace=ws, name="Carol's secret", handle="carols",
                                                  visibility=AgentProject.PERSONAL, owner_user=carol)
    SourceLink.objects.create(source=out["source"], subject=str(out["personal"].pk), how="declared", tier="core",
                              by=alice)
    # Bob reports to the owner; carol opted into coaching, with a person coach for the workspace.
    ReportingLine.objects.create(workspace=ws, person=bob, manager=out["owner"], set_by=out["owner"])
    out["coach"] = user("coachy")
    member(out["coach"], ws)
    coaching.designate(ws, user=out["coach"], by=out["owner"])
    CoachingOptIn.objects.create(user=carol)
    out["carols"] = Session.objects.create(workspace=ws, created_by=carol, origin=Session.ORIGIN_WEB, title="carol's")
    # An output about alice, learned from the session.
    o = BrainOutput.objects.create(capability=_cap(), kind="note", workspace=ws,
                                   subject_type="person", subject=str(alice.pk), title="alice ships")
    OutputLineage.objects.create(output_id=str(o.pk), source=out["source"])
    out["output"] = o
    # An agent with a login of its own.
    login = user("hal_login")
    member(login, ws)
    out["hal"] = Agent.objects.create(slug="hal", name="Hal", owner=out["owner"], workspace=ws, user=login)
    out["hal_login"] = login
    out["hal_work"] = Session.objects.create(workspace=ws, agent=out["hal"], origin=Session.ORIGIN_RUNNER,
                                             title="hal's own")
    return out


def _cap():
    return BrainCapability.objects.get(slug="coach")


def _get(c, **params):
    r = c.get(URL, params)
    assert r.status_code == 200, r.content
    return r.json()


def _ids(body, typ=None):
    return {n["id"] for n in body["nodes"] if typ is None or n["type"] == typ}


def _edges(body, kind=None):
    return {(e["source"], e["target"]) for e in body["edges"] if kind is None or e["kind"] == kind}


def test_a_non_member_gets_nothing(w):
    stranger = user("stranger")
    assert client(stranger).get(URL).status_code == 404
    assert Client().get(URL).status_code in (401, 403)


def test_a_person_in_the_session_sees_it_its_people_agent_task_and_project(w):
    body = _get(client(w["alice"]))
    s, p = f"session:{w['session'].pk}", f"project:{w['project'].pk}"
    assert {s, p, "task:ACM-1", "agent:eva", f"person:{w['alice'].pk}", f"person:{w['bob'].pk}"} <= _ids(body)
    assert (f"person:{w['alice'].pk}", s) in _edges(body, "started")
    assert (f"person:{w['bob'].pk}", s) in _edges(body, "participates")
    assert ("agent:eva", s) in _edges(body, "ran")
    assert ("task:ACM-1", s) in _edges(body, "dispatched_from")
    linked = [e for e in body["edges"] if e["kind"] == "linked" and e["source"] == s and e["target"] == p]
    assert len(linked) == 1 and linked[0]["tier"] == "core" and linked[0]["how"] == "dispatched"
    assert not linked[0]["proposed"]
    # Not a session she is not in, and never a personal project she may not read, link or no link.
    assert f"session:{w['carols'].pk}" not in _ids(body)
    assert f"project:{w['personal'].pk}" not in _ids(body)
    # Her own learning output and what it was learned from.
    assert (f"output:{w['output'].pk}", s) in _edges(body, "derived_from")
    assert (f"output:{w['output'].pk}", f"person:{w['alice'].pk}") in _edges(body, "about")


def test_a_sealed_session_is_drawn_as_the_network_draws_a_thread_and_no_contact_is_a_node(w):
    body = _get(client(w["alice"]))
    sens = next(n for n in body["nodes"] if n["id"] == f"session:{w['sensitive'].pk}")
    assert sens["sensitive"] is True and sens["label"] == "Sensitive conversation"
    assert "payroll" not in str(body)
    sealed = {f"session:{w['sensitive'].pk}", f"session:{w['contact_session'].pk}"}
    assert not [e for e in body["edges"] if e["kind"] == "linked" and e["source"] in sealed]
    assert f"session:{w['contact_session'].pk}" in _ids(body)
    assert not [n for n in body["nodes"] if n["type"] not in graph.NODE_TYPES]
    assert "llo@example.org" not in str(body)


def test_a_leg_7_only_reader_sees_the_project_and_not_the_session(w):
    assert access.readable_ids(w["reader"], [w["session"].pk], brain=True) == {w["session"].pk}   # the leg is open
    body = _get(client(w["reader"]))
    assert f"project:{w['project'].pk}" in _ids(body)
    assert _ids(body, "session") == set()
    assert not _edges(body, "linked")
    assert "deploy supply" not in str(body)


def test_an_agent_login_sees_its_own_work_and_no_person_only_layer(w):
    c = Client(**bearer_headers(pat_token(w["hal_login"])))
    body = _get(c)
    assert _ids(body, "session") == {f"session:{w['hal_work'].pk}"}
    assert body["reporting"] is False and not _edges(body, "reports_to")
    assert c.get(URL, {"include": "proposed"}).status_code == 403


def test_reporting_lines_go_to_who_the_reporting_route_shows_them_to(w):
    line = (f"person:{w['bob'].pk}", f"person:{w['owner'].pk}")
    assert line in _edges(_get(client(w["owner"])), "reports_to")                  # an owner: every line
    assert line in _edges(_get(client(w["bob"])), "reports_to")                    # someone in it
    assert line in _edges(_get(Client(**bearer_headers(pat_token(w["bob"])))), "reports_to")   # a hand-minted PAT
    assert line not in _edges(_get(client(w["carol"])), "reports_to")              # anyone else: not
    turn = client(w["bob"]).get(URL, HTTP_X_CANOPY_TURN_ID="x")                     # a request naming a turn
    assert turn.status_code == 200 and turn.json()["reporting"] is False and not _edges(turn.json(), "reports_to")


def test_a_coach_sees_their_roster_and_everyone_sees_the_program(w):
    coach, carol = f"person:{w['coach'].pk}", f"person:{w['carol'].pk}"
    body = _get(client(w["coach"]))
    assert (coach, carol) in _edges(body, "coaches")
    assert (coach, "workspace:acme") in _edges(body, "coaches")
    assert f"session:{w['carols'].pk}" in _ids(body)                                # leg 5, as the session list
    assert (carol, f"session:{w['carols'].pk}") in _edges(body, "started")
    # Carol sees who coaches her; bob sees the program and not who opted in.
    assert (coach, carol) in _edges(_get(client(w["carol"])), "coaches")
    bob = _get(client(w["bob"]))
    assert (coach, "workspace:acme") in _edges(bob, "coaches") and (coach, carol) not in _edges(bob, "coaches")


def test_proposed_links_are_the_brains_compute_marked_and_written_nowhere(w, django_capture_on_commit_callbacks):
    ProjectSignature.objects.create(project=w["project"], name_terms=["payroll", "carol"])
    SessionParticipant.objects.create(session=w["carols"], user=w["alice"])
    # The associator looks only at a session everyone in it agreed to project learning for, and so does its preview.
    assert not [e for e in graph.build(w["alice"], ["acme"], proposed=True, as_person=True)["edges"] if e["proposed"]]
    from apps.brain.models import LearningConsent

    LearningConsent.objects.create(user=w["carol"], classes=["project"])
    sources.for_session(w["carols"])                       # the associator's tick makes a session's source first
    before = (SourceLink.objects.count(), list(SourceLink.objects.values_list("pk", "tier", "how")))
    with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
        body = graph.build(w["alice"], ["acme"], proposed=True, as_person=True)
    assert _writes(ctx) == [] and callbacks == []
    assert (SourceLink.objects.count(), list(SourceLink.objects.values_list("pk", "tier", "how"))) == before
    proposed = [e for e in body["edges"] if e["proposed"]]
    assert {(e["source"], e["tier"], e["how"]) for e in proposed} == {
        (f"session:{w['carols'].pk}", "candidate", "signals")}            # the dispatched link is stored already
    assert not [e for e in proposed if e["source"] == f"session:{w['sensitive'].pk}"]      # never a sealed one
    # Over the wire: the person only, and still nothing written but the sign-in's own rows.
    for c in (client(w["alice"]), Client(**bearer_headers(pat_token(w["alice"])))):
        with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
            r = c.get(URL, {"include": "proposed"})
        assert r.status_code == 200 and r.json()["proposed"] is True
        assert all(q.split(" ", 2)[1] in ('"personal_tokens"', '"django_session"') for q in _writes(ctx)), _writes(ctx)
        assert callbacks == []
    assert client(w["alice"]).get(URL, {"include": "proposed"}, HTTP_X_CANOPY_TURN_ID="x").status_code == 403


def test_propose_many_is_propose_for_each_session(w):
    ProjectSignature.objects.create(project=w["project"], name_terms=["carol", "deploy"])
    rows = list(Session.objects.filter(workspace=w["ws"]))
    many = associate.propose_many(rows)

    def key(ps):
        return sorted((p.project.pk, p.how, p.tier, p.evidence) for p in ps)

    for s in rows:
        assert key(many[s.pk]) == key(associate.propose(s, s.workspace_id)), s.title


def test_the_cost_does_not_grow_with_the_sessions(w):
    def count():
        with CaptureQueriesContext(connection) as ctx:
            graph.build(w["alice"], ["acme"], proposed=True, as_person=True)
        return len(ctx.captured_queries)

    few = count()
    for i in range(12):
        s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, title=f"s{i}")
        SessionParticipant.objects.create(session=s, user=w["bob"])
        Turn.objects.create(agent=w["lead"], canopy_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="x",
                            idempotency_key=f"k-{i}", initiator_user=w["bob"], raised_from_task=w["task"])
        associate.put(sources.for_session(s), w["project"], how="signals", tier="candidate")
    assert count() == few


def test_the_window_and_the_caps(w, monkeypatch):
    monkeypatch.setattr(graph, "MAX_SESSIONS", 1)
    body = _get(client(w["alice"]))
    assert len(_ids(body, "session")) == 1 and body["truncated"] and body["notes"]
    assert _get(client(w["alice"]), days=500)["days"] == graph.MAX_DAYS


def _writes(ctx) -> list[str]:
    return [q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


def test_every_brain_read_goes_through_the_one_sourced_function(w):
    """The brain's rows are read in `graph.brain_reads` alone, so a snapshot source is one change there."""
    import ast
    import inspect

    brain_tables = {"SourceLink", "SourceParticipant", "ReportingLine", "OutputLineage", "BrainOutput"}
    tree = ast.parse(inspect.getsource(graph))
    outside = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name != "brain_reads"):
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (
                    (node.value.id in brain_tables and node.attr == "objects")
                    or (node.value.id == "outputs" and node.attr == "visible_to")):
                outside.append(f"{fn.name}: {node.value.id}.{node.attr}")
    assert outside == []
    with pytest.raises(ValueError):
        graph.brain_reads(w["alice"], ["acme"], sessions=[], unsealed=[], as_person=True, manager_leg=False,
                          source="preview:1")
    live = graph.build(w["alice"], ["acme"], as_person=True, source=graph.LIVE)
    assert live["edges"] == graph.build(w["alice"], ["acme"], as_person=True)["edges"]
