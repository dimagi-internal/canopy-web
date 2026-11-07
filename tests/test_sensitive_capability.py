"""The interface setting that a host site handles sensitive data (`data: sensitive`), and what it changes: it is set by
the agent's owner or an admin (browser or token), audited like any setting, refused when structurally false; and a phi
profile, and any turn in a sensitive conversation, is confined for everyone.

Spec: docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md §5.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access, roles, sensitive_data, turn_context
from apps.agents.published_profiles import InterfaceError, is_read_mode, parse, published
from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session
from apps.events.models import SettingChange
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import RunnerAssignment, Turn
from apps.passkeys import stepup
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import allow, declare, editors_run

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

DATA = {"sites": ["connect-labs"], "data": "sensitive",
        "tools": ["Read", "Grep", "Glob", "Skill", "ToolSearch",
                  "mcp__*canopy-web__current_page", "mcp__*canopy-web__who_is_asking"]}
IFACE = {"capabilities": {"data": DATA,
                          "ask": {"tools": ["Read", "Bash"]}}}
#: Who the access list lets in, by profile: "member" (the workspace's members) or "contact".
WHO = {"data": ["contact", "member"], "ask": ["member"], "connect": ["contact"]}
SITE = "connect-labs"
#: The internal name a new connected-labs row gets (`profile_settings.key_for`).
KEY = "connect_labs"
SAFE = {"tools": DATA["tools"], "sensitive": True}


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


def _put(c, slug, site=SITE, **body):
    return c.put(f"/api/agents/{slug}/host-sites/{site}", data=body, content_type="application/json")


def _let_in(agent, who, name):
    """Read mode is what `read` opens, so it is a read-only line; any other profile a named line."""
    line = dict(role=roles.READ_ONLY) if is_read_mode(name) else dict(capability=name)
    allow(agent, contact=(who == "contact"), **line)


def _declare(agent, by, doc=IFACE, who_=WHO):
    """Publish `doc`'s profiles and let `who_` (members and/or contacts, per profile) in."""
    declare(agent, doc["capabilities"])
    for name in doc["capabilities"]:
        for caller in who_[name]:
            _let_in(agent, caller, name)
    editors_run(agent)
    agent.interface_published_by = by
    agent.save(update_fields=["interface_published_by"])


# --- refused when structurally false ---------------------------------------------------


def test_a_read_only_host_site_may_say_sensitive():
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
def test_a_sensitive_profile_that_can_leak_is_a_422_naming_it(over, named):
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
    r = _put(c, "labsdata", **SAFE)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], KEY)
    [row] = SettingChange.objects.filter(operation="put_agent_host_site")
    assert row.actor == w["admin"]


def test_a_browser_needs_no_passkey(w):
    r = _put(_client(w["owner"], stepped_up=False), "labsdata", **SAFE)
    assert r.status_code == 200, r.content


def test_someone_who_is_not_an_admin_cannot_set_it(w):
    raw, _ = PersonalToken.create_for_user(user=w["editor"], label="cli")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert _put(c, "labsdata", **SAFE).status_code == 403
    assert _put(_client(w["editor"]), "labsdata", **SAFE).status_code in (403, 404)
    w["agent"].refresh_from_db()
    assert not published(w["agent"].interface)


def test_a_person_sets_it_and_the_change_is_audited(w):
    r = _put(_client(w["owner"]), "labsdata", **SAFE)
    assert r.status_code == 200, r.content
    assert [row["sensitive"] for row in r.json()["sites"]] == [True]
    w["agent"].refresh_from_db()
    assert sensitive_data.handles_sensitive(w["agent"], KEY)
    assert w["agent"].interface_published_by == w["owner"]
    [row] = SettingChange.objects.filter(operation="put_agent_host_site")
    assert (row.actor, row.via) == (w["owner"], "ui")
    assert row.request["body"]["sensitive"] is True
    [change] = row.changes
    assert change["scope"] == f"agent:labsdata/site/{SITE}"
    assert change["before"] == {} and change["after"][KEY]["data"] == "sensitive"


def test_turning_it_off_is_audited_and_takes_the_setting_out(w):
    _put(_client(w["owner"]), "labsdata", **SAFE)
    r = _put(_client(w["owner"], stepped_up=False), "labsdata", sensitive=False)
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], KEY)
    assert "data" not in w["agent"].interface["capabilities"][KEY]
    assert SettingChange.objects.filter(operation="put_agent_host_site").count() == 2


