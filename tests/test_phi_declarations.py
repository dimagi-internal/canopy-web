"""An author's PHI-safe declaration (`data: phi`), and what it changes: the
declaration is a person's, refused when structurally false; a phi capability, and
any turn in a ZDR conversation, is confined for everyone; and the declared commit
is what runs.

Spec: docs/superpowers/specs/2026-10-22-phi-agents-on-zdr-runners-design.md §5.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access, declarations
from apps.agents.interface import InterfaceError, parse
from apps.agents.models import Agent, AgentAdmin, AgentDeclaration
from apps.canopy_sessions.models import Session
from apps.events.models import Event
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import RunnerAssignment, Turn
from apps.passkeys import stepup
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
SHA = "a" * 40
SHA2 = "b" * 40

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


def _declare(agent, by, sha=SHA, doc=IFACE):
    agent.interface = parse(doc)
    agent.save(update_fields=["interface"])
    declarations.record(agent, sha=sha, by=by)


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


# --- a person's word, at a commit ------------------------------------------------------


def test_a_token_cannot_declare_phi(w):
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="cli")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = _put(c, "labsdata", source=SOURCE, declare_sha=SHA)
    assert r.status_code == 403, r.content
    assert not AgentDeclaration.objects.exists()
    w["agent"].refresh_from_db()
    assert w["agent"].interface == {}  # nothing was published either


def test_a_browser_without_a_fresh_passkey_is_asked_for_one(w):
    r = _put(_client(w["owner"], stepped_up=False), "labsdata", source=SOURCE, declare_sha=SHA)
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert not AgentDeclaration.objects.exists()


def test_a_declaration_names_its_commit(w):
    r = _put(_client(w["owner"]), "labsdata", source=SOURCE)
    assert r.status_code == 422 and "declare_sha" in r.json()["detail"]
    r = _put(_client(w["owner"]), "labsdata", source=SOURCE, declare_sha="main")
    assert r.status_code == 422


def test_a_person_declares_it_and_it_is_recorded(w):
    r = _put(_client(w["owner"]), "labsdata", source=SOURCE, declare_sha=SHA)
    assert r.status_code == 200, r.content
    [d] = r.json()["declarations"]
    assert (d["capability"], d["data"], d["sha"], d["declared_by_email"]) == (
        "data", "phi", SHA, "jj@dimagi.com")
    decl = declarations.current(w["agent"], "data")
    assert decl.declared_by == w["owner"] and "mcp__*canopy-web__site_call" in decl.profile["tools"]
    assert Event.objects.filter(kind="agent.phi_declared").count() == 1


def test_redeclaring_keeps_the_history_and_withdrawing_stops_it_counting(w):
    c = _client(w["owner"])
    _put(c, "labsdata", source=SOURCE, declare_sha=SHA)
    _put(c, "labsdata", source=SOURCE, declare_sha=SHA2)
    assert declarations.current(w["agent"], "data").sha == SHA2
    assert AgentDeclaration.objects.filter(agent=w["agent"]).count() == 2
    assert declarations.covers(w["agent"], "data", SHA2)
    assert not declarations.covers(w["agent"], "data", SHA)
    # No phi capability any more: no step-up needed to narrow, and nothing counts.
    r = _put(_client(w["owner"], stepped_up=False), "labsdata",
             source="capabilities:\n  ask:\n    callers: [member]\n")
    assert r.status_code == 200 and r.json()["declarations"] == []
    assert declarations.current(w["agent"], "data") is None
    assert Event.objects.filter(kind="agent.phi_withdrawn").count() == 1


def test_unpublishing_withdraws_every_declaration(w):
    _declare(w["agent"], w["owner"])
    assert _client(w["owner"]).delete("/api/agents/labsdata/interface").status_code == 200
    assert declarations.current(w["agent"], "data") is None


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


# --- the declared commit runs ------------------------------------------------------------


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


def test_a_phi_turn_is_stamped_with_the_declared_commit_at_enqueue(w):
    _declare(w["agent"], w["owner"])
    t = _enqueue(w, w["owner"])
    assert (t.capability, t.agent_sha) == ("data", SHA)


def test_only_a_runner_that_checks_out_the_commit_claims_it(w):
    _declare(w["agent"], w["owner"])
    c = _client(w["owner"])
    old = _runner(c, profiles=3, envelope=2)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=old, rank=0)
    _enqueue(w, w["owner"])
    assert c.post(f"/api/harness/runners/{old}/claim").status_code == 204
    assert c.post(f"/api/harness/runners/{old}/heartbeat",
                  {"active_turn_ids": [], "degraded": False, "note": "", "profiles": 3,
                   "envelope": 2, "pins": 1}, content_type="application/json").status_code == 200
    r = c.post(f"/api/harness/runners/{old}/claim")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["agent_sha"] == SHA and body["caller_context"]["profile"] == "confined"


def test_the_claim_carries_the_declaration_then_current(w):
    _declare(w["agent"], w["owner"])
    c = _client(w["owner"])
    rid = _runner(c, profiles=3, envelope=2, pins=1)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    _enqueue(w, w["owner"])
    declarations.record(w["agent"], sha=SHA2, by=w["owner"])  # re-declared while queued
    assert c.post(f"/api/harness/runners/{rid}/claim").json()["agent_sha"] == SHA2


def test_pins_is_reported_never_set_by_hand(w):
    c = _client(w["owner"])
    rid = _runner(c)
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {"pins": 1}},
                content_type="application/json")
    assert r.status_code == 422


# --- what the interface page shows an author ------------------------------------------


def test_a_declaration_links_its_commit_and_says_what_ran_last(w):
    w["agent"].repo_url = "https://github.com/dimagi-internal/labsdata"
    w["agent"].save(update_fields=["repo_url"])
    _declare(w["agent"], w["owner"])
    c = _client(w["owner"])
    [d] = c.get("/api/agents/labsdata/interface").json()["declarations"]
    assert d["commit_url"] == f"https://github.com/dimagi-internal/labsdata/commit/{SHA}"
    assert (d["last_run_sha"], d["last_run_at"]) == (None, None)
    rid = _runner(c, profiles=3, envelope=2, pins=1)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    _enqueue(w, w["owner"])
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 200
    declarations.record(w["agent"], sha=SHA2, by=w["owner"])  # re-declared after it ran
    [d] = c.get("/api/agents/labsdata/interface").json()["declarations"]
    assert (d["sha"], d["last_run_sha"]) == (SHA2, SHA) and d["last_run_at"]


class _Resp:
    def __init__(self, status, body):
        self.status_code, self.ok, self._body = status, 200 <= status < 300, body

    def json(self):
        return self._body


def test_the_repo_head_is_read_with_the_agents_github_identity(w, monkeypatch):
    from apps.agents.models import AgentDelegation
    from apps.common.encryption import encrypt_secret

    agent = w["agent"]
    agent.repo_url, agent.repo_ref = "https://github.com/dimagi-internal/labsdata", "main"
    agent.save(update_fields=["repo_url", "repo_ref"])
    url = "/api/agents/labsdata/interface/repo-head"
    assert _client(w["owner"]).get(url).json()["detail"] == (
        "the agent has no GitHub identity to read its repository with")
    AgentDelegation.objects.create(user=w["owner"], agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc=encrypt_secret("github_pat_x"))
    seen = {}

    def get(u, headers, timeout):
        seen.update(url=u, auth=headers["Authorization"])
        return _Resp(200, {"sha": SHA2})

    monkeypatch.setattr(declarations.requests, "get", get)
    body = _client(w["owner"]).get(url).json()
    assert (body["sha"], body["ref"], body["detail"]) == (SHA2, "main", "")
    assert body["commit_url"].endswith(f"/commit/{SHA2}")
    assert seen == {"url": "https://api.github.com/repos/dimagi-internal/labsdata/commits/main",
                    "auth": "Bearer github_pat_x"}
    monkeypatch.setattr(declarations.requests, "get", lambda *a, **k: _Resp(404, {}))
    body = _client(w["owner"]).get(url).json()
    assert body["sha"] == "" and "404" in body["detail"]
    # The agent's admins only: it reads the repo as the agent.
    assert _client(w["editor"]).get(url).status_code == 403
