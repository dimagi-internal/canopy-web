"""An admin AGENT manages a workspace's learning, by the one rule every management route asks (`brain.actor.of`).

dimagi → connect. `cee` is an agent whose own login holds admin in connect; `dee`'s login is only an editor there. A
management route answers cee's own login inside its own running turn when the person the turn answers to (its asker,
or the schedule's creator) manages connect's learning too; it refuses dee, cee working for someone who does not manage
there, cee's stored token, and a learning run's own turn (a run never widens itself). A person's own choices
(`/api/me/learning/…`), reporting lines and the manager leg stay the person's. Every write names the agent, its turn
and its person."""
from __future__ import annotations

import contextlib
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.test import Client
from django.utils import timezone
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent, AgentProject
from apps.brain import registry, sources
from apps.brain.models import (
    BrainCapability,
    BrainOutput,
    BrainRun,
    BrainRunArtifact,
    BrainTarget,
    LearningConsent,
    WorkspaceLearningReads,
)
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.events.models import Event, SettingChange
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import Runner, RunnerAssignment, Turn, TurnAgentToken
from apps.mcp import api_tools
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace
from apps.workspaces.models import WorkspaceMembership as M
from tests.fixtures.brain_world import aim, member, reads, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture()
def w():
    registry.ensure_builtin()
    registry.ensure_learners()
    p = {n: user(n) for n in ("jj", "connect_admin", "connect_editor", "alice", "bob")}
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=p["jj"])
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=p["jj"], parent=dimagi)
    member(p["jj"], dimagi, M.OWNER)
    member(p["connect_admin"], connect, M.ADMIN)
    member(p["connect_editor"], connect)
    for u in (p["alice"], p["bob"]):
        member(u, connect)
        LearningConsent.objects.create(user=u, classes=["system", "project"])
    cee_login, dee_login = user("cee_login"), user("dee_login")
    member(cee_login, connect, M.ADMIN)
    member(dee_login, connect)
    cee = Agent.objects.create(slug="cee", name="Cee", owner=p["connect_admin"], workspace=connect, user=cee_login)
    dee = Agent.objects.create(slug="dee", name="Dee", owner=p["connect_admin"], workspace=connect, user=dee_login)
    ada = Agent.objects.create(slug="ada", name="Ada", owner=p["jj"], workspace=dimagi)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=p["connect_admin"], workspace=connect,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    for a in (cee, dee):
        RunnerAssignment.objects.create(agent=a, runner=box, rank=0)
    s = Session.objects.create(workspace=connect, created_by=p["alice"], origin=Session.ORIGIN_WEB, title="work")
    SessionParticipant.objects.create(session=s, user=p["bob"])
    Message.objects.create(session=s, role=Message.USER, turn_index=0, plaintext="the runner idles",
                           author={"user_id": p["alice"].pk})
    sources.for_session(s)
    system = BrainCapability.objects.get(slug="system-learning")
    registry.update(system, by=p["jj"], operator=ada, entry="/canopy:brain-learn", enabled=True)
    reads(connect)
    target = aim(system, connect, by=p["jj"])
    finding = BrainOutput.objects.create(capability=system, kind="finding", workspace=connect, subject_type="system",
                                         subject="connect", title="connect finding")
    run = BrainRun.objects.create(capability=system, subject_type="system", subject="connect", purpose="improve",
                                  state=BrainRun.DONE, scope="connect")
    flag = BrainRunArtifact.objects.create(run=run, kind="pr", ref="https://x/pr/1", workspace=connect,
                                           owner=p["jj"], state=BrainRunArtifact.FLAGGED, changed_at=timezone.now())
    own = Session.objects.create(workspace=connect, created_by=cee_login, origin=Session.ORIGIN_WEB, title="mine")
    return {**p, "dimagi": dimagi, "connect": connect, "cee": cee, "dee": dee, "ada": ada, "box": box, "session": s,
            "own_session": own, "system": system, "target": target, "finding": finding, "flag": flag}


def _turn(w, agent, by, *, kind="user", via="chat", assurance="session", **kw):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="set up learning",
                               idempotency_key=uuid.uuid4().hex, status=Turn.RUNNING, claimed_by=w["box"],
                               initiator_kind=kind, initiator_via=via, initiator_assurance=assurance,
                               initiator_user=by, **kw)


def _login(w, agent="cee", by="connect_admin", **kw) -> str:
    """The per-turn login of a running turn of `agent`, asked for by `by`."""
    return turn_agent_tokens.issue(_turn(w, w[agent], w[by], **kw), w["box"])["token"]


