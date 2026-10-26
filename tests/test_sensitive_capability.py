"""The interface setting that a capability handles sensitive data (`data: sensitive`), and what it changes: it is set by
the agent's owner or an admin (browser or token), audited like any setting, refused when structurally false; and a phi
capability, and any turn in a sensitive conversation, is confined for everyone.

Spec: docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §5.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access, sensitive_data
from apps.agents.interface import InterfaceError, is_default, parse
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

DATA = {"callers": ["contact", "member"], "sites": ["connect-labs"], "data": "sensitive",
        "tools": ["Read", "Grep", "Glob", "Skill", "ToolSearch",
                  "mcp__*canopy-web__current_page", "mcp__*canopy-web__who_is_asking"]}
IFACE = {"capabilities": {"data": DATA,
                          "ask": {"callers": ["member"], "tools": ["Read", "Bash"]}}}
SOURCE = """capabilities:
  data:
    callers: [contact, member]
    sites: [connect-labs]
    data: sensitive
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


def test_a_read_only_site_capability_may_say_sensitive():
    out = parse(IFACE)["capabilities"]["data"]
    assert out["data"] == "sensitive"
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
def test_a_sensitive_capability_that_can_leak_is_a_422_naming_it(over, named):
    with pytest.raises(InterfaceError) as exc:
        parse({"capabilities": {"data": {**DATA, **over}}})
    assert named in str(exc.value)


def test_data_takes_only_sensitive():
    with pytest.raises(InterfaceError, match="data must be one of"):
        parse({"capabilities": {"data": {**DATA, "data": "pii"}}})


# --- a person's setting, audited --------------------------------------------------------


def test_an_admin_token_can_set_sensitive_and_it_is_audited(w):
    raw, _ = PersonalToken.create_for_user(user=w["admin"], label="cli")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = _put(c, "labsdata", source=SOURCE)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], "data")
    [row] = SettingChange.objects.filter(operation="publish_interface")
    assert row.actor == w["admin"]


def test_a_browser_needs_no_passkey(w):
    r = _put(_client(w["owner"], stepped_up=False), "labsdata", source=SOURCE)
    assert r.status_code == 200, r.content


def test_someone_who_is_not_an_admin_cannot_set_it(w):
    raw, _ = PersonalToken.create_for_user(user=w["editor"], label="cli")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert _put(c, "labsdata", source=SOURCE).status_code == 403
    w["agent"].refresh_from_db()
    assert is_default(w["agent"].interface)


def test_a_person_sets_it_and_the_change_is_audited(w):
    r = _put(_client(w["owner"]), "labsdata", source=SOURCE)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["interface"]["capabilities"]["data"]["data"] == "sensitive"
    assert body["published_by_email"] == "jj@dimagi.com"
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], "data")
    [row] = SettingChange.objects.filter(operation="publish_interface")
    assert (row.actor, row.via, row.target) == (w["owner"], "ui", "labsdata")
    assert "data: sensitive" in row.request["body"]["source"]


def test_turning_it_off_is_audited(w):
    _put(_client(w["owner"]), "labsdata", source=SOURCE)
    r = _put(_client(w["owner"], stepped_up=False), "labsdata",
             source="capabilities:\n  ask:\n    callers: [member]\n")
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], "data")
    assert SettingChange.objects.filter(operation="publish_interface").count() == 2


def test_unpublishing_turns_it_off(w):
    _declare(w["agent"], w["owner"])
    assert _client(w["owner"]).delete("/api/agents/labsdata/interface").status_code == 200
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], "data")


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
def test_on_the_site_the_sensitive_door_confines_even_the_owner(w, who_):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w[who_], _site_session(w))
    assert (d.access, d.capability, d.basis) == (access.CONFINED, "data", "sensitive:data")
    assert d.stamp == "data"


def test_the_owner_keeps_their_role_and_mode_rights_when_confined(w):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w["owner"], _site_session(w))
    assert d.role == access.OWNER and d.may_pin_runner


def test_naming_a_sensitive_capability_confines_the_owner_anywhere(w):
    _declare(w["agent"], w["owner"])
    d = access.decide(w["agent"], w["owner"], verified=True, capability="data")
    assert (d.access, d.capability) == (access.CONFINED, "data")
    # Not naming it, off the site: the owner's full profile, as ever.
    assert access.decide(w["agent"], w["owner"], verified=True).access == access.FULL


def test_a_sensitive_conversation_with_no_sensitive_door_is_refused_even_for_the_owner(w):
    w["agent"].interface = parse({"capabilities": {"ask": {"callers": ["member"]}}})
    w["agent"].save(update_fields=["interface"])
    d = _turn(w, w["owner"], _site_session(w, {"runner_requirements": ["sensitive_data"]}))
    assert d.access == access.NONE and "handles sensitive data" in d.reason


def test_a_sensitive_conversation_runs_only_in_its_sensitive_door(w):
    _declare(w["agent"], w["owner"])
    sensitive = {"runner_requirements": ["sensitive_data"]}
    for who_ in ("owner", "editor", "viewer"):
        d = _turn(w, w[who_], _site_session(w, sensitive))
        assert (d.access, d.capability) == (access.CONFINED, "data"), who_


def test_a_sensitive_caller_the_sensitive_door_does_not_list_is_refused(w):
    _declare(w["agent"], w["owner"], doc={"capabilities": {
        "data": {**DATA, "callers": ["contact"]},
        "ask": {"callers": ["member"], "tools": ["Read"]}}})
    d = _turn(w, w["viewer"], _site_session(w, {"runner_requirements": ["sensitive_data"]}))
    assert d.access == access.NONE and "'data' does not list you" in d.reason


