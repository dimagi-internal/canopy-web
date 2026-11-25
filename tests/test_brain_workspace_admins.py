"""A workspace's owners AND admins manage its learning (`perms.BRAIN_MANAGE`), and "Learning reads" is a workspace setting.

dimagi → {connect, labs}. At connect, every brain capability of its page (its `system` findings, Run now, configuring a
learner, Learning reads) is open to connect's owner and admin and to dimagi's owner and admin, as a person; never to an
editor of connect, a sibling's (labs') admin, connect's admin acting at dimagi (up the tree), an agent's login, or an
MCP call by a person; an admin agent in its own turn is `tests/test_brain_agent_admin.py`. "Learning reads" is off unless set, inherits from the nearest ancestor that sets it, and is asked of a source's
own workspace by `eligibility.explain` and `eligible_sources` alike; every change is a `SettingChange` the page lists."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.brain import eligibility, learning_reads, outputs, registry, sources, state
from apps.brain.models import (
    BrainCapability,
    BrainOutput,
    BrainRun,
    BrainTarget,
    LearningConsent,
    WorkspaceLearningReads,
)
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.events.models import SettingChange
from apps.mcp import api_tools
from apps.workspaces.models import Workspace
from apps.workspaces.models import WorkspaceMembership as M
from tests.fixtures.brain_world import aim, member, reads, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

ALLOWED = ("connect_owner", "connect_admin", "dimagi_owner", "dimagi_admin", "root")
REFUSED = ("connect_editor", "labs_admin", "agent_login")


def _client(u) -> Client:
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def w():
    registry.ensure_builtin()
    registry.ensure_learners()
    people = {name: user(name) for name in ("jj", "connect_owner", "connect_admin", "connect_editor", "dimagi_owner",
                                            "dimagi_admin", "labs_admin", "alice", "bob")}
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=people["jj"])
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=people["jj"], parent=dimagi)
    labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=people["jj"], parent=dimagi)
    member(people["jj"], dimagi, M.OWNER)
    member(people["dimagi_owner"], dimagi, M.OWNER)
    member(people["dimagi_admin"], dimagi, M.ADMIN)
    member(people["connect_owner"], connect, M.OWNER)
    member(people["connect_admin"], connect, M.ADMIN)
    member(people["connect_editor"], connect)
    member(people["labs_admin"], labs, M.ADMIN)
    member(people["labs_admin"], connect)                      # an editor of connect too: still not its manager
    for u in (people["alice"], people["bob"]):
        member(u, connect)
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    root = user("root")
    root.is_superuser = True
    root.save()
    member(root, connect)
    member(root, dimagi)                                       # answers for system-learning, whose operator is there
    people["root"] = root
    login = user("cee_login")
    member(login, connect, M.ADMIN)                            # an agent's login holding admin: still never a person
    people["agent_login"] = login
    cee = Agent.objects.create(slug="cee", name="Cee", owner=people["connect_owner"], workspace=connect, user=login)
    lab_agent = Agent.objects.create(slug="lab", name="Lab", owner=people["jj"], workspace=labs)
    ada = Agent.objects.create(slug="ada", name="Ada", owner=people["jj"], workspace=dimagi)
    s = Session.objects.create(workspace=connect, created_by=people["alice"], origin=Session.ORIGIN_WEB, title="work")
    SessionParticipant.objects.create(session=s, user=people["bob"])
    Message.objects.create(session=s, role=Message.USER, turn_index=0, plaintext="the runner idles",
                           author={"user_id": people["alice"].pk})
    source = sources.for_session(s)
    system = BrainCapability.objects.get(slug="system-learning")
    registry.update(system, by=root, operator=ada, entry="/canopy:brain-learn", enabled=True)
    aim(BrainCapability.objects.get(slug="system-learning"), connect, by=root)
    finding = BrainOutput.objects.create(capability=system, kind="finding", workspace=connect, subject_type="system",
                                         subject="connect", title="connect finding")
    return {**people, "dimagi": dimagi, "connect": connect, "labs": labs, "cee": cee, "lab_agent": lab_agent,
            "ada": ada, "session": s, "source": source, "system": BrainCapability.objects.get(slug="system-learning"),
            "finding": finding}


def _put_reads(u, ws, on, **extra):
    return (extra.pop("client", None) or _client(u)).put(f"/api/w/{ws}/brain/learning-reads", {"on": on},
                                                         content_type="application/json", **extra)


def _run(u, ws="connect", target=None):
    t = target or BrainTarget.objects.get(workspace_id="connect", capability__slug="system-learning")
    return _client(u).post(f"/api/w/{ws}/brain/targets/{t.pk}/runs", {}, content_type="application/json")


def _configure(u, ws, slug, **body):
    return _client(u).patch(f"/api/w/{ws}/brain/learners/{slug}/", body, content_type="application/json")


def _findings(u, ws="connect"):
    r = _client(u).get(f"/api/w/{ws}/brain/outputs/")
    return {o["title"] for o in r.json()} if r.status_code == 200 else set()


# ---- each capability, one test per role ----------------------------------------------------------------------

@pytest.mark.parametrize("who", ALLOWED)
def test_a_manager_sees_the_system_findings(w, who):
    assert _findings(w[who]) == {"connect finding"}


@pytest.mark.parametrize("who", REFUSED)
def test_anyone_else_never_sees_the_system_findings(w, who):
    assert _findings(w[who]) == set()


def test_system_findings_only_to_the_person_never_an_agent_or_mcp(w):
    # The leg opens only when the request is the person (`manager_leg_open`: not MCP, not a turn's token).
    assert w["finding"] in outputs.visible_to(w["connect_admin"], as_person=True)
    assert w["finding"] not in outputs.visible_to(w["connect_admin"])
    assert w["finding"] not in outputs.visible_to(w["agent_login"], as_person=True)
    r = Client().get("/api/w/connect/brain/outputs/", **bearer_headers(pat_token(w["agent_login"])))
    assert r.status_code != 200 or r.json() == []


@pytest.mark.parametrize("who", ALLOWED)
def test_a_manager_turns_learning_reads_on_and_off(w, who):
    r = _put_reads(w[who], "connect", True)
    assert r.status_code == 200, r.content
    assert r.json()["on"] is True and r.json()["own"] is True and r.json()["source"] == "connect"
    assert learning_reads.is_on("connect")
    assert _put_reads(w[who], "connect", None).json()["own"] is None
    assert not learning_reads.is_on("connect")


@pytest.mark.parametrize("who", REFUSED)
def test_anyone_else_may_not_change_learning_reads(w, who):
    assert _put_reads(w[who], "connect", True).status_code in (403, 404)
    assert not WorkspaceLearningReads.objects.exists()


@pytest.mark.parametrize("who", ALLOWED)
def test_a_manager_runs_a_learner_now(w, who):
    reads(w["connect"])
    r = _run(w[who])
    assert r.status_code == 200, r.content
    assert BrainRun.objects.get(pk=r.json()["id"]).scope == "connect"


@pytest.mark.parametrize("who", REFUSED)
def test_anyone_else_may_not_run_a_learner(w, who):
    reads(w["connect"])
    assert _run(w[who]).status_code in (403, 404)
    assert not BrainRun.objects.exists()


@pytest.mark.parametrize("who", ALLOWED)
def test_a_manager_enables_and_configures_a_learner_operated_here(w, who):
    r = _configure(w[who], "connect", "project-learning", operator="cee", entry="/canopy:brain-learn", enabled=True)
    assert r.status_code == 200, r.content
    cap = BrainCapability.objects.get(slug="project-learning")
    assert cap.enabled and cap.operator == w["cee"] and cap.entry == "/canopy:brain-learn"
    assert registry.accountable(cap) == w[who]                          # nobody answered for it: now they do
    assert _configure(w[who], "connect", "project-learning", enabled=False).json()["enabled"] is False


@pytest.mark.parametrize("who", REFUSED)
def test_anyone_else_may_not_configure_a_learner(w, who):
    r = _configure(w[who], "connect", "project-learning", operator="cee", enabled=True)
    assert r.status_code in (403, 404)
    assert not BrainCapability.objects.get(slug="project-learning").enabled


# ---- never up the tree or sideways -------------------------------------------------------------------------

def test_a_sub_workspace_admin_manages_nothing_above_it(w):
    a = w["connect_admin"]
    assert _put_reads(a, "dimagi", True).status_code in (403, 404)
    assert _run(a, ws="dimagi").status_code in (403, 404)
    assert _configure(a, "dimagi", "project-learning", operator="ada").status_code in (403, 404)
    # The learner operated at dimagi is configured at dimagi, not from connect.
    assert _configure(a, "connect", "system-learning", enabled=False).status_code == 403
    assert BrainCapability.objects.get(slug="system-learning").enabled
    assert "dimagi" not in {s["workspace"] for s in _client(a).get("/api/w/connect/brain/state").json()["sources"]}


def test_a_learner_is_given_only_an_operator_of_this_subtree(w):
    r = _configure(w["connect_admin"], "connect", "project-learning", operator="lab")    # a sibling's agent
    assert r.status_code == 422 and "no agent" in r.json()["detail"]
    r = _configure(w["connect_admin"], "connect", "project-learning", operator="ada")    # the parent's agent
    assert r.status_code == 422
    assert BrainCapability.objects.get(slug="project-learning").operator is None


def test_run_now_refuses_an_operator_that_lives_below_or_beside(w):
    reads(w["dimagi"])
    BrainCapability.objects.filter(slug="system-learning").update(operator=w["lab_agent"])
    r = _run(w["connect_owner"])
    assert r.status_code == 422 and "operator lives outside" in r.json()["detail"]
    BrainCapability.objects.filter(slug="system-learning").update(operator=w["cee"])
    top = aim(BrainCapability.objects.get(slug="system-learning"), w["dimagi"], by=w["root"])
    r = _run(w["dimagi_owner"], ws="dimagi", target=top)                 # a child's agent reading its parent
    assert r.status_code == 422 and "operator lives outside" in r.json()["detail"]


def test_the_management_routes_refuse_an_agents_stored_token(w):
    for name in ("set_learning_reads", "configure_learner", "run_learning_target", "add_learning_target",
                 "remove_learning_target", "brain_state"):
        assert name not in api_tools.EXCLUDED                    # MCP tools, for admin agents: the route decides
    r = _put_reads(None, "connect", True, client=Client(), **bearer_headers(pat_token(w["agent_login"])))
    assert r.status_code == 403


# ---- the built-in project readers --------------------------------------------------------------------------

def test_the_project_readers_never_offer_run_now_and_are_never_configured_here(w):
    reads(w["connect"])
    body = _client(w["connect_owner"]).get("/api/w/connect/brain/state").json()
    learners = {x["slug"]: x for x in body["learners"]}
    assert learners["project-readers"]["runnable"] is False and learners["project-readers"]["configurable"] is False
    assert learners["system-learning"]["runnable"] is True
    assert learners["coaching"]["runnable"] is False
    assert body["can_manage"] is True
    r = _client(w["connect_owner"]).post("/api/w/connect/brain/targets", {"learner": "project-readers"},
                                         content_type="application/json")
    assert r.status_code == 422 and "built in" in r.json()["detail"]
    assert _configure(w["root"], "connect", "project-readers", enabled=False).status_code == 403
    # A per-person learner is canopy-wide too: never switched on from one workspace.
    assert learners["coaching"]["configurable"] is False
    assert _configure(w["connect_owner"], "connect", "agent-memory", enabled=True).status_code == 403
    assert not BrainCapability.objects.get(slug="agent-memory").enabled


# ---- the setting: inheritance and eligibility --------------------------------------------------------------

def test_learning_reads_are_off_until_set_and_inherit_from_the_nearest_ancestor(w):
    assert not learning_reads.is_on("connect") and learning_reads.on_slugs() == set()
    reads(w["dimagi"])
    assert learning_reads.effective("connect") == learning_reads.Effective(True, "dimagi", None)
    assert learning_reads.on_slugs() == {"dimagi", "connect", "labs"}
    reads(w["connect"], False)                                            # the nearest setting wins
    assert learning_reads.effective("connect") == learning_reads.Effective(False, "connect", False)
    assert learning_reads.on_slugs() == {"dimagi", "labs"}
    reads(w["dimagi"], False)
    reads(w["connect"], True)                                             # a child on under a parent off
    assert learning_reads.on_slugs() == {"connect"}
    reads(w["connect"], None)                                             # unset: inherits again
    assert learning_reads.effective("connect") == learning_reads.Effective(False, "dimagi", None)


def test_eligibility_asks_the_sources_own_workspace(w):
    src, cap = w["source"], w["system"]
    assert eligibility.explain(src, cap, "improve").reason == "learning_reads_off"
    assert not eligibility.eligible_sources(cap, "improve").exists()
    assert not eligibility.everyone_agreed(src, "system")
    reads(w["labs"])                                                      # a sibling on: still not connect's
    assert not eligibility.eligible(src, cap, "improve")
    reads(w["dimagi"])                                                    # the parent on: connect inherits it
    assert eligibility.eligible(src, cap, "improve")
    assert list(eligibility.eligible_sources(cap, "improve")) == [src]
    assert eligibility.everyone_agreed(src, "system")
    reads(w["connect"], False)                                            # and connect may still say no for itself
    assert eligibility.explain(src, cap, "improve").reason == "learning_reads_off"
    assert not eligibility.eligible_sources(cap, "improve").exists()


def test_a_run_refuses_to_start_where_reads_are_off(w):
    r = _run(w["connect_owner"])
    assert r.status_code == 422 and "learning reads are off in connect" in r.json()["detail"]
    reads(w["dimagi"])
    assert _run(w["connect_owner"]).status_code == 200


# ---- every change is audited and shown on the page ---------------------------------------------------------

def test_every_change_is_audited_and_listed_on_the_page(w):
    assert _put_reads(w["connect_admin"], "connect", True).status_code == 200
    assert _put_reads(w["dimagi_owner"], "connect", False).status_code == 200
    assert _put_reads(w["dimagi_owner"], "connect", False).status_code == 200        # no change: no row
    rows = list(SettingChange.objects.filter(operation="set_learning_reads").order_by("pk"))
    assert [(r.workspace_id, r.actor_email) for r in rows] == [
        ("connect", "connect_admin@dimagi.com"), ("connect", "dimagi_owner@dimagi.com")]
    assert rows[0].changes[0]["before"] == {"own": None, "on": False, "from": ""}
    assert rows[0].changes[0]["after"] == {"own": True, "on": True, "from": "connect"}
    assert "from inherit to on" in rows[0].summary
    page = _client(w["connect_admin"]).get("/api/w/connect/brain/state").json()["learning_reads"]
    assert page["on"] is False and page["own"] is False and page["can_edit"] is True
    assert [(c["by"], c["before"], c["after"], c["was_on"], c["now_on"]) for c in page["changes"]] == [
        ("dimagi_owner@dimagi.com", True, False, True, False), ("connect_admin@dimagi.com", None, True, False, True)]
    # A member sees the setting, not who changed it, and cannot edit it.
    mine = _client(w["alice"]).get("/api/w/connect/brain/state").json()["learning_reads"]
    assert mine["can_edit"] is False and mine["changes"] == []


# ---- consent counts in a small workspace -------------------------------------------------------------------

def test_a_consent_count_below_three_is_hidden(w):
    row = {r["workspace"]: r for r in state.summary(w["connect_owner"], {"connect"}, "connect")["consent"]["workspaces"]}
    assert row["connect"]["agreed"]["system"] is None and row["connect"]["agreed_any"] is None   # alice and bob: 2
    assert row["connect"]["small"] == 3
    LearningConsent.objects.create(user=w["connect_editor"], classes=["system"])
    row = {r["workspace"]: r for r in state.summary(w["connect_owner"], {"connect"}, "connect")["consent"]["workspaces"]}
    assert row["connect"]["agreed"]["system"] == 3 and row["connect"]["agreed"]["project"] is None
    assert row["connect"]["agreed_any"] == 3


# ---- the deploy flag it replaces is gone -------------------------------------------------------------------

def test_the_old_deploy_flag_is_named_nowhere():
    """The env flag that "Learning reads" replaced has no compat shim: any reference left is a failure here."""
    from pathlib import Path

    old = "BRAIN_READS" + "_ENABLED"
    root = Path(__file__).resolve().parents[1]
    hits = []
    for top in ("apps", "config", "deploy", "docs", "frontend/src", "tests", "CLAUDE.md", "runner", "sdk"):
        base = root / top
        paths = [base] if base.is_file() else base.rglob("*") if base.exists() else []
        for p in paths:
            if p.is_file() and p.suffix in {".py", ".ts", ".tsx", ".md", ".yaml", ".yml", ".json", ".toml"}:
                if old in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(root)))
    assert hits == []
    from django.conf import settings
    assert not hasattr(settings, old)