def _run_login(w) -> str:
    """The per-turn login of a learning run's own turn (its conversation stamped `brain_run`)."""
    run = BrainRun.objects.create(capability=w["system"], subject_type="system", subject="connect", purpose="improve",
                                  state=BrainRun.OPEN, scope="connect")
    conv = Session.objects.create(workspace=w["connect"], agent=w["cee"], origin=Session.ORIGIN_WEB,
                                  metadata={"brain_run": run.pk})
    turn = _turn(w, None, w["connect_admin"], kind="system", via="brain-run", assurance="internal",
                 chat_session=conv)
    return turn_agent_tokens.issue(turn, w["box"])["token"]


# ---- every management route: (method, path, body, success status) -------------------------------------------

def _routes(w):
    t, f, o = w["target"].pk, w["flag"].pk, w["finding"].pk
    return {
        "set_learning_reads": ("put", "/api/w/connect/brain/learning-reads", {"on": False}, 200),
        "configure_learner": ("patch", "/api/w/connect/brain/learners/project-learning/",
                              {"operator": "cee", "entry": "/canopy:brain-learn", "enabled": True}, 200),
        "add_learning_target": ("post", "/api/w/connect/brain/targets",
                                {"learner": "system-learning", "workspace": "connect", "cron": "0 7 * * *"}, 200),
        "remove_learning_target": ("delete", f"/api/w/connect/brain/targets/{t}", None, 204),
        "run_learning_target": ("post", f"/api/w/connect/brain/targets/{t}/runs", {}, 200),
        "set_target_budget": ("patch", f"/api/w/connect/brain/targets/{t}/budget", {"max_sources_per_run": 3}, 200),
        "set_output_status": ("post", f"/api/w/connect/brain/outputs/{o}/status", {"status": "accepted"}, 200),
        "acknowledge_learning_flag": ("post", f"/api/w/connect/brain/flags/{f}/acknowledge", {}, 204),
        "brain_state": ("get", "/api/w/connect/brain/state", None, 200),
        "brain_state_links": ("get", "/api/w/connect/brain/state/links", None, 200),
        "brain_state_runs": ("get", "/api/w/connect/brain/state/runs", None, 200),
        "why_not_read": ("get", f"/api/w/connect/brain/why/{w['own_session'].pk}?learner=system-learning", None, 200),
    }


ROUTE_NAMES = ("set_learning_reads", "configure_learner", "add_learning_target", "remove_learning_target",
               "run_learning_target", "set_target_budget", "set_output_status", "acknowledge_learning_flag",
               "brain_state", "brain_state_links", "brain_state_runs", "why_not_read")


def _call(w, name, token):
    method, path, body, _ok = _routes(w)[name]
    if name == "add_learning_target":
        w["target"].schedule.delete()                               # free the slot so the add can succeed
        assert not BrainTarget.objects.exists()
    kw = {"data": body, "content_type": J} if body is not None else {}
    return getattr(Client(), method)(path, **kw, **bearer_headers(token))


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_an_admin_agent_in_its_own_turn_manages(w, name):
    r = _call(w, name, _login(w))
    assert r.status_code == _routes(w)[name][3], (name, r.status_code, r.content[:300])


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_a_non_admin_agent_is_refused(w, name):
    r = _call(w, name, _login(w, agent="dee"))
    assert r.status_code in (403, 404), (name, r.status_code, r.content[:300])


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_an_admin_agent_working_for_someone_who_does_not_manage_is_refused(w, name):
    r = _call(w, name, _login(w, by="connect_editor"))
    assert r.status_code in (403, 404), (name, r.status_code, r.content[:300])


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_a_learning_runs_own_turn_may_manage_nothing(w, name):
    r = _call(w, name, _run_login(w))
    assert r.status_code == 403, (name, r.status_code, r.content[:300])
    assert "learning run" in r.json()["title"]


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_an_agents_stored_token_is_refused(w, name):
    r = _call(w, name, pat_token(w["cee"].user))
    assert r.status_code == 403, (name, r.status_code, r.content[:300])


def test_an_agent_with_no_person_behind_its_turn_manages_nothing(w):
    token = _login(w, kind="agent", via="dispatch", assurance="internal")
    r = _call(w, "set_learning_reads", token)
    assert r.status_code == 403


def test_a_scheduled_turn_answers_to_its_schedules_creator(w):
    ok = _login(w, kind="system", via="schedule", assurance="internal")
    assert _call(w, "set_learning_reads", ok).status_code == 200
    no = _login(w, by="connect_editor", kind="system", via="schedule", assurance="internal")
    assert _call(w, "set_learning_reads", no).status_code == 403