def test_without_sensitive_nothing_changes(w):
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


def test_a_runner_that_confines_claims_a_sensitive_turn_and_runs_its_current_code(w):
    _declare(w["agent"], w["owner"])
    c = _client(w["owner"])
    rid = _runner(c, profiles=3, envelope=2)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    _enqueue(w, w["owner"])
    r = c.post(f"/api/harness/runners/{rid}/claim")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["caller_context"]["profile"] == "confined" and "agent_sha" not in body


# --- the switch: one capability, the same setting the YAML holds -------------------------

SWITCHABLE = """# who may read a host's data
capabilities:
  data:
    # the site's data door
    callers: [contact, member]
    sites: [connect-labs]
    tools: [Read, Grep, "mcp__*canopy-web__current_page"]
  ask:
    callers: [member]
    tools: [Read, Bash]
"""


def _switch(c, slug, name, on):
    return c.put(f"/api/agents/{slug}/interface/capabilities/{name}/sensitive",
                 data={"handles_sensitive": on}, content_type="application/json")


def _publish_source(w, source):
    r = _put(_client(w["owner"]), "labsdata", source=source)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()


def test_with_sensitive_writes_one_line_and_keeps_every_comment():
    on = sensitive_data.with_sensitive(SWITCHABLE, "data", True)
    assert on.splitlines()[3] == "    data: sensitive"
    assert "# the site's data door" in on and on.startswith("# who may read")
    assert sensitive_data.with_sensitive(on, "data", False) == SWITCHABLE
    assert sensitive_data.with_sensitive(on, "data", True) == on  # already on: unchanged


def test_with_sensitive_refuses_a_layout_it_cannot_edit_by_line():
    flow = 'capabilities: {data: {callers: [member], sites: [connect-labs]}}\n'
    with pytest.raises(sensitive_data.SourceEditError):
        sensitive_data.with_sensitive(flow, "data", True)
    with pytest.raises(sensitive_data.SourceEditError):
        sensitive_data.with_sensitive(SWITCHABLE, "nope", True)


def test_the_switch_is_an_ordinary_edit_no_passkey_from_a_browser_or_a_token(w):
    _publish_source(w, SWITCHABLE)
    r = _switch(_client(w["owner"], stepped_up=False), "labsdata", "data", True)
    assert r.status_code == 200, r.content
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="cli")
    r = _switch(Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), "labsdata", "data", False)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], "data")


def test_the_switch_writes_the_yaml_and_the_yaml_says_what_the_switch_did(w):
    _publish_source(w, SWITCHABLE)
    r = _switch(_client(w["owner"]), "labsdata", "data", True)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], "data") and not sensitive_data.handles_sensitive(w["agent"], "ask")
    source = w["agent"].interface_source
    assert "    data: sensitive" in source and "# the site's data door" in source
    # Saving that YAML back, untouched, through the YAML path changes nothing.
    stored = w["agent"].interface
    r = _put(_client(w["owner"]), "labsdata", source=r.json()["source"])
    assert r.status_code == 200
    assert r.json()["interface"] == stored


def test_the_switch_is_audited_with_what_it_changed(w):
    _publish_source(w, SWITCHABLE)
    _switch(_client(w["owner"]), "labsdata", "data", True)
    [row] = SettingChange.objects.filter(operation="set_capability_sensitive")
    assert (row.actor, row.target) == (w["owner"], "labsdata / data")
    [change] = row.changes
    assert change["scope"] == "interface:labsdata/data"
    assert change["before"] == {"sensitive_capabilities": []}
    assert change["after"] == {"sensitive_capabilities": ["data"]}


def test_turning_the_switch_off_takes_the_line_out(w):
    _publish_source(w, SWITCHABLE)
    saved = w["agent"].interface_source
    _switch(_client(w["owner"]), "labsdata", "data", True)
    r = _switch(_client(w["owner"], stepped_up=False), "labsdata", "data", False)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], "data")
    assert w["agent"].interface_source == saved


def test_the_switch_refuses_a_capability_that_could_leak_and_saves_nothing(w):
    _publish_source(w, SWITCHABLE)
    saved = w["agent"].interface_source
    r = _switch(_client(w["owner"]), "labsdata", "ask", True)
    assert r.status_code == 422 and "Bash" in r.json()["detail"]
    w["agent"].refresh_from_db()
    assert w["agent"].interface_source == saved
    assert not SettingChange.objects.filter(operation="set_capability_sensitive").exists()


def test_an_interface_saved_as_a_mapping_is_switched_as_a_mapping(w):
    cap = {k: v for k, v in DATA.items() if k != "data"}
    r = _client(w["owner"]).put("/api/agents/labsdata/interface", content_type="application/json",
                                data={"interface": {"capabilities": {"data": cap}}})
    assert r.status_code == 200, r.content
    r = _switch(_client(w["owner"]), "labsdata", "data", True)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], "data") and w["agent"].interface_source == ""


def test_the_switch_names_a_capability_the_interface_has(w):
    _publish_source(w, SWITCHABLE)
    assert _switch(_client(w["owner"]), "labsdata", "nope", True).status_code == 404


def test_only_the_agents_admins_flip_the_switch(w):
    _publish_source(w, SWITCHABLE)
    assert _switch(_client(w["editor"]), "labsdata", "data", True).status_code in (403, 404)
    assert _switch(_client(w["admin"]), "labsdata", "data", True).status_code == 200
