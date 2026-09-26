"""The declared interface: what an agent offers people who are not its admins.

Phases 4-5 (canopy-web half) of the who-is-asking spec. An agent publishes
`config/interface.yaml`; from then on a turn from anyone but its owner, admins
or canopy itself runs in the capability their class is offered — or is refused.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import ASK, FULL, InterfaceError, parse
from apps.agents.models import Agent, AgentAdmin
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DMARC = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=llo-foo.org"}]

IFACE = {"capabilities": {"ask": {
    "description": "Ask ACE about your programme.",
    "callers": ["contact:verified", "member"],
    "entry": "/ace:ask --thread {thread_id}",
    "tools": ["Read", "mcp__canopy-web__who_is_asking"],
    "bash": ["canopy email read --repo . {thread_id}"],
    "read_paths": ["{cwd}/**"],
}}, "callers_default": "none"}


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.EDITOR)
    M.objects.create(user=ed, workspace=ws, role=M.EDITOR)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    return {"op": op, "ed": ed, "ws": ws, "agent": agent}


def _publish(agent, doc=IFACE):
    agent.interface = parse(doc)
    agent.save(update_fields=["interface"])


def _email(agent, key, **ref):
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                                 origin_ref={"from": "fatima@llo-foo.org", **ref})
    return t


def _as_user(agent, user, key):
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key,
                                 initiator=who.for_user(user, via="chat", assurance=who.SESSION))
    return t


# --- parsing is strict -------------------------------------------------------------

@pytest.mark.parametrize("doc, msg", [
    ({"capabilities": {"ask": {"tool": ["Read"]}}}, "unknown key"),
    ({"capabilities": {"ask": {"callers": ["everyone"]}}}, "not a caller class"),
    ({"capabilities": {"ask": {"callers": ["contact:signed"]}}}, "not a caller class"),
    ({"capabilities": {"Ask!": {}}}, "must match"),
    ({"capabilities": {}, "callers_default": "all"}, "callers_default"),
    ({"capabilities": {"ask": {"entry": "run everything"}}}, "slash command"),
    ({"surprise": 1}, "unknown top-level"),
])
def test_parse_refuses_what_it_does_not_understand(doc, msg):
    with pytest.raises(InterfaceError, match=msg):
        parse(doc)


def test_parse_normalises():
    got = parse(IFACE)
    assert got["version"] == 1 and got["callers_default"] == "none"
    assert got["capabilities"]["ask"]["bash"] == ["canopy email read --repo . {thread_id}"]


def test_write_paths_are_their_own_list_and_reach_the_profile():
    # One list for read and write let a caller overwrite the script its bash
    # allowlist runs (bin/ace-email) and then run it. Writes get their own list;
    # none published means none allowed (the guard reads an empty list as deny).
    from apps.agents.interface import profile

    got = parse({"capabilities": {"ask": {**IFACE["capabilities"]["ask"],
                                          "write_paths": ["{cwd}/.ace-ask/*"]}}})
    assert got["capabilities"]["ask"]["write_paths"] == ["{cwd}/.ace-ask/*"]
    assert parse(IFACE)["capabilities"]["ask"]["write_paths"] == []

    class _A:
        interface = got
    assert profile(_A(), "ask")["write_paths"] == ["{cwd}/.ace-ask/*"]
    assert profile(_A(), "gone")["write_paths"] == []   # unpublished → deny everything


# --- no interface: default deny, except the people the workspace let in ------------

def test_without_an_interface_a_contact_is_refused(w):
    # Until 2026-09-26 an agent with no interface ran every turn FULL, so a
    # stranger on Slack or email drove the whole agent (Hal, via Slack, that day).
    t = _email(w["agent"], "e1")
    assert t.status == Turn.CANCELLED
    assert "offers nothing" in t.result_note
    assert caller_context.build(t)["granted_by"] == "refused"


def test_without_an_interface_a_member_still_gets_the_whole_agent(w):
    from apps.workspaces import services as wsvc

    member = User.objects.create_user(username="m", email="m@dimagi.com")
    wsvc.ensure_member(w["agent"].workspace, member, WorkspaceMembership.EDITOR)
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="m1",
                                prompt="x", initiator=who.for_user(member, via="api", assurance="pat"))
    assert t.status == Turn.QUEUED and t.capability == FULL
    assert caller_context.build(t)["granted_by"] == "no-interface"


def test_an_agents_own_login_is_the_agent_itself(w):

    login = User.objects.create_user(username="ace-login", email="ace@dimagi-ai.com")
    w["agent"].user = login
    w["agent"].interface = parse(IFACE)       # even with an interface that confines callers
    w["agent"].save()
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="self",
                                prompt="x", initiator=who.for_user(login, via="api", assurance="pat"))
    assert t.capability == FULL
    assert caller_context.build(t)["relationship"] == "system"


def test_another_agents_login_is_not_trusted_through_it(w):
    # Otherwise anyone with the whole of agent A could steer agent B through A.

    login = User.objects.create_user(username="hal-login", email="hal@dimagi-ai.com")
    Agent.objects.create(slug="hal", name="Hal", workspace=w["agent"].workspace, user=login)
    w["agent"].interface = parse(IFACE)
    w["agent"].save()
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="other",
                                prompt="x", initiator=who.for_user(login, via="api", assurance="pat"))
    assert t.status == Turn.CANCELLED   # not the whole of ace — here, nothing at all
    assert caller_context.build(t)["relationship"] != "system"


# --- who gets what ---------------------------------------------------------------------

def test_a_verified_emailer_gets_ask_and_its_profile(w):
    _publish(w["agent"])
    t = _email(w["agent"], "e1", headers=DMARC)
    assert t.capability == ASK and t.status == Turn.QUEUED
    env = caller_context.build(t)
    assert env["profile"] == "restricted"
    assert env["capability"]["entry"] == "/ace:ask --thread {thread_id}"
    assert env["capability"]["bash"] == ["canopy email read --repo . {thread_id}"]


def test_an_unverified_emailer_is_refused_when_ask_needs_verified(w):
    _publish(w["agent"])
    t = _email(w["agent"], "e1")
    assert t.status == Turn.CANCELLED and "offers nothing to this caller" in t.result_note
    assert t.capability == FULL        # never ran, so never had a profile


def test_owner_workspace_owner_and_admin_stay_full(w):
    _publish(w["agent"])
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    M.objects.create(user=boss, workspace=w["ws"], role=M.OWNER)
    AgentAdmin.objects.create(agent=w["agent"], user=w["ed"])
    for i, u in enumerate((w["op"], boss, w["ed"])):
        assert _as_user(w["agent"], u, f"k{i}").capability == FULL


def test_a_plain_member_is_a_caller(w):
    _publish(w["agent"])
    t = _as_user(w["agent"], w["ed"], "k")
    assert t.capability == ASK


def test_a_member_is_refused_when_members_are_not_offered_ask(w):
    doc = {"capabilities": {"ask": {**IFACE["capabilities"]["ask"], "callers": ["contact"]}}}
    _publish(w["agent"], doc)
    assert _as_user(w["agent"], w["ed"], "k").status == Turn.CANCELLED


def test_a_schedule_stays_full(w):
    _publish(w["agent"])
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                 idempotency_key="s", initiator=who.system(via="schedule:1"))
    assert t.capability == FULL


def test_an_email_thread_session_turn_is_decided_too(w):
    """Email turns bind to a SESSION, not the agent — the decision must follow."""
    _publish(w["agent"])
    t = _email(w["agent"], "e1", thread_id="t-1", headers=DMARC)
    assert t.chat_session_id and t.capability == ASK


def test_unpublishing_a_capability_denies_everything_to_turns_already_queued(w):
    """A turn restricted when queued must never become unrestricted because
    the file changed under it."""
    _publish(w["agent"])
    t = _email(w["agent"], "e1", headers=DMARC)
    w["agent"].interface = {}
    w["agent"].save(update_fields=["interface"])
    env = caller_context.build(t)
    assert env["profile"] == "restricted"
    assert env["capability"]["tools"] == [] and env["capability"]["bash"] == []


# --- publishing -------------------------------------------------------------------------

def _client(u):
    c = Client()
    c.force_login(u)
    return c


def test_the_owner_publishes_and_any_member_reads(w):
    r = _client(w["op"]).put("/api/agents/ace/interface", {"interface": IFACE},
                             content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["published_by_email"] == "op@dimagi.com"
    got = _client(w["ed"]).get("/api/agents/ace/interface").json()
    assert got["interface"]["capabilities"]["ask"]["callers"] == ["contact:verified", "member"]


def test_an_editor_cannot_publish_it_is_a_security_policy(w):
    r = _client(w["ed"]).put("/api/agents/ace/interface", {"interface": IFACE},
                             content_type="application/json")
    assert r.status_code == 403


def test_a_bad_interface_is_a_422_not_a_silent_partial(w):
    r = _client(w["op"]).put("/api/agents/ace/interface",
                             {"interface": {"capabilities": {"ask": {"tool": ["Read"]}}}},
                             content_type="application/json")
    assert r.status_code == 422


def test_unpublish_reverts_to_full(w):
    _publish(w["agent"])
    r = _client(w["op"]).delete("/api/agents/ace/interface")
    assert r.status_code == 200 and r.json()["interface"] == {}
    w["agent"].refresh_from_db()
    assert _as_user(w["agent"], w["ed"], "k").capability == FULL


# --- only a runner that can confine a caller's session may claim one ----------------

def _runner(client, name="box"):
    r = client.post("/api/harness/runners/", {"name": name, "kind": "emdash",
                                              "capabilities": {"agents": ["ace"]}},
                    content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()["id"]


def _beat(client, rid, **extra):
    return client.post(f"/api/harness/runners/{rid}/heartbeat",
                       {"active_turn_ids": [], "degraded": False, "note": "", **extra},
                       content_type="application/json")


@pytest.fixture()
def claimable(w):
    from apps.harness.models import RunnerAssignment

    _publish(w["agent"])
    c = _client(w["op"])
    rid = _runner(c)
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    return c, rid


def test_a_runner_that_cannot_confine_never_claims_a_callers_turn(w, claimable):
    c, rid = claimable
    assert _beat(c, rid).status_code == 200            # an older runner: no `profiles`
    _email(w["agent"], "e1", headers=DMARC)
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 204


def test_the_same_runner_still_claims_full_turns(w, claimable):
    c, rid = claimable
    _beat(c, rid)
    _as_user(w["agent"], w["op"], "k")                 # the owner: full profile
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 200


def test_a_runner_reporting_profiles_claims_it(w, claimable):
    c, rid = claimable
    _beat(c, rid, profiles=3)
    _email(w["agent"], "e1", headers=DMARC)
    r = c.post(f"/api/harness/runners/{rid}/claim")
    assert r.status_code == 200
    assert r.json()["caller_context"]["profile"] == "restricted"


def test_a_pin_is_not_a_way_past_it(w, claimable):
    c, rid = claimable
    _beat(c, rid)
    t = _email(w["agent"], "e1", headers=DMARC)
    Turn.objects.filter(pk=t.pk).update(pinned_runner_id=rid)
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 204


def test_a_runner_whose_guard_predates_write_paths_is_not_given_one(w, claimable):
    # Guard 2 checked Write against read_paths: a caller could overwrite the
    # script its bash allowlist runs. Such a runner must not get a caller turn.
    c, rid = claimable
    _beat(c, rid, profiles=2)
    _email(w["agent"], "e1", headers=DMARC)
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 204


def test_a_downgraded_runner_stops_claiming_on_its_next_beat(w, claimable):
    c, rid = claimable
    _beat(c, rid, profiles=3)
    _beat(c, rid)                                        # rolled back to an old build
    _email(w["agent"], "e1", headers=DMARC)
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 204


def test_profiles_cannot_be_declared_by_hand(w, claimable):
    c, rid = claimable
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {"agents": ["ace"], "profiles": 1}},
                content_type="application/json")
    assert r.status_code == 422
    rid2 = c.post("/api/harness/runners/", {"name": "b2", "kind": "emdash",
                                            "capabilities": {"agents": ["ace"], "profiles": 1}},
                  content_type="application/json").json()["id"]
    from apps.harness.models import Runner
    assert "profiles" not in Runner.objects.get(pk=rid2).capabilities


def test_a_stuck_callers_turn_says_why(w, claimable):
    from datetime import timedelta

    from django.utils import timezone

    c, rid = claimable
    _beat(c, rid)
    t = _email(w["agent"], "e1", headers=DMARC)
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(hours=1))
    rows = services.unclaimable_queued_turns(w["op"])
    assert [r["turn_id"] for r in rows] == [str(t.pk)]
    assert "confine" in rows[0]["reason"] and rows[0]["kind"] == "config"


# --- a page can pick the door --------------------------------------------------
# The gap this closes: every free-form channel collapsed to `ask`, so an agent
# embedded in a page shared one door with its mailbox. Equipping the page meant
# equipping an email stranger identically.

PAGED = {"capabilities": {
    "ask": IFACE["capabilities"]["ask"],
    "marketplace": {
        "description": "Ask about the organisations on this page.",
        "callers": ["contact", "member"],
        "pages": ["labs-marketplace://*"],
        "tools": ["Skill", "mcp__*connect_labs__marketplace_*"],
        "read_paths": ["{cwd}/**"],
    },
}, "callers_default": "none"}


def _on_page(agent, user, key, resource):
    """A chat turn whose conversation has declared a page, as the widget does."""
    from apps.canopy_sessions import page_state
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=agent.workspace, agent=agent, created_by=user)
    if resource:
        page_state.set_page_state(session, {"resource": resource, "visible_ids": ["a", "b"]})
        session.refresh_from_db()
    # A turn targets exactly one of agent / project / session; a widget's turn
    # is bound to the CONVERSATION, which is how the page reaches it at all.
    t, _ = services.enqueue_turn(
        origin=Turn.ORIGIN_API, idempotency_key=key, session=session,
        initiator=who.for_user(user, via="widget:connect-labs", assurance=who.SESSION))
    return t


def test_a_conversation_on_a_declared_page_runs_in_that_capability(w):
    _publish(w["agent"], PAGED)
    t = _on_page(w["agent"], w["ed"], "p1", "labs-marketplace://orgs")
    assert t.capability == "marketplace"


def test_the_same_caller_elsewhere_still_gets_ask(w):
    """The page selects a door; it does not change the agent for everyone."""
    _publish(w["agent"], PAGED)
    assert _as_user(w["agent"], w["ed"], "p2").capability == ASK
    assert _email(w["agent"], "p3", headers=DMARC).capability == ASK


def test_a_page_that_declared_nothing_gets_ask(w):
    _publish(w["agent"], PAGED)
    assert _on_page(w["agent"], w["ed"], "p4", "").capability == ASK


def test_an_unmatched_resource_gets_ask(w):
    _publish(w["agent"], PAGED)
    t = _on_page(w["agent"], w["ed"], "p5", "stock://on-hand")
    assert t.capability == ASK


def test_a_page_capability_a_caller_is_not_offered_is_skipped_not_fatal(w):
    """`ask` may still admit them, so a page door they cannot open must not
    refuse the turn outright."""
    doc = {"capabilities": {
        "ask": IFACE["capabilities"]["ask"],
        "marketplace": {**PAGED["capabilities"]["marketplace"], "callers": ["contact"]},
    }, "callers_default": "none"}
    _publish(w["agent"], doc)
    t = _on_page(w["agent"], w["ed"], "p6", "labs-marketplace://orgs")
    assert t.capability == ASK


def test_the_owner_is_unaffected_by_a_page(w):
    _publish(w["agent"], PAGED)
    assert _on_page(w["agent"], w["op"], "p7", "labs-marketplace://orgs").capability == FULL


def test_the_page_cannot_widen_what_the_capability_grants(w):
    """The security property. The page supplies the resource; the OWNER supplies
    the tools. A page declaring a resource reaches exactly the profile its owner
    wrote for that resource — never a tool nobody listed."""
    _publish(w["agent"], PAGED)
    t = _on_page(w["agent"], w["ed"], "p8", "labs-marketplace://orgs")
    profile = caller_context.build(t)
    assert profile["profile"] == "restricted"
    assert profile["capability"]["tools"] == ["Skill", "mcp__*connect_labs__marketplace_*"]
    assert profile["capability"]["bash"] == []


def test_an_existing_interface_is_unchanged_by_the_feature(w):
    """No `pages` anywhere means selection behaves exactly as before."""
    _publish(w["agent"])  # the original IFACE, no pages
    t = _on_page(w["agent"], w["ed"], "p9", "labs-marketplace://orgs")
    assert t.capability == ASK


@pytest.mark.parametrize("pattern, msg", [
    ("*", "matches every page"),
    ("labs-marketplace", "not a resource pattern"),
])
def test_a_pages_pattern_that_says_everything_is_refused(pattern, msg):
    doc = {"capabilities": {"m": {"callers": ["contact"], "pages": [pattern]}},
           "callers_default": "none"}
    with pytest.raises(InterfaceError, match=msg):
        parse(doc)


def test_pages_survives_a_round_trip_through_parse():
    out = parse(PAGED)
    assert out["capabilities"]["marketplace"]["pages"] == ["labs-marketplace://*"]
    assert out["capabilities"]["ask"]["pages"] == []