def test_a_turn_header_naming_a_run_is_refused_even_for_the_person(w):
    run_login = _run_login(w)
    turn_id = TurnAgentToken.objects.latest("pk").turn_id
    c = Client()
    c.force_login(w["connect_admin"])
    r = c.put("/api/w/connect/brain/learning-reads", {"on": True}, content_type=J, HTTP_X_CANOPY_TURN_ID=str(turn_id))
    assert r.status_code == 403
    assert run_login


# ---- what each write records -----------------------------------------------------------------------------------

def test_a_setting_change_names_the_agent_its_turn_and_its_person(w):
    token = _login(w)
    turn = Turn.objects.latest("created_at")
    assert _call(w, "set_learning_reads", token).status_code == 200
    row = SettingChange.objects.filter(operation="set_learning_reads").latest("pk")
    assert row.actor == w["cee"].user
    assert row.acting == {"by_id": w["cee"].user.pk, "by": w["cee"].user.email, "agent": "cee",
                          "turn": str(turn.pk), "for_id": w["connect_admin"].pk, "for": "connect_admin@dimagi.com"}
    assert row.summary.startswith("cee for connect_admin@dimagi.com set learning reads")
    c = Client()
    c.force_login(w["connect_admin"])
    changes = c.get("/api/w/connect/brain/state").json()["learning_reads"]["changes"]
    assert changes[0]["by"] == "cee for connect_admin@dimagi.com"           # the page: by <agent> for <person>


def test_a_learner_nobody_answers_for_becomes_the_persons_never_the_agents(w):
    assert _call(w, "configure_learner", _login(w)).status_code == 200
    cap = BrainCapability.objects.get(slug="project-learning")
    assert cap.enabled and cap.operator == w["cee"]
    assert registry.accountable(cap) == w["connect_admin"]


def test_status_run_now_and_flags_are_events_naming_the_agent(w):
    token = _login(w)
    assert _call(w, "set_output_status", token).status_code == 200
    ev = Event.objects.filter(source="brain.output", kind="output.status").latest("pk")
    assert ev.payload["agent"] == "cee" and ev.payload["for"] == "connect_admin@dimagi.com"
    assert _call(w, "acknowledge_learning_flag", token).status_code == 204
    ev = Event.objects.filter(source="brain.manage", kind="flag.acknowledged").latest("pk")
    assert ev.payload["agent"] == "cee" and ev.payload["for_id"] == w["connect_admin"].pk


def test_the_system_findings_list_opens_to_the_admin_agent(w):
    r = Client().get("/api/w/connect/brain/outputs/", **bearer_headers(_login(w)))
    assert [(o["title"], o["may_set_status"]) for o in r.json()] == [("connect finding", True)]
    r = Client().get("/api/w/connect/brain/outputs/", **bearer_headers(_login(w, agent="dee")))
    assert r.json() == []
    r = Client().get("/api/w/connect/brain/outputs/", **bearer_headers(_login(w, by="connect_editor")))
    assert r.json() == []


# ---- a project's own people --------------------------------------------------------------------------------------

@pytest.fixture()
def pw(w):
    project = AgentProject.objects.create(workspace=w["connect"], handle="relay", name="Relay",
                                          visibility=AgentProject.WORKSPACE, owner_user=w["alice"])
    learner = BrainCapability.objects.get(slug="project-learning")
    registry.update(learner, by=w["connect_admin"], operator=w["cee"], entry="/canopy:brain-learn", enabled=True)
    return {**w, "project": project, "learner": learner}


def test_a_projects_lead_and_an_agent_working_for_them_manage_its_targets(pw):
    c = Client()
    c.force_login(pw["alice"])                                         # the project's lead, an editor of connect
    r = c.post("/api/w/connect/brain/targets", {"learner": "project-learning", "project": "relay"}, content_type=J)
    assert r.status_code == 200, r.content
    tid = r.json()["id"]
    assert c.patch(f"/api/w/connect/brain/targets/{tid}/budget", {"max_outputs_per_run": 2},
                   content_type=J).status_code == 200
    # Not the workspace's: a workspace target stays its managers'.
    r = c.post("/api/w/connect/brain/targets", {"learner": "system-learning"}, content_type=J)
    assert r.status_code == 403
    # An agent working for the lead: only once it is one of the project's member agents.
    token = _login(pw, agent="dee", by="alice")
    path = f"/api/w/connect/brain/targets/{tid}/budget"
    assert Client().patch(path, {"max_outputs_per_run": 1}, content_type=J,
                          **bearer_headers(token)).status_code == 403
    pw["project"].member_agents.add(pw["dee"])
    assert Client().patch(path, {"max_outputs_per_run": 1}, content_type=J,
                          **bearer_headers(token)).status_code == 200
    # Working for someone who is not the project's lead or admin: refused.
    other = _login(pw, agent="dee", by="bob")
    assert Client().patch(path, {"max_outputs_per_run": 1}, content_type=J,
                          **bearer_headers(other)).status_code == 403


