"""THE rule, asked through every door — docs/architecture/access.md.

Owner decisions of 2026-10-04: one function (`apps.agents.access.decide`) decides
what a person may make an agent do, and every entry point that makes an agent
work asks it — the harness API, a canopy-web chat send, Slack intake, email
intake and the caller envelope. This matrix walks roles × entry points ×
interface (none / Eva-like `full: [member, contact@dimagi.com:verified]` /
Hal-like ask-only) and pins, for each cell, the profile the turn runs in, why
(`granted_by`), and the mode it is claimed in — plus who may REQUEST `auto`, the
pin rule, and the routing-auto gate.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access
from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions import services as chat
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness import turn_mode
from apps.harness.models import Runner, RunnerAdmin, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

#: Eva's real setup: members and verified dimagi staff get her whole.
EVA = {"full": ["member", "contact@dimagi.com:verified"]}
#: Hal-like: everyone not an admin is confined to `ask`.
HAL = {"capabilities": {"ask": {"description": "Ask", "callers": ["member", "contact"],
                                "tools": ["Read"]}}}
IFACES = {"none": None, "eva": EVA, "hal": HAL}


def _dmarc(domain):
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from={domain}"}]


@pytest.fixture()
def w():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")      # workspace owner
    op = User.objects.create_user("op", "op@dimagi.com", "pw")            # agent owner
    adm = User.objects.create_user("adm", "adm@dimagi.com", "pw")         # explicit AgentAdmin
    wsadm = User.objects.create_user("wsadm", "wsadm@dimagi.com", "pw")   # workspace admin
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")            # workspace editor
    vw = User.objects.create_user("vw", "vw@dimagi.com", "pw")            # workspace viewer
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    for u, role in ((boss, M.OWNER), (op, M.EDITOR), (adm, M.EDITOR), (wsadm, M.ADMIN),
                    (ed, M.EDITOR), (vw, M.VIEWER)):
        M.objects.create(user=u, workspace=ws, role=role)
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    agent = Agent.objects.create(slug="agent", name="Agent", workspace=ws, owner=op,
                                 turn_mode="auto")
    AgentAdmin.objects.create(agent=agent, user=adm, granted_by=op)
    return {"ws": ws, "agent": agent, "owner": op, "ws_owner": boss, "admin": adm,
            "ws_admin": wsadm, "editor": ed, "viewer": vw}


def _publish(agent, doc):
    agent.interface = parse(doc) if doc else {}
    agent.save(update_fields=["interface"])


# --- the doors ---------------------------------------------------------------------

def _via_api(w, user, key):
    c = Client()
    c.force_login(user)
    r = c.post("/api/harness/turns/", {"agent_slug": "agent", "origin": "api",
                                       "idempotency_key": key, "prompt": "go"},
               content_type="application/json")
    if r.status_code == 403:
        return None
    assert r.status_code == 201, r.content
    return Turn.objects.get(idempotency_key=key)


def _via_chat(w, user, key, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, assurance=who.SESSION):
    session = chat.create_session(workspace=w["ws"], created_by=user, agent=w["agent"], title="t")
    _msg, turn = chat.send_message(session=session, text="go", user=user, client_id=key,
                                   origin=origin,
                                   initiator=who.for_user(user, via=origin, assurance=assurance))
    return turn


def _via_slack(w, user, key):
    return _via_chat(w, user, key, origin=Turn.ORIGIN_SLACK, assurance=who.SLACK_LINKED)


def _via_email(w, email, key, verified=True):
    ref = {"from": email, "thread_id": f"t-{key}", "subject": "s"}
    if verified:
        ref["headers"] = _dmarc(email.rpartition("@")[2])
    turn, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL,
                                    idempotency_key=f"email-{key}", origin_ref=ref,
                                    prompt="/agent:turn")
    return turn


DOORS = {"api": _via_api, "chat": _via_chat, "slack": _via_slack}

#: What each member role reaches, per interface: (profile, granted_by, mode at claim).
#: The agent's own switch is `auto`, so `manual` here is the editor tier at work.
EXPECT = {
    "owner":    {"none": ("full", "owner", "auto"), "eva": ("full", "owner", "auto"),
                 "hal": ("full", "owner", "auto")},
    "ws_owner": {"none": ("full", "admin", "auto"), "eva": ("full", "admin", "auto"),
                 "hal": ("full", "admin", "auto")},
    "admin":    {"none": ("full", "admin", "auto"), "eva": ("full", "admin", "auto"),
                 "hal": ("full", "admin", "auto")},
    # A workspace admin runs the workspace and holds no agent keys: editor tier.
    "ws_admin": {"none": ("full", "editor", "manual"), "eva": ("full", "full:member", "auto"),
                 "hal": ("full", "editor", "manual")},
    "editor":   {"none": ("full", "editor", "manual"), "eva": ("full", "full:member", "auto"),
                 "hal": ("full", "editor", "manual")},
    "viewer":   {"none": (None, "refused", None), "eva": ("full", "full:member", "auto"),
                 "hal": ("confined", "capability:ask", "auto")},
}


def _check(turn, want):
    profile, basis, mode = want
    env = caller_context.build(turn)
    if profile is None:
        assert turn.status == Turn.CANCELLED, turn.result_note
        assert env["granted_by"] == "refused"
        return
    assert turn.status == Turn.QUEUED, turn.result_note
    assert env["profile"] == profile
    assert env["granted_by"] == basis
    agent = turn.agent or turn.chat_session.agent
    assert turn_mode.for_turn(turn, fresh=True).mode == mode, turn_mode.for_turn(turn, fresh=True)
    # The roster says what the harness does (same rule).
    if turn.initiator_user_id:
        row = next(r for r in access.roster(agent) if r["user_id"] == turn.initiator_user_id)
        assert row["access"] == profile


@pytest.mark.parametrize("iface", list(IFACES))
@pytest.mark.parametrize("role", list(EXPECT))
@pytest.mark.parametrize("door", list(DOORS))
def test_members_through_every_signed_in_door(w, door, role, iface):
    _publish(w["agent"], IFACES[iface])
    user = w[role]
    turn = DOORS[door](w, user, f"{door}-{role}-{iface}")
    want = EXPECT[role][iface]
    if door == "api" and role == "viewer":
        # The harness API's agent branch is the editor tier's door: a viewer
        # talks to an agent by chat, Slack or a capability's MCP tool instead.
        assert turn is None
        return
    _check(turn, want)


@pytest.mark.parametrize("iface", list(IFACES))
@pytest.mark.parametrize("role", list(EXPECT))
def test_members_by_verified_email(w, role, iface):
    """A member proven by THIS message's DMARC is that member (`_member_behind_email`)."""
    _publish(w["agent"], IFACES[iface])
    _check(_via_email(w, w[role].email, f"{role}-{iface}"), EXPECT[role][iface])


