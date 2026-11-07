"""THE rule, asked through every entry point — docs/architecture/access.md.

Owner decisions of 2026-10-04: one function (`apps.agents.turn_context.resolve_turn_context`) decides
what a person may make an agent do, and every entry point that makes an agent
work asks it — the harness API, a canopy-web chat send, Slack intake, email
intake and the caller envelope. This matrix walks roles × entry points ×
access list (none / editors only / Eva-like "members and verified dimagi staff are editors" /
Hal-like ask-only, each with and without the editors line; set up through
`tests/access_helpers`) and pins, for each cell, the profile the turn runs in, why
(`granted_by`), and the mode it is claimed in — plus who may REQUEST `auto`, the
pin rule, and the routing-auto gate.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import access, roles, turn_context
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions import services as chat
from apps.harness import caller_context, services, turn_mode
from apps.harness import initiator as who
from apps.harness.models import Runner, RunnerAdmin, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import allow, declare, editors_run

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

#: Eva's real setup: members and verified dimagi staff get her whole.
def _eva(agent):
    allow(agent, role=roles.EDITOR)
    allow(agent, role=roles.EDITOR, contact=True, domain="dimagi.com", verified=True)


#: Hal-like: everyone not an admin is confined to read mode (published as `ask`).
HAL = {"ask": {"description": "Ask", "tools": ["Read"]}}


def _hal(agent):
    declare(agent, HAL)
    allow(agent, role=roles.READ_ONLY)
    allow(agent, role=roles.READ_ONLY, contact=True)


#: How an agent is set up, and whether the workspace-editors-drive-it-manually line is also on
#: the list. `none` is the default (an agent nothing was said about: owner and admins only);
#: `editors` is that line alone; `*_confined` leave it off.
IFACES = {"none": (None, False), "editors": (None, True), "eva": (_eva, True), "hal": (_hal, True),
          "eva_confined": (_eva, False), "hal_confined": (_hal, False)}
#: Why, for each access-list line the matrix uses (`TurnContext.basis`).
MEMBER_LINE = "grant:workspace:connect::"
EDITORS_LINE = "grant:workspace:connect:editor:"
DIMAGI_STAFF_LINE = "grant:contact:dimagi.com"


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


def _publish(agent, iface):
    setup, editors = IFACES[iface]
    declare(agent, None)
    if setup:
        setup(agent)
    if editors:
        editors_run(agent)


# --- the entry points ---------------------------------------------------------------------

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
    _msg, turn = session_send.send_message(session=session, text="go", user=user, client_id=key,
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


ENTRY_POINTS = {"api": _via_api, "chat": _via_chat, "slack": _via_slack}

#: What each member role reaches, per interface: (profile, granted_by, mode at claim).
#: The agent's own switch is `auto`, so `manual` here is an editor line that says `manual_only`.
OWNER_ROW = {i: ("full", "owner", "auto") for i in IFACES}
ADMIN_ROW = {i: ("full", "admin", "auto") for i in IFACES}
REFUSED = (None, "refused", None)
EXPECT = {
    "owner": OWNER_ROW, "ws_owner": ADMIN_ROW, "admin": ADMIN_ROW,
    # A workspace admin or editor holds no agent keys: only a line lets them in. The old
    # "editors drive it" rule is a line now, so it must be on the list for them to get in.
    "ws_admin": {"none": REFUSED, "editors": ("full", EDITORS_LINE, "manual"),
                 "eva": ("full", MEMBER_LINE, "auto"), "hal": ("full", EDITORS_LINE, "manual"),
                 "eva_confined": ("full", MEMBER_LINE, "auto"),
                 "hal_confined": ("confined", "capability:ask", "auto")},
    "editor": {"none": REFUSED, "editors": ("full", EDITORS_LINE, "manual"),
               "eva": ("full", MEMBER_LINE, "auto"), "hal": ("full", EDITORS_LINE, "manual"),
               "eva_confined": ("full", MEMBER_LINE, "auto"),
               "hal_confined": ("confined", "capability:ask", "auto")},
    "viewer": {"none": REFUSED, "editors": REFUSED, "eva": ("full", MEMBER_LINE, "auto"),
               "hal": ("confined", "capability:ask", "auto"), "eva_confined": ("full", MEMBER_LINE, "auto"),
               "hal_confined": ("confined", "capability:ask", "auto")},
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
@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_members_through_every_signed_in_entry_point(w, entry, role, iface):
    _publish(w["agent"], iface)
    user = w[role]
    turn = ENTRY_POINTS[entry](w, user, f"{entry}-{role}-{iface}")
    want = EXPECT[role][iface]
    if entry == "api" and role == "viewer":
        # The harness API's agent branch is the editor tier's entry point: a viewer
        # talks to an agent by chat, Slack or a host site's MCP tool instead.
        assert turn is None
        return
    if turn is None:        # refused on the way in: the list does not name them
        assert want[0] is None
        return
    _check(turn, want)


@pytest.mark.parametrize("iface", list(IFACES))
@pytest.mark.parametrize("role", list(EXPECT))
def test_members_by_verified_email(w, role, iface):
    """A member proven by THIS message's DMARC is that member (`member_behind_email`)."""
    _publish(w["agent"], iface)
    _check(_via_email(w, w[role].email, f"{role}-{iface}"), EXPECT[role][iface])


