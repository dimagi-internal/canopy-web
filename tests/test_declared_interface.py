"""The declared interface: what an agent offers people who are not its admins.

Phases 4-5 (canopy-web half) of the who-is-asking spec. An agent publishes
`config/interface.yaml`; from then on a turn from anyone but its owner, admins
or canopy itself runs in the profile their class is offered (read mode or a host site) — or
is refused.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import roles
from apps.agents.published_profiles import FULL_STAMP, READ_MODE_STAMP, InterfaceError, parse
from apps.agents.models import Agent, AgentAdmin
from apps.harness import caller_context, claim, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import allow, declare, editors_run

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DMARC = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=llo-foo.org"}]

#: What read mode IS: the part an interface still declares.
READ_MODE_PROFILE = dict(
    description="Ask ACE about your programme.",
    entry="/ace:ask --thread {thread_id}",
    tools=["Read", "mcp__canopy-web__who_is_asking"],
    bash=["canopy email read --repo . {thread_id}"],
    read_paths=["{cwd}/**"],
)
#: The same interface as it is published now: no one named, and no role: it says only what it is.
PLAIN = {"capabilities": {"ask": READ_MODE_PROFILE}}
#: A document in the retired words (who may use read mode inside the interface): refused.
RETIRED_WORDS = {"capabilities": {"ask": {**READ_MODE_PROFILE, "callers": ["contact:verified", "member"]}},
                 "callers_default": "none"}
#: The profiles most tests publish; who may use read mode is the access list (`_publish`).
IFACE = {"ask": READ_MODE_PROFILE}


def _read_mode_for_verified_contacts_and_members(agent):
    allow(agent, role=roles.READ_ONLY, contact=True, verified=True)
    allow(agent, role=roles.READ_ONLY)


def _read_mode_for_contacts(agent):
    allow(agent, role=roles.READ_ONLY, contact=True)


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.EDITOR)
    # `ed` is the member these tests confine — a VIEWER: a workspace editor is in
    # the editor tier (whole agent, manual only; docs/architecture/access.md) and
    # is never confined by an interface.
    M.objects.create(user=ed, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    return {"op": op, "ed": ed, "ws": ws, "agent": agent}


def _publish(agent, caps=IFACE, lines=_read_mode_for_verified_contacts_and_members):
    """Declare `caps` and write the access-list `lines`, and workspace editors drive it manually."""
    declare(agent, caps)
    lines(agent)
    editors_run(agent)


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
    # Who may use an agent is its access list now: the old keys are refused, and say where to go.
    ({"capabilities": {"ask": {"callers": ["member"]}}}, "access list"),
    ({"full": ["member"], "capabilities": {}}, "access list"),
    ({"editors": "confined", "capabilities": {}}, "access list"),
    ({"capabilities": {"ask": {"role": "editor"}}}, "role"),
    ({"capabilities": {"Ask!": {}}}, "must match"),
    ({"capabilities": {}, "callers_default": "all"}, "callers_default"),
    ({"capabilities": {"ask": {"entry": "run everything"}}}, "plugin command"),
    ({"capabilities": {"ask": {"entry": "/help me"}}}, "plugin command"),
    ({"surprise": 1}, "unknown top-level"),
])
def test_parse_refuses_what_it_does_not_understand(doc, msg):
    with pytest.raises(InterfaceError, match=msg):
        parse(doc)


def test_parse_normalises():
    got = parse({"capabilities": {"ask": READ_MODE_PROFILE}})
    assert "version" not in got and "callers_default" not in got
    assert "role" not in got["capabilities"]["ask"]     # a capability says what it is, never who may use it
    assert got["capabilities"]["ask"]["bash"] == ["canopy email read --repo . {thread_id}"]


def test_write_paths_are_their_own_list_and_reach_the_profile():
    # One list for read and write let a caller overwrite the script its bash
    # allowlist runs (bin/ace-email) and then run it. Writes get their own list;
    # none published means none allowed (the guard reads an empty list as deny).
    from apps.agents.published_profiles import profile

    got = parse({"capabilities": {"ask": {**READ_MODE_PROFILE, "write_paths": ["{cwd}/.ace-ask/*"]}}})
    assert got["capabilities"]["ask"]["write_paths"] == ["{cwd}/.ace-ask/*"]
    assert parse({"capabilities": {"ask": READ_MODE_PROFILE}})["capabilities"]["ask"]["write_paths"] == []

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
    assert "access list" in t.result_note
    assert caller_context.build(t)["granted_by"] == "refused"


def test_without_a_line_a_workspace_editor_is_refused_too(w):
    # 2026-10-30: an agent nothing was said about is usable by its owner and admins only; a
    # workspace editor no longer gets in implicitly.
    from apps.workspaces import services as wsvc

    member = User.objects.create_user(username="m", email="m@dimagi.com")
    wsvc.ensure_member(w["agent"].workspace, member, WorkspaceMembership.EDITOR)
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="m0",
                                prompt="x", initiator=who.for_user(member, via="api", assurance="pat"))
    assert t.status == Turn.CANCELLED and "access list" in t.result_note
    assert caller_context.build(t)["granted_by"] == "refused"


def test_with_the_editors_line_an_editor_gets_the_whole_agent_manual(w):
    from apps.workspaces import services as wsvc

    editors_run(w["agent"])
    member = User.objects.create_user(username="m", email="m@dimagi.com")
    wsvc.ensure_member(w["agent"].workspace, member, WorkspaceMembership.EDITOR)
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="m1",
                                prompt="x", initiator=who.for_user(member, via="api", assurance="pat"))
    assert t.status == Turn.QUEUED and t.capability == FULL_STAMP
    env = caller_context.build(t)
    assert env["granted_by"].startswith("grant:workspace:connect:editor") and env["relationship"] == "member"
    assert env["turn_mode"]["mode"] == "manual"


def test_without_an_interface_a_viewer_is_refused(w):
    # 2026-10-04: no published interface = admins and editors only.
    t = _as_user(w["agent"], w["ed"], "v1")
    assert t.status == Turn.CANCELLED
    assert "access list" in t.result_note
    assert caller_context.build(t)["granted_by"] == "refused"


def test_an_agents_own_login_is_the_agent_itself(w):

    login = User.objects.create_user(username="ace-login", email="ace@dimagi-ai.com")
    w["agent"].user = login
    w["agent"].save()
    _publish(w["agent"])                      # even with an interface that confines callers
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="self",
                                prompt="x", initiator=who.for_user(login, via="api", assurance="pat"))
    assert t.capability == FULL_STAMP
    assert caller_context.build(t)["relationship"] == "system"


def test_another_agents_login_is_not_trusted_through_it(w):
    # Otherwise anyone with the whole of agent A could steer agent B through A.

    login = User.objects.create_user(username="hal-login", email="hal@dimagi-ai.com")
    Agent.objects.create(slug="hal", name="Hal", workspace=w["agent"].workspace, user=login)
    _publish(w["agent"])
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="other",
                                prompt="x", initiator=who.for_user(login, via="api", assurance="pat"))
    assert t.status == Turn.CANCELLED   # not the whole of ace — here, nothing at all
    assert caller_context.build(t)["relationship"] != "system"


# --- who gets what ---------------------------------------------------------------------

def test_a_verified_emailer_gets_read_mode_and_its_profile(w):
    _publish(w["agent"])
    t = _email(w["agent"], "e1", headers=DMARC)
    assert t.capability == READ_MODE_STAMP and t.status == Turn.QUEUED
    env = caller_context.build(t)
    assert env["profile"] == "confined"
    assert env["capability"]["entry"] == "/ace:ask --thread {thread_id}"
    assert env["capability"]["bash"] == ["canopy email read --repo . {thread_id}"]


def test_an_unverified_emailer_is_refused_when_read_mode_needs_verified(w):
    _publish(w["agent"])
    t = _email(w["agent"], "e1")
    assert t.status == Turn.CANCELLED and "access list" in t.result_note
    assert t.capability == FULL_STAMP        # never ran, so never had a profile


def test_owner_workspace_owner_and_admin_stay_full(w):
    _publish(w["agent"])
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    M.objects.create(user=boss, workspace=w["ws"], role=M.OWNER)
    AgentAdmin.objects.create(agent=w["agent"], user=w["ed"])
    for i, u in enumerate((w["op"], boss, w["ed"])):
        assert _as_user(w["agent"], u, f"k{i}").capability == FULL_STAMP


def test_a_plain_member_is_a_caller(w):
    _publish(w["agent"])
    t = _as_user(w["agent"], w["ed"], "k")
    assert t.capability == READ_MODE_STAMP


def test_a_member_is_refused_when_members_are_not_offered_read_mode(w):
    _publish(w["agent"], lines=_read_mode_for_contacts)
    assert _as_user(w["agent"], w["ed"], "k").status == Turn.CANCELLED


def test_a_schedule_stays_full(w):
    _publish(w["agent"])
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                 idempotency_key="s", initiator=who.system(via="schedule:1"))
    assert t.capability == FULL_STAMP


def test_an_email_thread_session_turn_is_decided_too(w):
    """Email turns bind to a SESSION, not the agent — the decision must follow."""
    _publish(w["agent"])
    t = _email(w["agent"], "e1", thread_id="t-1", headers=DMARC)
    assert t.chat_session_id and t.capability == READ_MODE_STAMP


def test_unpublishing_a_profile_denies_everything_to_turns_already_queued(w):
    """A turn restricted when queued must never become unrestricted because
    the file changed under it."""
    _publish(w["agent"])
    t = _email(w["agent"], "e1", headers=DMARC)
    w["agent"].interface = {}
    w["agent"].save(update_fields=["interface"])
    env = caller_context.build(t)
    assert env["profile"] == "confined"
    # Stamped `READ_MODE_STAMP` is read mode: with no override left it runs canopy's default read profile,
    # never the full one.
    assert env["capability"]["name"] == "ask" and env["capability"]["write_paths"] == ["{cwd}/.ace-read/*", "{cwd}/.ace-ask/*"]
    assert env["capability"]["default_profile_version"] == 4
    # A retired host site, though, yields the empty deny-all profile.
    t.capability = "retired-site"
    env = caller_context.build(t)
    assert env["profile"] == "confined"
    assert env["capability"]["tools"] == [] and env["capability"]["bash"] == []


# --- publishing -------------------------------------------------------------------------

def _client(u):
    c = Client()
    c.force_login(u)
    return c


def test_the_owner_publishes_and_any_member_reads(w):
    r = _client(w["op"]).put("/api/agents/ace/read-mode", READ_MODE_PROFILE, content_type="application/json")
    assert r.status_code == 200, r.content
    w["agent"].refresh_from_db()
    assert w["agent"].interface_published_by == w["op"]
    stored = w["agent"].interface["capabilities"]["ask"]
    assert "role" not in stored and "callers" not in stored
    got = _client(w["ed"]).get("/api/agents/ace/read-mode").json()
    assert got["source"] == "custom" and got["profile"]["tools"] == READ_MODE_PROFILE["tools"]


def test_the_old_who_keys_are_refused_and_point_at_the_access_list(w):
    with pytest.raises(InterfaceError, match="access list"):
        parse(RETIRED_WORDS)
    r = _client(w["op"]).put("/api/agents/ace/read-mode", {"callers": ["member"]},
                             content_type="application/json")
    assert r.status_code == 422


def test_an_editor_cannot_publish_it_is_a_security_policy(w):
    r = _client(w["ed"]).put("/api/agents/ace/read-mode", READ_MODE_PROFILE, content_type="application/json")
    assert r.status_code == 403


def test_a_bad_setting_is_a_422_not_a_silent_partial(w):
    before = Agent.objects.get(pk=w["agent"].pk).interface
    r = _client(w["op"]).put("/api/agents/ace/read-mode", {"tool": ["Read"]}, content_type="application/json")
    assert r.status_code == 422
    w["agent"].refresh_from_db()
    assert w["agent"].interface == before


def test_reset_returns_to_the_default_not_to_nothing(w):
    _publish(w["agent"])
    r = _client(w["op"]).delete("/api/agents/ace/read-mode")
    assert r.status_code == 200 and r.json()["source"] == "default"
    w["agent"].refresh_from_db()
    # `ed`'s members' read line still stands, and read mode needs no published profile.
    assert _as_user(w["agent"], w["ed"], "k").capability == "read"


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
    _beat(c, rid, profiles=3, envelope=2)
    _email(w["agent"], "e1", headers=DMARC)
    r = c.post(f"/api/harness/runners/{rid}/claim")
    assert r.status_code == 200
    assert r.json()["caller_context"]["profile"] == "confined"


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


def test_a_runner_whose_code_reads_only_restricted_never_claims_a_callers_turn(w, claimable):
    # Envelope VERSION 2 says `profile: confined`; runner code from before it tests
    # `== "restricted"` and would run the turn in the FULL profile. Such a box
    # reports no `envelope`, so it gets no caller turn until it updates.
    c, rid = claimable
    _beat(c, rid, profiles=3)
    _email(w["agent"], "e1", headers=DMARC)
    assert c.post(f"/api/harness/runners/{rid}/claim").status_code == 204


def test_envelope_cannot_be_declared_by_hand(w, claimable):
    c, rid = claimable
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {"agents": ["ace"], "envelope": 2}},
                content_type="application/json")
    assert r.status_code == 422


def test_turning_sessions_off_round_trips_what_the_box_reported(w, claimable):
    # The Slack & chat toggle sends back the capabilities it read: the box's own
    # report, unchanged, is an echo and must not 422 the write.
    c, rid = claimable
    _beat(c, rid, profiles=3, envelope=2)
    from apps.harness.models import Runner
    caps = dict(Runner.objects.get(pk=rid).capabilities)
    caps.pop("profiles", None)
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {**caps, "sessions": False}},
                content_type="application/json")
    assert r.status_code == 200, r.content
    after = Runner.objects.get(pk=rid).capabilities
    assert after["sessions"] is False and after["envelope"] == 2 and after["profiles"] == 3


def test_a_changed_reported_value_is_still_refused(w, claimable):
    c, rid = claimable
    _beat(c, rid, profiles=3, envelope=2)
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {"agents": ["ace"], "envelope": 3}},
                content_type="application/json")
    assert r.status_code == 422


@pytest.mark.parametrize("key", ["data_mode", "data_mode_hash"])
def test_the_data_mode_cannot_be_declared_by_hand(w, claimable, key):
    # A `sensitive_data` declaration counts only while the box itself reports sensitive-data mode
    # (runner_requirements.in_force): a hand edit would make the report a person's.
    c, rid = claimable
    r = c.patch(f"/api/harness/runners/{rid}", {"capabilities": {"agents": ["ace"], key: "sensitive_data"}},
                content_type="application/json")
    assert r.status_code == 422


def test_a_downgraded_runner_stops_claiming_on_its_next_beat(w, claimable):
    c, rid = claimable
    _beat(c, rid, profiles=3, envelope=2)
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
    rows = claim.unclaimable_queued_turns(w["op"])
    assert [r["turn_id"] for r in rows] == [str(t.pk)]
    assert "confine" in rows[0]["reason"] and rows[0]["kind"] == "config"


# --- held on canopy-web as YAML ----------------------------------------------------------

def test_full_rules_are_refused_and_point_at_the_access_list():
    with pytest.raises(InterfaceError, match="access list"):
        parse({"full": ["everyone"]})
    with pytest.raises(InterfaceError, match="access list"):
        parse({"full": ["contact@*.com"]})


# --- a page's resource selects nothing ----------------------------------------------------
# A profile is chosen by the host site its conversation is held on, never by what a page
# says it is showing: `pages:` is gone, and a document still carrying it is refused, not ignored.

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


def test_a_conversation_on_a_page_that_declared_a_resource_runs_in_read_mode(w):
    _publish(w["agent"])
    t = _on_page(w["agent"], w["ed"], "p9", "labs-marketplace://orgs")
    assert t.capability == READ_MODE_STAMP


@pytest.mark.parametrize("pages", [["labs-marketplace://*"], []])
def test_a_profile_that_still_names_pages_is_refused(pages):
    with pytest.raises(InterfaceError, match="pages is gone") as raised:
        parse({"capabilities": {"m": {"tools": ["Read"], "pages": pages}}})
    assert raised.value.path == "m.pages"


# --- someone let into the owner's conversation is part of it ------------------------

def _owners_session(w):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["op"], title="t")


def _in_session(session, user, key):
    t, _ = services.enqueue_turn(
        origin=Turn.ORIGIN_SLACK, idempotency_key=key, session=session,
        initiator=who.for_user(user, via="slack:T1", assurance=who.SLACK_LINKED))
    return t


def test_an_editor_of_the_owners_session_answers_by_their_own_line(w):
    """Until 2026-11-06 an editor row in the owner's session ran the whole agent (`session_writer`). That rule
    is gone: their own read line gives read mode, here as anywhere."""
    from apps.canopy_sessions.models import SessionParticipant

    _publish(w["agent"])
    session = _owners_session(w)
    SessionParticipant.objects.create(session=session, user=w["ed"], role=SessionParticipant.EDITOR)
    t = _in_session(session, w["ed"], "s1")
    assert t.capability == READ_MODE_STAMP and t.status == Turn.QUEUED
    env = caller_context.build(t)
    assert env["granted_by"] == f"capability:{READ_MODE_STAMP}"


def test_without_a_grant_in_the_session_the_member_stays_confined(w):
    from apps.canopy_sessions.models import SessionParticipant

    _publish(w["agent"])
    session = _owners_session(w)
    assert _in_session(session, w["ed"], "s2").capability == READ_MODE_STAMP
    SessionParticipant.objects.create(session=session, user=w["ed"], role=SessionParticipant.VIEWER)
    assert _in_session(session, w["ed"], "s3").capability == READ_MODE_STAMP


def test_a_session_a_member_started_themselves_grants_nothing(w):
    """Otherwise any member could start a chat and be its owner, undoing the interface."""
    from apps.canopy_sessions.models import Session, SessionParticipant

    _publish(w["agent"])
    other = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    M.objects.create(user=other, workspace=w["ws"], role=M.VIEWER)
    session = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=other, title="t")
    SessionParticipant.objects.create(session=session, user=w["ed"], role=SessionParticipant.EDITOR)
    assert _in_session(session, other, "s4").capability == READ_MODE_STAMP
    assert _in_session(session, w["ed"], "s5").capability == READ_MODE_STAMP


def test_retired_fields_are_ignored_not_refused_so_a_stored_interface_republishes(caplog):
    """Every interface saved before `input` and `version` were retired carries them (`input: {}` on
    each profile, `version: 1`); loading one and saving it back must not 422."""
    stored = {"version": 1, "callers_default": "none",
              "capabilities": {"ask": {**READ_MODE_PROFILE, "input": {"opportunity_id": "integer"}}, "x": {"input": {}}}}
    with caplog.at_level("WARNING"):
        got = parse(stored)
    assert "version" not in got and "callers_default" not in got
    assert all("input" not in cap for cap in got["capabilities"].values())
    assert "ask.input" in caplog.text and "version" in caplog.text
    assert parse(got) == got                                   # and what is written back parses to itself