CONTACTS = {
    # (address, verified) -> per interface
    ("staff@dimagi.com", True): {"none": (None, "refused", None),
                                 "eva": ("full", "full:contact@dimagi.com:verified", "auto"),
                                 "hal": ("confined", "capability:ask", "auto")},
    ("staff@dimagi.com", False): {"none": (None, "refused", None), "eva": (None, "refused", None),
                                  "hal": ("confined", "capability:ask", "auto")},
    ("someone@partner.org", True): {"none": (None, "refused", None), "eva": (None, "refused", None),
                                    "hal": ("confined", "capability:ask", "auto")},
}


@pytest.mark.parametrize("iface", list(IFACES))
@pytest.mark.parametrize("sender", list(CONTACTS))
def test_contacts_by_email(w, sender, iface):
    _publish(w["agent"], IFACES[iface])
    address, verified = sender
    turn = _via_email(w, address, f"{address}-{verified}-{iface}", verified=verified)
    _check(turn, CONTACTS[sender][iface])
    env = caller_context.build(turn)
    assert env["relationship"] == "contact"


def test_a_refusal_says_what_the_caller_can_do(w):
    _publish(w["agent"], None)
    t = _via_chat(w, w["viewer"], "r1")
    assert "published no interface" in t.result_note and "editor" in t.result_note
    _publish(w["agent"], {"capabilities": {"ask": {"callers": ["member"]},
                                           "status": {"callers": ["member"]}}})
    d = access.decide(w["agent"], w["viewer"], verified=True, capability="deploy")
    assert d.access == access.NONE
    assert "you may use capability 'ask', 'status'" in d.reason


def test_the_mcp_tool_list_follows_the_rule(w):
    from apps.agents.interface import offered_to

    _publish(w["agent"], HAL)
    assert offered_to(w["editor"], w["agent"]) == ["ask"]      # full: every door
    assert offered_to(w["viewer"], w["agent"]) == ["ask"]
    _publish(w["agent"], {"capabilities": {"ask": {"callers": ["contact"]},
                                           "admin_only": {"callers": []}}})
    assert offered_to(w["editor"], w["agent"]) == ["admin_only", "ask"]
    assert offered_to(w["viewer"], w["agent"]) == []


# --- requested mode ------------------------------------------------------------------

