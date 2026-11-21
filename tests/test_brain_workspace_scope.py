"""The brain works at workspace level: `/api/w/<ws>/brain/…` covers `<ws>` and the workspaces below it.

dimagi → {connect, labs}. An owner of dimagi sees every descendant's aggregate and findings; connect's owner sees
connect's and never labs' (a sibling) or dimagi's (a parent); a member sees their own; a non-member nothing. A run
started for a workspace reads only its subtree, what it finds is that workspace's, and an owner cannot start one
above their own workspace. The schedule runs a system learner per workspace that has something new."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents.models import Agent, AgentProject
from apps.brain import associate, emit, reading, registry, schedule, sources
from apps.brain.models import BrainCapability, BrainOutput, BrainRun, BrainRunRead, LearningConsent
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.workspaces.models import Workspace, WorkspaceMembership as M
from tests.fixtures.brain_world import member, reads, user

pytestmark = pytest.mark.django_db


def _client(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w(settings):
    registry.ensure_builtin()
    registry.ensure_learners()
    jj, md, sib, mem, out = user("jj"), user("md"), user("sib"), user("mem"), user("out")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=jj, parent=dimagi)
    family = Workspace.objects.create(slug="family", display_name="Family", created_by=out)
    reads(dimagi)                                         # on at the root: connect and labs inherit it
    member(jj, dimagi, M.OWNER)
    member(md, connect, M.OWNER)
    member(md, dimagi)                                    # an editor of the parent: never its aggregate
    member(sib, labs, M.OWNER)
    member(mem, connect)
    member(out, family, M.OWNER)
    for u in (jj, md, sib, mem):
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    op_login = user("ada_login")
    member(op_login, dimagi)
    ada = Agent.objects.create(slug="ada", name="Ada", owner=jj, workspace=dimagi, user=op_login)
    cap = BrainCapability.objects.get(slug="system-learning")
    registry.update(cap, by=jj, operator=ada, entry="/canopy:brain-learn", enabled=True)
    cap.refresh_from_db()

    def chat(ws, by, *others):
        s = Session.objects.create(workspace=ws, created_by=by, origin=Session.ORIGIN_WEB, title=f"{ws.slug} work")
        for o in others:
            SessionParticipant.objects.create(session=s, user=o)
        sources.for_session(s)
        return s

    s_connect, s_labs, s_dimagi = chat(connect, mem, md), chat(labs, sib), chat(dimagi, jj)
    project = AgentProject.objects.create(agent=ada, ext_id="P1", name="Supply", handle="supply", workspace=connect)
    associate.put(sources.for_session(s_connect), project, how="dispatched", tier="core")
    runs = {ws: BrainRun.objects.create(capability=cap, subject_type="system", subject=ws, scope=ws, purpose="improve",
                                        state=BrainRun.DONE) for ws in ("dimagi", "connect", "labs")}
    finds = {ws: BrainOutput.objects.create(capability=cap, kind="finding", workspace_id=ws, subject_type="system",
                                            subject=ws, title=f"{ws} finding") for ws in ("dimagi", "connect", "labs")}
    return {"jj": jj, "md": md, "sib": sib, "mem": mem, "out": out, "cap": cap, "ada": ada, "project": project,
            "s_connect": s_connect, "s_labs": s_labs, "s_dimagi": s_dimagi, "runs": runs, "finds": finds}


def _state(u, ws):
    return _client(u).get(f"/api/w/{ws}/brain/state")


def _findings(u, ws):
    return {o["title"] for o in _client(u).get(f"/api/w/{ws}/brain/outputs/").json()}


def _run_scopes(u, ws):
    return {r["scope"] for r in _client(u).get(f"/api/w/{ws}/brain/state/runs").json()["runs"]}


def test_an_ancestor_owner_sees_every_descendant(w):
    body = _state(w["jj"], "dimagi").json()
    assert body["admin_workspaces"] == ["connect", "dimagi", "labs"]
    assert [c["workspace"] for c in body["consent"]["workspaces"]] == ["connect", "dimagi", "labs"]
    assert {s["workspace"]: s["scope"] for s in body["sources"]} == {
        "connect": "workspace", "dimagi": "workspace", "labs": "workspace"}
    assert body["links"] == [{"project": "supply", "name": "Supply", "workspace": "connect", "tier": "core",
                              "count": 1}]
    assert body["runs"]["done"] == 3 and _run_scopes(w["jj"], "dimagi") == {"dimagi", "connect", "labs"}
    assert _findings(w["jj"], "dimagi") == {"dimagi finding", "connect finding", "labs finding"}
    # Pinned lower, the same owner sees that subtree only.
    assert _findings(w["jj"], "connect") == {"connect finding"}


def test_a_sub_owner_sees_their_subtree_and_never_a_sibling_or_parent(w):
    body = _state(w["md"], "connect").json()
    assert body["admin_workspaces"] == ["connect"]
    assert [c["workspace"] for c in body["consent"]["workspaces"]] == ["connect"]
    assert _run_scopes(w["md"], "connect") == {"connect"}
    assert _findings(w["md"], "connect") == {"connect finding"}
    # At the parent they are an editor: their own subtree's aggregate, nothing of dimagi's or labs'.
    up = _state(w["md"], "dimagi").json()
    assert up["admin_workspaces"] == ["connect"]
    assert [c["workspace"] for c in up["consent"]["workspaces"]] == ["connect"]
    assert {s["workspace"] for s in up["sources"] if s["scope"] == "workspace"} == {"connect"}
    assert _run_scopes(w["md"], "dimagi") == {"connect"}
    assert _findings(w["md"], "dimagi") == {"connect finding"}
    assert _state(w["md"], "labs").status_code in (403, 404)                  # a sibling: not theirs at all


def test_a_sibling_owner_sees_only_their_own(w):
    assert _findings(w["sib"], "labs") == {"labs finding"}
    assert _run_scopes(w["sib"], "labs") == {"labs"}
    assert _state(w["sib"], "connect").status_code in (403, 404)


def test_a_member_sees_their_own_learning_and_no_aggregate(w):
    body = _state(w["mem"], "connect").json()
    assert body["admin_workspaces"] == [] and body["consent"]["workspaces"] == []
    assert sorted(body["consent"]["mine"]) == ["project", "system"]
    assert body["sources"] == [{"workspace": "connect", "scope": "yours", "open": 1, "withdrawn": 0, "sealed": {}}]
    assert body["runs"] == {"open": 0, "done": 0, "failed": 0} and _run_scopes(w["mem"], "connect") == set()
    assert _findings(w["mem"], "connect") == set()
    # A link only on a session they read, to a project they may read.
    links = _client(w["mem"]).get("/api/w/connect/brain/state/links").json()["links"]
    assert [x["session_id"] for x in links] == [str(w["s_connect"].pk)]


def test_a_non_member_sees_nothing(w):
    assert _state(w["out"], "connect").status_code in (403, 404)
    assert _state(w["out"], "dimagi").status_code in (403, 404)
    body = _state(w["out"], "family").json()
    assert body["workspaces"] == ["family"] and body["sources"] == [] and body["links"] == []


def test_a_run_reads_only_its_subtree_and_its_findings_are_its_workspace_s(w):
    BrainRun.objects.all().delete()
    r = _client(w["md"]).post("/api/w/connect/brain/capabilities/system-learning/runs/", {},
                              content_type="application/json")
    assert r.status_code == 200, r.content
    run = BrainRun.objects.get(pk=r.json()["id"])
    assert run.scope == "connect"
    assert [s.session_id for s in reading.candidates(run)] == [w["s_connect"].pk]
    top = _client(w["jj"]).post("/api/w/dimagi/brain/capabilities/system-learning/runs/", {},
                                content_type="application/json")
    wide = BrainRun.objects.get(pk=top.json()["id"])
    assert {s.session_id for s in reading.candidates(wide)} == {
        w["s_connect"].pk, w["s_labs"].pk, w["s_dimagi"].pk}
    # One open run per learner per workspace: a second at connect waits, a run at labs does not.
    again = _client(w["md"]).post("/api/w/connect/brain/capabilities/system-learning/runs/", {},
                                  content_type="application/json")
    assert again.status_code == 422 and f"run {run.pk}" in again.json()["detail"]
    # What the dimagi run finds about connect's session is dimagi's: connect's owner does not see it.
    BrainRunRead.objects.create(run=wide, source=sources.for_session(w["s_connect"]))
    out = emit.emit(wide, kind="finding", title="cross-team", body={"what": "x"},
                    source_ids=[sources.for_session(w["s_connect"]).pk])
    assert out.workspace_id == "dimagi"
    assert "cross-team" in _findings(w["jj"], "dimagi")
    assert "cross-team" not in _findings(w["md"], "connect") | _findings(w["md"], "dimagi")


def test_an_owner_cannot_start_a_run_above_their_workspace(w):
    r = _client(w["md"]).post("/api/w/dimagi/brain/capabilities/system-learning/runs/", {},
                              content_type="application/json")
    assert r.status_code == 403
    r = _client(w["mem"]).post("/api/w/connect/brain/capabilities/system-learning/runs/", {},
                               content_type="application/json")
    assert r.status_code == 403
    r = _client(w["jj"]).post("/api/w/connect/brain/capabilities/system-learning/runs/", {},
                              content_type="application/json")
    assert r.status_code == 200 and r.json()["scope"] == "connect"           # an ancestor's owner may
    assert _client(w["jj"]).post("/api/brain/capabilities/system-learning/runs/", {},
                                 content_type="application/json").status_code == 422   # it names its workspace


def test_the_schedule_runs_per_workspace_with_something_new(w):
    BrainRun.objects.all().delete()
    BrainCapability.objects.filter(pk=w["cap"].pk).update(schedule="daily")
    LearningConsent.objects.filter(user=w["sib"]).update(classes=["project"])  # labs has nothing for system
    started = schedule.run_due()
    assert sorted(r.scope for r in started) == ["connect", "dimagi"]
    assert all(r.subject == r.scope for r in started)
    assert schedule.run_due() == []                                            # within the interval