STAFF = {"full": (("full", DIMAGI_STAFF_LINE, "auto")), "read_mode": ("confined", "capability:ask", "auto")}
CONTACTS = {
    # (address, verified) -> per interface; `editors` lines are about members only
    ("staff@dimagi.com", True): {"none": REFUSED, "editors": REFUSED, "eva": STAFF["full"],
                                 "hal": STAFF["read_mode"]},
    ("staff@dimagi.com", False): {"none": REFUSED, "editors": REFUSED, "eva": REFUSED, "hal": STAFF["read_mode"]},
    ("someone@partner.org", True): {"none": REFUSED, "editors": REFUSED, "eva": REFUSED, "hal": STAFF["read_mode"]},
}
for _row in CONTACTS.values():
    _row["eva_confined"], _row["hal_confined"] = _row["eva"], _row["hal"]


@pytest.mark.parametrize("iface", list(IFACES))
@pytest.mark.parametrize("sender", list(CONTACTS))
def test_contacts_by_email(w, sender, iface):
    _publish(w["agent"], iface)
    address, verified = sender
    turn = _via_email(w, address, f"{address}-{verified}-{iface}", verified=verified)
    _check(turn, CONTACTS[sender][iface])
    env = caller_context.build(turn)
    assert env["relationship"] == "contact"


def test_a_refusal_says_what_the_caller_can_do(w):
    _publish(w["agent"], "none")
    t = _via_chat(w, w["viewer"], "r1")
    assert "access list" in t.result_note and "editor" in t.result_note and w["owner"].email in t.result_note
    declare(w["agent"], {"ask": {}, "status": {}})
    allow(w["agent"], role=roles.READ_ONLY)
    allow(w["agent"], capability="status")
    editors_run(w["agent"])
    d = turn_context.for_asker(w["agent"], w["viewer"], verified=True, requested="deploy")
    assert d.access == turn_context.REFUSED        # a line cannot use a profile the agent does not have
    # Why, in plain words, and who can fix it, by name (`apps/agents/refusals.py`). Naming one the agent does not
    # have is fixed by not naming it, so the fix itself names nobody.
    assert "access list" in d.refusal.why and "Send without naming 'deploy'" in d.refusal.how_to_fix
    assert w["owner"].email in d.refusal.who_can_fix
    assert turn_context.for_asker(w["agent"], w["viewer"], verified=True, requested="status").access == access.CONFINED


def test_the_mcp_tool_list_follows_the_rule(w):
    from apps.agents.published_profiles import offered_to

    _publish(w["agent"], "hal")
    assert offered_to(w["editor"], w["agent"]) == ["ask"]      # a whole-agent line: every profile
    assert offered_to(w["viewer"], w["agent"]) == ["ask"]
    AgentGrant.objects.filter(agent=w["agent"]).delete()
    declare(w["agent"], {"ask": {}, "admin_only": {}})
    allow(w["agent"], role=roles.READ_ONLY, contact=True)
    editors_run(w["agent"])
    assert offered_to(w["editor"], w["agent"]) == ["admin_only", "ask"]
    assert offered_to(w["viewer"], w["agent"]) == []


# --- the permission model: one line, one person, every entry point ----------------------------------

#: A person with ONE line of each kind on an agent that publishes read mode (`ask`) and `status` (no
#: profiles-only: who may use them is the list's business). (profile, why, mode) per line, through
#: the entry points that can confine. `read_only` is `read`: it opens read mode and nothing else; a
#: line naming one profile opens just that profile; `write` (read_write, editor) is the whole agent.
HAL_NO_CALLERS = {"ask": {"description": "Ask", "tools": ["Read"]},
                  "status": {"description": "Status", "tools": ["Read"]}}
LINES = {
    "read_only": {"role": roles.READ_ONLY},
    "read_write": {"role": roles.READ_WRITE},
    "editor": {"role": roles.EDITOR},
    "use_read_mode": {"capability": "ask"},
    "use_status": {"capability": "status"},
}


def _expect_line(kind, user):
    return {
        "read_only": ("confined", "capability:ask", "auto"),
        "read_write": ("full", f"grant:person:{user.pk}", "auto"),
        "editor": ("full", f"grant:person:{user.pk}", "auto"),
        "use_read_mode": ("confined", "capability:ask", "auto"),
        "use_status": REFUSED,          # a free-form message asks for read mode, and this line is not for it
    }[kind]


