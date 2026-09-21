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
    ({"capabilities": {"ask": {"callers": ["everyone"]}}}, "is not one of"),
    ({"capabilities": {"ask": {"callers": ["contact:signed"]}}}, "is not one of"),
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


# --- opt-in: nothing changes until an agent publishes ---------------------------------

def test_without_an_interface_every_turn_is_full(w):
    t = _email(w["agent"], "e1")
    assert t.capability == FULL and t.status == Turn.QUEUED
    assert caller_context.build(t)["profile"] == "full"


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
    _beat(c, rid, profiles=1)
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


def test_a_downgraded_runner_stops_claiming_on_its_next_beat(w, claimable):
    c, rid = claimable
    _beat(c, rid, profiles=1)
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
