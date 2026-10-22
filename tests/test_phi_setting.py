"""The PHI-safe interface setting (`data: phi`), and what it changes: it is set by
a person, audited like any setting, refused when structurally false; and a phi
capability, and any turn in a ZDR conversation, is confined for everyone.

Spec: docs/superpowers/specs/2026-10-22-phi-agents-on-zdr-runners-design.md §5.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access, phi
from apps.agents.interface import InterfaceError, parse
from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session
from apps.events.models import SettingChange
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import RunnerAssignment, Turn
from apps.passkeys import stepup
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

DATA = {"callers": ["contact", "member"], "sites": ["connect-labs"], "data": "phi",
        "tools": ["Read", "Grep", "Glob", "Skill", "ToolSearch",
                  "mcp__*canopy-web__current_page", "mcp__*canopy-web__who_is_asking"]}
IFACE = {"capabilities": {"data": DATA,
                          "ask": {"callers": ["member"], "tools": ["Read", "Bash"]}}}
SOURCE = """capabilities:
  data:
    callers: [contact, member]
    sites: [connect-labs]
    data: phi
    tools: [Read, Grep, Glob, Skill, ToolSearch, "mcp__*canopy-web__current_page",
            "mcp__*canopy-web__who_is_asking"]
"""


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    admin = User.objects.create_user("ad", "ad@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    viewer = User.objects.create_user("vi", "vi@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="labs", display_name="Labs", created_by=owner)
    M.objects.create(user=owner, workspace=ws, role=M.OWNER)
    M.objects.create(user=admin, workspace=ws, role=M.EDITOR)
    M.objects.create(user=editor, workspace=ws, role=M.EDITOR)
    M.objects.create(user=viewer, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=owner)
    AgentAdmin.objects.create(agent=agent, user=admin, granted_by=owner)
    return {"owner": owner, "admin": admin, "editor": editor, "viewer": viewer, "ws": ws,
            "agent": agent}


def _client(user, stepped_up=True):
    c = Client()
    c.force_login(user)
    if stepped_up:
        s = c.session
        s[stepup.SESSION_KEY] = {"user": user.pk, "until": time.time() + 60}
        s.save()
    return c


def _put(c, slug, **body):
    return c.put(f"/api/agents/{slug}/interface", data=body, content_type="application/json")


def _declare(agent, by, doc=IFACE):
    agent.interface = parse(doc)
    agent.interface_published_by = by
    agent.save(update_fields=["interface", "interface_published_by"])


# --- refused when structurally false ---------------------------------------------------


def test_a_read_only_site_capability_may_say_phi():
    out = parse(IFACE)["capabilities"]["data"]
    assert out["data"] == "phi"
    assert "data" not in parse(IFACE)["capabilities"]["ask"]


@pytest.mark.parametrize("over, named", [
    ({"tools": ["Read", "Bash"]}, "Bash"),
    ({"tools": ["Write"]}, "Write"),
    ({"tools": ["WebFetch", "mcp__gdrive__upload"]}, "mcp__gdrive__upload"),
    ({"tools": ["mcp__*canopy-web__send_mail"]}, "send_mail"),
    ({"tools": ["mcp__*canopy-web__add_person_fact"]}, "add_person_fact"),
    ({"bash": ["canopy email read"]}, "shell"),
    ({"write_paths": ["{cwd}/out/*"]}, "write nothing"),
    ({"sites": []}, "sites"),
])
def test_a_phi_capability_that_can_leak_is_a_422_naming_it(over, named):
    with pytest.raises(InterfaceError) as exc:
        parse({"capabilities": {"data": {**DATA, **over}}})
    assert named in str(exc.value)


def test_data_takes_only_phi():
    with pytest.raises(InterfaceError, match="data must be one of"):
        parse({"capabilities": {"data": {**DATA, "data": "pii"}}})


# --- a person's setting, audited --------------------------------------------------------


def test_a_token_cannot_set_phi(w):
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="cli")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = _put(c, "labsdata", source=SOURCE)
    assert r.status_code == 403, r.content
    w["agent"].refresh_from_db()
    assert w["agent"].interface == {}  # nothing was published


def test_a_browser_without_a_fresh_passkey_is_asked_for_one(w):
    r = _put(_client(w["owner"], stepped_up=False), "labsdata", source=SOURCE)
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert not SettingChange.objects.exists()


def test_a_person_sets_it_and_the_change_is_audited(w):
    r = _put(_client(w["owner"]), "labsdata", source=SOURCE)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["interface"]["capabilities"]["data"]["data"] == "phi"
    assert body["published_by_email"] == "jj@dimagi.com"
    w["agent"].refresh_from_db()
    assert phi.is_phi(w["agent"], "data")
    [row] = SettingChange.objects.filter(operation="publish_interface")
    assert (row.actor, row.via, row.target) == (w["owner"], "ui", "labsdata")
    assert "data: phi" in row.request["body"]["source"]


def test_turning_it_off_needs_no_passkey_and_is_audited(w):
    _put(_client(w["owner"]), "labsdata", source=SOURCE)
    r = _put(_client(w["owner"], stepped_up=False), "labsdata",
             source="capabilities:\n  ask:\n    callers: [member]\n")
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert not phi.is_phi(w["agent"], "data")
    assert SettingChange.objects.filter(operation="publish_interface").count() == 2


def test_unpublishing_turns_it_off(w):
    _declare(w["agent"], w["owner"])
    assert _client(w["owner"]).delete("/api/agents/labsdata/interface").status_code == 200
    w["agent"].refresh_from_db()
    assert not phi.is_phi(w["agent"], "data")


# --- confined for everyone ---------------------------------------------------------------


def _site_session(w, meta=None):
    return Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["owner"],
                                  metadata={"embed_app": "connect-labs", **(meta or {})})


def _turn(w, user, session=None, **kw):
    t = Turn.objects.create(chat_session=session, prompt="how many visits?",
                            initiator_user=user, initiator_kind=who.USER,
                            idempotency_key=f"{user.pk}-{time.monotonic_ns()}", **kw)
    return access.decide_for_turn(t, w["agent"])


@pytest.mark.parametrize("who_", ["owner", "admin", "editor"])
def test_on_the_site_the_phi_door_confines_even_the_owner(w, who_):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w[who_], _site_session(w))
    assert (d.access, d.capability, d.basis) == (access.CONFINED, "data", "phi:data")
    assert d.stamp == "data"


def test_the_owner_keeps_their_role_and_mode_rights_when_confined(w):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w["owner"], _site_session(w))
    assert d.role == access.OWNER and d.may_pin_runner


def test_naming_a_phi_capability_confines_the_owner_anywhere(w):
    _declare(w["agent"], w["owner"])
    d = access.decide(w["agent"], w["owner"], verified=True, capability="data")
    assert (d.access, d.capability) == (access.CONFINED, "data")
    # Not naming it, off the site: the owner's full profile, as ever.
    assert access.decide(w["agent"], w["owner"], verified=True).access == access.FULL


def test_a_zdr_conversation_with_no_phi_door_is_refused_even_for_the_owner(w):
    w["agent"].interface = parse({"capabilities": {"ask": {"callers": ["member"]}}})
    w["agent"].save(update_fields=["interface"])
    d = _turn(w, w["owner"], _site_session(w, {"runner_requirements": ["zdr"]}))
    assert d.access == access.NONE and "PHI-safe" in d.reason


def test_a_zdr_conversation_runs_only_in_its_phi_door(w):
    _declare(w["agent"], w["owner"])
    zdr = {"runner_requirements": ["zdr"]}
    for who_ in ("owner", "editor", "viewer"):
        d = _turn(w, w[who_], _site_session(w, zdr))
        assert (d.access, d.capability) == (access.CONFINED, "data"), who_


def test_a_zdr_caller_the_phi_door_does_not_list_is_refused(w):
    _declare(w["agent"], w["owner"], doc={"capabilities": {
        "data": {**DATA, "callers": ["contact"]},
        "ask": {"callers": ["member"], "tools": ["Read"]}}})
    d = _turn(w, w["viewer"], _site_session(w, {"runner_requirements": ["zdr"]}))
    assert d.access == access.NONE and "'data' does not list you" in d.reason


def test_without_phi_nothing_changes(w):
    w["agent"].interface = parse({"capabilities": {
        "connect": {"callers": ["contact"], "sites": ["connect-labs"]}}})
    w["agent"].save(update_fields=["interface"])
    assert _turn(w, w["owner"], _site_session(w)).access == access.FULL


# --- any runner that confines runs it --------------------------------------------------


def _runner(c, **beat):
    rid = c.post("/api/harness/runners/", {"name": "box", "kind": "emdash",
                                           "capabilities": {"agents": ["labsdata"]}},
                 content_type="application/json").json()["id"]
    assert c.post(f"/api/harness/runners/{rid}/heartbeat",
                  {"active_turn_ids": [], "degraded": False, "note": "", **beat},
                  content_type="application/json").status_code == 200
    return rid


def _enqueue(w, user):
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API,
                                 idempotency_key=f"q-{time.monotonic_ns()}", capability="data",
                                 initiator=who.for_user(user, via="api", assurance=who.SESSION))
    return t


def test_a_runner_that_confines_claims_a_phi_turn_and_runs_its_current_code(w):
    _declare(w["agent"], w["owner"])
    c = _client(w["owner"])
    rid = _runner(c, profiles=3, envelope=2)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    _enqueue(w, w["owner"])
    r = c.post(f"/api/harness/runners/{rid}/claim")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["caller_context"]["profile"] == "confined" and "agent_sha" not in body
