"""The caller envelope: who asked, how sure we are, and what canopy knows about them.

Phase 1b of the who-is-asking spec. Phase 1a recorded the initiator; this is the
half that reaches the agent — in the claim response (the runner writes it to
disk), over REST, and as the `who_is_asking` MCP tool for a mid-turn re-read.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.contacts import services as contacts
from apps.contacts.models import Contact
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

PASS_ALL = ("mx.google.com; dkim=pass header.i=@llo-foo.org; spf=pass "
            "smtp.mailfrom=llo-foo.org; dmarc=pass header.from=llo-foo.org")
HDRS = [{"name": "Authentication-Results", "value": PASS_ALL}]


@pytest.fixture()
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=owner)
    return owner, ws, agent


def _email(agent, key="e-1", thread="", **ref):
    base = {"from": "fatima@llo-foo.org", "subject": "payments?"}
    if thread:
        base["thread_id"] = thread
    turn, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL,
                                    idempotency_key=key, origin_ref={**base, **ref})
    return turn


# --- what the envelope says -------------------------------------------------------

def test_a_verified_email_contact_carries_their_profile(ctx):
    _o, ws, agent = ctx
    c = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    Contact.objects.filter(pk=c.pk).update(
        notes="Program lead at LLO Foo.", attributes={"org": "LLO Foo", "connect_opp": 42})
    env = caller_context.build(_email(agent, headers=HDRS))

    assert env["version"] == caller_context.VERSION
    assert env["agent"] == "ace"
    assert env["who"]["kind"] == who.CONTACT
    assert env["verified"] is True
    assert env["relationship"] == caller_context.CALLER
    assert env["contact"]["notes"] == "Program lead at LLO Foo."
    assert env["contact"]["attributes"] == {"org": "LLO Foo", "connect_opp": 42}
    assert env["contact"]["this_message_grade"] == Contact.AUTH_DMARC
    assert env["conversation"]["subject"] == "payments?"
    assert env["capability"] is None


def test_an_unverified_email_is_not_verified(ctx):
    _o, _ws, agent = ctx
    env = caller_context.build(_email(agent))
    assert env["verified"] is False
    assert env["contact"]["this_message_grade"] == Contact.AUTH_NONE


def test_a_spoof_of_a_once_verified_address_is_not_verified(ctx):
    """The bug in phase 1a: the turn was stamped with the contact's BEST-ever
    grade, so a forged message from an address that once passed DMARC read as
    `dmarc`. Both the turn and the envelope must describe THIS message."""
    _o, _ws, agent = ctx
    _email(agent, key="real", headers=HDRS)            # Fatima, genuinely
    forged = _email(agent, key="spoof")                # no auth at all
    assert forged.initiator_assurance == Contact.AUTH_NONE
    env = caller_context.build(forged)
    assert env["verified"] is False
    assert env["contact"]["best_grade"] == Contact.AUTH_DMARC     # history, visible
    assert env["contact"]["this_message_grade"] == Contact.AUTH_NONE


def test_dkim_alone_is_not_verified(ctx):
    """DKIM proves a domain signed it, not that the visible From is that domain."""
    _o, _ws, agent = ctx
    turn = _email(agent, headers=[{"name": "Authentication-Results",
                                   "value": "mx.google.com; dkim=pass; dmarc=fail"}])
    assert turn.initiator_assurance == Contact.AUTH_DKIM
    assert caller_context.build(turn)["verified"] is False


def test_the_owner_is_the_owner_and_a_member_is_a_member(ctx):
    owner, ws, agent = ctx
    mem = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=mem, workspace=ws, role=WorkspaceMembership.EDITOR)
    stranger = User.objects.create_user("zz", "zz@else.org", "pw")
    for user, want in ((owner, "owner"), (mem, "member"), (stranger, "caller")):
        t, _ = services.enqueue_turn(
            agent=agent, origin=Turn.ORIGIN_API, idempotency_key=f"m-{user.pk}",
            initiator=who.for_user(user, via="chat", assurance=who.SESSION))
        env = caller_context.build(t)
        assert env["relationship"] == want, user.username
        assert env["verified"] is True
        assert env["contact"] is None


def test_a_schedule_is_system(ctx):
    owner, _ws, agent = ctx
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                 idempotency_key="s", initiator=who.system(
                                     via="schedule:1", accountable=owner))
    env = caller_context.build(t)
    assert env["relationship"] == caller_context.SYSTEM
    assert env["verified"] is True


def test_an_email_thread_names_its_session(ctx):
    _o, _ws, agent = ctx
    env = caller_context.build(_email(agent, thread="t-7"))
    assert env["conversation"]["thread_id"] == "t-7"
    assert env["conversation"]["session_id"]
    assert env["agent"] == "ace"          # derived through the session


# --- how it reaches the runner ----------------------------------------------------

def test_the_claim_response_carries_the_envelope(ctx):
    owner, _ws, agent = ctx
    c = Client()
    c.force_login(owner)
    r = c.post("/api/harness/runners/", {"name": "box", "kind": "emdash",
                                         "capabilities": {"agents": ["ace"]}},
               content_type="application/json")
    rid = r.json()["id"]
    c.post(f"/api/harness/runners/{rid}/heartbeat",
           {"active_turn_ids": [], "degraded": False, "note": ""},
           content_type="application/json")
    RunnerAssignment.objects.create(agent=agent, runner_id=rid, rank=0)
    _email(agent, headers=HDRS)

    claim = c.post(f"/api/harness/runners/{rid}/claim")
    assert claim.status_code == 200, claim.content
    env = claim.json()["caller_context"]
    assert env["who"]["contact"]["email"] == "fatima@llo-foo.org"
    assert env["verified"] is True


def test_a_turn_listing_does_not_spread_the_profile(ctx):
    owner, _ws, agent = ctx
    _email(agent)
    c = Client()
    c.force_login(owner)
    rows = c.get("/api/harness/turns/").json()
    assert rows and "caller_context" not in rows[0]


def test_the_rest_route_is_tenant_gated(ctx):
    owner, _ws, agent = ctx
    turn = _email(agent)
    c = Client()
    c.force_login(owner)
    r = c.get(f"/api/harness/turns/{turn.pk}/caller-context")
    assert r.status_code == 200 and r.json()["envelope"]["turn_id"] == str(turn.pk)

    outsider = User.objects.create_user("x", "x@else.org", "pw")
    Workspace.objects.create(slug="other", display_name="Other", created_by=outsider)
    c2 = Client()
    c2.force_login(outsider)
    assert c2.get(f"/api/harness/turns/{turn.pk}/caller-context").status_code == 404


# --- the MCP re-read --------------------------------------------------------------

@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id=str(user.pk), scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _who(turn_id):
    return async_to_sync(mcp.call_tool)("who_is_asking", {"turn_id": turn_id}).structured_content


def test_who_is_asking_is_registered_and_returns_the_envelope(ctx):
    owner, _ws, agent = ctx
    turn = _email(agent, headers=HDRS)
    with as_user(owner):
        env = _who(str(turn.pk))
    assert env["verified"] is True
    assert env["who"]["contact"]["email"] == "fatima@llo-foo.org"


def test_who_is_asking_reads_live_state_not_the_claim_snapshot(ctx):
    """The reason to re-read: a contact blocked mid-turn must show as blocked."""
    owner, ws, agent = ctx
    turn = _email(agent)
    contacts.block(Contact.objects.get(workspace=ws, email="fatima@llo-foo.org"))
    with as_user(owner):
        assert _who(str(turn.pk))["contact"]["is_blocked"] is True


@pytest.mark.parametrize("turn_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_who_is_asking_fails_closed_on_a_bad_id(ctx, turn_id):
    owner, _ws, _agent = ctx
    with as_user(owner), pytest.raises(Exception, match="turn not found"):
        _who(turn_id)


def test_who_is_asking_refuses_another_tenant(ctx):
    _owner, _ws, agent = ctx
    turn = _email(agent)
    outsider = User.objects.create_user("x", "x@else.org", "pw")
    with as_user(outsider), pytest.raises(Exception, match="turn not found"):
        _who(str(turn.pk))