def test_removing_the_site_turns_it_off(w):
    _declare(w["agent"], w["owner"])
    assert _client(w["owner"]).delete(f"/api/agents/labsdata/host-sites/{SITE}").status_code == 200
    w["agent"].refresh_from_db()
    assert not sensitive_data.handles_sensitive(w["agent"], "data")


def test_a_site_that_could_leak_cannot_turn_it_on_and_nothing_is_saved(w):
    assert _put(_client(w["owner"]), "labsdata", tools=["Read", "Bash"]).status_code == 200
    w["agent"].refresh_from_db()
    stored = w["agent"].interface
    r = _put(_client(w["owner"]), "labsdata", sensitive=True)
    assert r.status_code == 422 and "Bash" in r.json()["detail"]
    w["agent"].refresh_from_db()
    assert w["agent"].interface == stored
    assert SettingChange.objects.filter(operation="put_agent_host_site").count() == 1


def test_removing_a_site_the_agent_does_not_have_is_a_404(w):
    assert _client(w["owner"]).delete("/api/agents/labsdata/host-sites/nope").status_code == 404


# --- confined for everyone ---------------------------------------------------------------


def _site_session(w, meta=None):
    return Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["owner"],
                                  metadata={"embed_app": "connect-labs", **(meta or {})})


def _turn(w, user, session=None, **kw):
    t = Turn.objects.create(chat_session=session, prompt="how many visits?",
                            initiator_user=user, initiator_kind=who.USER,
                            idempotency_key=f"{user.pk}-{time.monotonic_ns()}", **kw)
    return turn_context.for_turn(t, w["agent"])


@pytest.mark.parametrize("who_", ["owner", "admin", "editor"])
def test_on_the_site_the_sensitive_profile_confines_even_the_owner(w, who_):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w[who_], _site_session(w))
    assert (d.access, d.stamped, d.basis) == (access.CONFINED, "data", "sensitive:data")
    assert d.stamped == "data"


def test_the_owner_keeps_their_role_and_mode_rights_when_confined(w):
    _declare(w["agent"], w["owner"])
    d = _turn(w, w["owner"], _site_session(w))
    assert d.standing.relationship == access.OWNER and d.review.may_pin_runner


def test_naming_a_sensitive_profile_confines_the_owner_anywhere(w):
    _declare(w["agent"], w["owner"])
    d = turn_context.for_asker(w["agent"], w["owner"], verified=True, requested="data")
    assert (d.access, d.stamped) == (access.CONFINED, "data")
    # Not naming it, off the site: the owner's full profile, as ever.
    assert turn_context.for_asker(w["agent"], w["owner"], verified=True).access == access.FULL


def test_a_sensitive_conversation_with_no_sensitive_profile_is_refused_even_for_the_owner(w):
    declare(w["agent"], {"ask": {}})
    _let_in(w["agent"], "member", "ask")
    editors_run(w["agent"])
    d = _turn(w, w["owner"], _site_session(w, {"runner_requirements": ["sensitive_data"]}))
    assert d.access == turn_context.REFUSED and "handles sensitive data" in d.refusal.why


def test_a_sensitive_conversation_runs_only_in_its_sensitive_profile(w):
    _declare(w["agent"], w["owner"])
    sensitive = {"runner_requirements": ["sensitive_data"]}
    for who_ in ("owner", "editor", "viewer"):
        d = _turn(w, w[who_], _site_session(w, sensitive))
        assert (d.access, d.stamped) == (access.CONFINED, "data"), who_


def test_a_sensitive_caller_the_sensitive_profile_does_not_list_is_refused(w):
    # The sensitive profile takes a user; a workspace viewer's line makes them only a viewer.
    _declare(w["agent"], w["owner"], doc={"capabilities": {
        "data": {**DATA},
        "ask": {"tools": ["Read"]}}}, who_={"data": ["contact"], "ask": ["member"]})
    d = _turn(w, w["viewer"], _site_session(w, {"runner_requirements": ["sensitive_data"]}))
    assert d.access == turn_context.REFUSED and "'data' does not list you" in d.refusal.why


def test_without_sensitive_nothing_changes(w):
    declare(w["agent"], {"connect": {"sites": ["connect-labs"]}})
    _let_in(w["agent"], "contact", "connect")
    editors_run(w["agent"])
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