def test_an_agent_working_for_a_projects_lead_sets_a_project_findings_status(pw):
    out = BrainOutput.objects.create(capability=pw["learner"], kind="finding", workspace=pw["connect"],
                                     subject_type="project", subject=str(pw["project"].pk), title="relay finding")
    path = f"/api/w/connect/brain/outputs/{out.pk}/status"
    token = _login(pw, agent="dee", by="alice")
    assert Client().post(path, {"status": "done"}, content_type=J, **bearer_headers(token)).status_code == 403
    pw["project"].member_agents.add(pw["dee"])
    r = Client().post(path, {"status": "done"}, content_type=J, **bearer_headers(token))
    assert r.status_code == 200, r.content
    out.refresh_from_db()
    assert out.status == "done" and out.status_by == pw["dee"].user
    other = _login(pw, agent="dee", by="bob")                          # bob reads the project but leads nothing
    assert Client().post(path, {"status": "open"}, content_type=J, **bearer_headers(other)).status_code == 403


# ---- what stays the person's ---------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/me/learning/", None),
    ("put", "/api/me/learning/", {"classes": ["project"]}),
    ("delete", "/api/me/learning/", None),
    ("get", "/api/me/learning/disclosure/", None),
    ("get", "/api/me/learning/removed/", None),
    ("get", "/api/brain/workspaces/connect/reporting/", None),
    ("put", "/api/brain/workspaces/connect/reporting/", {"person": "alice@dimagi.com", "manager": "bob@dimagi.com"}),
])
def test_a_persons_own_choices_and_reporting_lines_stay_the_persons(w, method, path, body):
    kw = {"data": body, "content_type": J} if body is not None else {}
    r = getattr(Client(), method)(path, **kw, **bearer_headers(_login(w)))
    assert r.status_code == 403, (path, r.status_code)


def test_capability_registration_stays_a_canopy_admins(w):
    r = Client().post("/api/brain/capabilities/", {"slug": "xx", "name": "x", "purpose_class": "system",
                                                    "purposes": ["improve"]}, content_type=J,
                      **bearer_headers(_login(w)))
    assert r.status_code == 403


# ---- MCP: the same routes, the same rule ---------------------------------------------------------------------

@contextlib.contextmanager
def _mcp_as(token: str):
    access = async_to_sync(CanopyPATVerifier().verify_token)(token)
    assert access is not None
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def test_the_management_routes_are_mcp_tools_and_the_persons_choices_are_not():
    for name in ROUTE_NAMES:
        assert name not in api_tools.EXCLUDED, name
    for name in ("get_my_learning", "set_my_learning", "exclude_my_session", "set_reporting", "list_reporting"):
        assert name in api_tools.EXCLUDED, name


def test_an_admin_agent_turns_learning_reads_on_over_mcp(w):
    reads(w["connect"], None)
    with _mcp_as(_login(w)):
        async_to_sync(mcp.call_tool)("set_learning_reads", {"workspace": "connect", "on": True})
    assert WorkspaceLearningReads.objects.get(workspace_id="connect").on is True
    row = SettingChange.objects.filter(operation="set_learning_reads").latest("pk")
    assert row.via == "mcp" and row.acting["agent"] == "cee"
    with _mcp_as(_login(w, agent="dee")), pytest.raises(ToolError):
        async_to_sync(mcp.call_tool)("set_learning_reads", {"workspace": "connect", "on": False})
    with _mcp_as(_run_login(w)), pytest.raises(ToolError, match="learning run"):
        async_to_sync(mcp.call_tool)("set_learning_reads", {"workspace": "connect", "on": False})
    assert WorkspaceLearningReads.objects.get(workspace_id="connect").on is True


def test_a_person_over_mcp_and_an_act_as_permission_are_still_refused(w):
    c_pat = pat_token(w["connect_admin"])
    with _mcp_as(c_pat), pytest.raises(ToolError):
        async_to_sync(mcp.call_tool)("set_learning_reads", {"workspace": "connect", "on": False})
    for path in ("/api/w/connect/brain/learning-reads", "/api/w/connect/brain/targets/1/runs",
                 "/api/w/connect/brain/outputs/1/status", "/api/w/connect/brain/state"):
        assert act_as.route_refusal("POST", path, "write", is_tool=True) is not None, path