@pytest.mark.parametrize("kind", list(LINES))
@pytest.mark.parametrize("entry", ["chat", "slack"])
def test_each_kind_of_line_through_the_entry_points_that_can_confine(w, entry, kind):
    declare(w["agent"], HAL_NO_CALLERS)
    user = w["viewer"]
    AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=user, **LINES[kind])
    turn = ENTRY_POINTS[entry](w, user, f"line-{entry}-{kind}")
    _check(turn, _expect_line(kind, user))


@pytest.mark.parametrize("kind", list(LINES))
def test_each_kind_of_line_by_verified_email(w, kind):
    declare(w["agent"], HAL_NO_CALLERS)
    user = w["viewer"]
    AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=user, **LINES[kind])
    _check(_via_email(w, user.email, f"line-email-{kind}"), _expect_line(kind, user))


def test_the_api_route_gives_the_same_answer_to_a_workspace_editor(w):
    """The harness API asks the rule like every other entry point: `write` is the whole agent, `read`
    or a line naming one profile is a confined turn, and a line naming another profile is refused."""
    declare(w["agent"], HAL_NO_CALLERS)
    user = w["editor"]                    # a workspace editor may start work through the API
    for kind in LINES:
        AgentGrant.objects.filter(agent=w["agent"]).delete()
        AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=user, **LINES[kind])
        turn = _via_api(w, user, f"api-{kind}")
        want = _expect_line(kind, user)
        if want[0] is None:
            assert turn is None, kind
        else:
            _check(turn, want)


def test_read_mode_is_on_for_every_agent_and_the_owners_own_read_mode_overrides_it(w):
    """`read` opens read mode on every agent: canopy's default (stamp `read`) until the owner
    publishes a read mode of their own (`READ_MODE_STAMP`)."""
    declare(w["agent"], None)
    AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=w["viewer"], role=roles.READ_ONLY)
    _check(_via_chat(w, w["viewer"], "off"), ("confined", "capability:read", "auto"))
    declare(w["agent"], {"ask": {"tools": ["Read"]}})
    _check(_via_chat(w, w["viewer"], "on"), ("confined", "capability:ask", "auto"))


def test_manual_only_gates_whoever_is_driving_the_whole_agent_whatever_the_role(w):
    declare(w["agent"], HAL_NO_CALLERS)
    AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=w["viewer"],
                              role=roles.READ_WRITE, manual_only=True)
    _check(_via_chat(w, w["viewer"], "gated"), ("full", f"grant:person:{w['viewer'].pk}", "manual"))


def test_a_workspace_line_with_the_subtree_option_reaches_a_person_in_a_child(w):
    root = Workspace.objects.create(slug="org", display_name="Org", created_by=w["owner"])
    w["ws"].parent = root
    w["ws"].save()
    kid = User.objects.create_user("kid", "kid@dimagi.com", "pw")      # only in connect, below org
    M.objects.create(user=kid, workspace=w["ws"], role=M.VIEWER)
    declare(w["agent"], HAL_NO_CALLERS)
    line = AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.WORKSPACE, subject_workspace=root,
                                     role=roles.READ_ONLY)
    _check(_via_chat(w, kid, "plain"), REFUSED)                         # org alone does not reach a child
    line.include_below = True
    line.save()
    _check(_via_chat(w, kid, "below"), ("confined", "capability:ask", "auto"))


# --- requested mode ------------------------------------------------------------------

@pytest.mark.parametrize("role, may_auto", [
    ("owner", True), ("ws_owner", True), ("admin", True),
    ("ws_admin", False), ("editor", False),
])
@pytest.mark.parametrize("mode", ["", "manual", "auto"])
def test_who_may_request_which_mode(w, role, may_auto, mode):
    _publish(w["agent"], "editors")
    c = Client()
    c.force_login(w[role])
    body = {"agent_slug": "agent", "origin": "api", "idempotency_key": f"m-{role}-{mode}",
            "prompt": "go"}
    if mode:
        body["turn_mode"] = mode
    r = c.post("/api/harness/turns/", body, content_type="application/json")
    if mode == "auto" and not may_auto:
        assert r.status_code == 403
        assert "manual" in r.json()["detail"]
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
    assert turn_context.for_asker(w["agent"], w[role], verified=True).review.may_request_auto is may_auto


def test_an_editor_promoted_while_queued_gets_the_agents_mode_at_claim(w):
    _publish(w["agent"], "editors")
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
    _publish(w["agent"], "editors")
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
    assert r.status_code == 403 and "turn_mode=auto" in r.json()["detail"], r.content
    path = "/api/agents/agent/actor-routes/beth@dimagi.com"
    runners = [{"runner_id": str(box.id), "enabled": True}]
    r = c.put(path, {"runners": runners, "turn_mode": "auto"}, content_type="application/json")
    assert r.status_code == 403 and "turn_mode=auto" in r.json()["detail"], r.content
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