@pytest.mark.parametrize("role, may_auto", [
    ("owner", True), ("ws_owner", True), ("admin", True),
    ("ws_admin", False), ("editor", False),
])
@pytest.mark.parametrize("mode", ["", "manual", "auto"])
def test_who_may_request_which_mode(w, role, may_auto, mode):
    c = Client()
    c.force_login(w[role])
    body = {"agent_slug": "agent", "origin": "api", "idempotency_key": f"m-{role}-{mode}",
            "prompt": "go"}
    if mode:
        body["turn_mode"] = mode
    r = c.post("/api/harness/turns/", body, content_type="application/json")
    if mode == "auto" and not may_auto:
        assert r.status_code == 403
        assert "manual" in r.json()["title"]
        return
    assert r.status_code == 201, r.content
    t = Turn.objects.get(pk=r.json()["id"])
    got = turn_mode.for_turn(t, fresh=True).mode
    if mode == "manual":
        assert got == "manual"
    elif may_auto:
        assert got == "auto"          # requested auto, or the agent's own switch
    else:
        assert got == "manual"        # the editor tier: always manual
    assert access.decide(w["agent"], w[role], verified=True).may_request_auto is may_auto


def test_an_editor_promoted_while_queued_gets_the_agents_mode_at_claim(w):
    t = _via_api(w, w["editor"], "promote")
    assert turn_mode.for_turn(t, fresh=True).mode == "manual"
    AgentAdmin.objects.create(agent=w["agent"], user=w["editor"], granted_by=w["owner"])
    assert turn_mode.for_turn(t, fresh=True).mode == "auto"


# --- pinning ---------------------------------------------------------------------------

def _runner(owner, name):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, owner=owner,
                                 workspace_id="connect", capabilities={"agents": ["agent"]})


@pytest.mark.parametrize("role, may_pin", [
    ("owner", True), ("ws_owner", True), ("admin", True), ("ws_admin", False), ("editor", False),
])
def test_pinning_needs_agent_admin_or_runner_admin(w, role, may_pin):
    box = _runner(w["owner"], "owners-box")                 # may hold the agent
    c = Client()
    c.force_login(w[role])
    body = {"agent_slug": "agent", "origin": "api", "idempotency_key": f"pin-{role}",
            "prompt": "go", "runner_id": str(box.id)}
    r = c.post("/api/harness/turns/", body, content_type="application/json")
    assert r.status_code == (201 if may_pin else 403), r.content
    if not may_pin:
        RunnerAdmin.objects.create(runner=box, user=w[role])
        body["idempotency_key"] += "-again"
        r = c.post("/api/harness/turns/", body, content_type="application/json")
        assert r.status_code == 201, r.content


# --- the routing-auto gate -------------------------------------------------------------

def _rules(c, rules):
    return c.put("/api/agents/agent/runner-rules", {"rules": rules},
                 content_type="application/json")


def _rule(box, mode, source="email", actor=""):
    return {"source": source, "actor": actor, "strict": False, "turn_mode": mode,
            "runners": [{"runner_id": str(box.id), "enabled": True}]}


@pytest.mark.parametrize("role", ["owner", "admin", "ws_owner"])
def test_an_agent_admin_may_set_an_auto_rule(w, role):
    box = _runner(w[role], f"{role}-box")          # their own box: they may hold the agent
    c = Client()
    c.force_login(w[role])
    assert _rules(c, [_rule(box, "manual")]).status_code == 200
    assert _rules(c, [_rule(box, "auto")]).status_code == 200
    route = c.put("/api/agents/agent/actor-routes/beth@dimagi.com",
                  {"runners": [{"runner_id": str(box.id), "enabled": True}], "turn_mode": "auto"},
                  content_type="application/json")
    assert route.status_code == 200, route.content


@pytest.mark.parametrize("role", ["ws_admin", "editor"])
def test_an_editor_may_route_but_not_set_auto(w, role):
    """The routing bypass, closed: a rule's `auto` ran every matching turn auto,
    around the dispatch gate that keeps `auto` from a non-admin."""
    box = _runner(w["owner"], "owners-box")         # may hold the agent
    RunnerAdmin.objects.create(runner=box, user=w[role])
    c = Client()
    c.force_login(w[role])
    r = _rules(c, [_rule(box, "auto")])
    assert r.status_code == 403 and "turn_mode=auto" in r.json()["title"], r.content
    path = "/api/agents/agent/actor-routes/beth@dimagi.com"
    runners = [{"runner_id": str(box.id), "enabled": True}]
    r = c.put(path, {"runners": runners, "turn_mode": "auto"}, content_type="application/json")
    assert r.status_code == 403 and "turn_mode=auto" in r.json()["title"], r.content
    for mode in ("manual", ""):
        r = c.put(path, {"runners": runners, "turn_mode": mode}, content_type="application/json")
        assert r.status_code == 200, r.content


def test_the_agents_own_switch_is_the_last_rung(w):
    w["agent"].turn_mode = "manual"
    w["agent"].save()
    ed, owner = Client(), Client()
    ed.force_login(w["editor"])
    owner.force_login(w["owner"])
    path = "/api/agents/agent/turn-mode"
    assert ed.patch(path, {"turn_mode": "auto"}, content_type="application/json").status_code == 403
    assert owner.patch(path, {"turn_mode": "auto"}, content_type="application/json").status_code == 200
    assert ed.patch(path, {"turn_mode": "manual"}, content_type="application/json").status_code == 200
