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
from apps.harness.models import RunnerAssignment, Turn
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.agents.testing import admit_contacts

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
    admit_contacts(agent)
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


def test_the_page_the_person_is_on_is_the_selection_not_the_rows(ctx):
    # The widget used to paste the page state under the person's message, so the
    # agent had it on turn one. The envelope now carries the selection instead
    # (the hook puts it in context, outside the transcript); rows stay behind
    # the backing tool.
    _o, _ws, agent = ctx
    t = _email(agent, thread="t-9")
    t.chat_session.page_state = {"resource": "labs-marketplace://orgs",
                                 "visible_ids": ["org-alpha", "org-beta", "org-gamma"],
                                 "backing_tool": "marketplace_orgs_get", "path": "/labs/marketplace/"}
    t.chat_session.save()
    page = caller_context.build(t)["page"]
    assert page == {"resource": "labs-marketplace://orgs",
                    "visible_ids": ["org-alpha", "org-beta", "org-gamma"], "visible_count": 3,
                    "filters": None, "backing_tool": "marketplace_orgs_get",
                    "path": "/labs/marketplace/", "read_with": "current_page"}


def test_no_page_declared_is_null(ctx):
    _o, _ws, agent = ctx
    assert caller_context.build(_email(agent, thread="t-8"))["page"] is None


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


# --- why this turn exists: trigger + thread history ---------------------------------

def test_the_envelope_names_the_triggering_message(ctx):
    """ace@ 1a0d0a1632cfde4f: 14 confined sessions on SES receipts could not say which
    message started them, or whether push or poll found it."""
    _o, _ws, agent = ctx
    env = caller_context.build(_email(agent, key="email-ace-thr-9-21", thread="thr-9",
                                      discovered_by="push", message_id="18f-abc",
                                      message_count=21))
    t = env["trigger"]
    assert t["origin"] == Turn.ORIGIN_EMAIL
    assert t["discovered_by"] == "push"
    assert t["message_id"] == "18f-abc"
    assert t["message_count"] == 21
    assert t["from"] == "fatima@llo-foo.org"


def test_message_count_falls_back_to_the_idempotency_key(ctx):
    """Turns a pre-upgrade runner enqueued carry no `message_count`; the key does."""
    _o, _ws, agent = ctx
    env = caller_context.build(_email(agent, key="email-ace-thr-9-7", thread="thr-9"))
    assert env["trigger"]["message_count"] == 7
    assert env["trigger"]["message_id"] is None


def test_thread_history_counts_earlier_turns_on_the_same_thread(ctx):
    _o, _ws, agent = ctx
    first = _email(agent, key="email-ace-thr-9-1", thread="thr-9")
    _email(agent, key="email-ace-other-1", thread="other")
    third = _email(agent, key="email-ace-thr-9-3", thread="thr-9", message_count=3)
    h = caller_context.build(third)["thread_history"]
    assert h["prior_turns"] == 1
    assert h["last_prior_turn_id"] == str(first.pk)
    assert h["last_prior_message_count"] == 1
    assert caller_context.build(first)["thread_history"]["prior_turns"] == 0


def test_no_thread_means_no_thread_history(ctx):
    _o, _ws, agent = ctx
    assert caller_context.build(_email(agent, key="e-nothread"))["thread_history"] is None


# --- current_page, for a visitor who has no canopy account -------------------------
# Measured on connect-labs' marketplace panel: the agent called `current_page`,
# got `{"result":[]}`, and told the person it could not see their screen — while
# 23 declared organisations sat in front of them. A caller token's tools run as
# the CALLER, and a widget visitor is a contact with no canopy account, so the
# user-scoped predicate matched nothing. It was answering the wrong question:
# "whose pages may this USER see" rather than "what is THIS conversation showing".


@contextlib.contextmanager
def as_caller(turn, user=None):
    """A confined caller's token: names the conversation, and a user only when
    the caller happens to have an account (a contact does not)."""
    access = AccessToken(
        token="cct_x", client_id=f"turn:{turn.pk}", scopes=["canopy:turn"],
        claims={"sub": f"turn:{turn.pk}", "user_id": user.pk if user else None,
                "auth_method": "caller_token", "turn_id": str(turn.pk),
                "turn_ids": [str(turn.pk)], "tool_globs": ["*"]},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _current_page():
    return async_to_sync(mcp.call_tool)("current_page", {}).structured_content["result"]


def _widget_turn(agent, contact, state, key="w-1"):
    """A contact's conversation with a declared page, as the widget makes one."""
    from apps.canopy_sessions import page_state
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=agent.workspace, agent=agent, contact=contact)
    page_state.set_page_state(session, state)
    turn, _ = services.enqueue_turn(origin=Turn.ORIGIN_API, idempotency_key=key, session=session,
                                    initiator=who.for_contact(contact, via="widget:connect-labs"))
    return turn, session


@pytest.fixture()
def visitor(ctx):
    _owner, ws, agent = ctx
    contact = contacts.record_inbound_sender(workspace=ws, address="visitor@partner.org")
    return agent, contact


VIEW = {"resource": "labs-marketplace://orgs", "backing_tool": "marketplace_orgs_get",
        "visible_ids": ["acme-health", "beta-care"]}


def test_a_contact_visitor_can_have_their_page_read(visitor):
    """The one that was broken. No canopy account, and the page still answers."""
    agent, contact = visitor
    turn, session = _widget_turn(agent, contact, VIEW)

    with as_caller(turn):
        pages = _current_page()

    assert [p["session_id"] for p in pages] == [str(session.id)]
    assert pages[0]["state"]["visible_ids"] == ["acme-health", "beta-care"]
    assert pages[0]["version"] == 1


def test_it_answers_about_THIS_conversation_and_not_another(visitor):
    """Narrower than the user-scoped path, not wider: a second conversation's
    page — the same contact's — is not this screen."""
    agent, contact = visitor
    turn, session = _widget_turn(agent, contact, VIEW, key="w-1")
    _other_turn, other = _widget_turn(agent, contact, {"resource": "stock://on-hand"}, key="w-2")

    with as_caller(turn):
        pages = _current_page()

    ids = [p["session_id"] for p in pages]
    assert str(session.id) in ids
    assert str(other.id) not in ids


def test_a_conversation_with_no_declared_page_answers_empty(visitor):
    agent, contact = visitor
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=agent.workspace, agent=agent, contact=contact)
    turn, _ = services.enqueue_turn(origin=Turn.ORIGIN_API, idempotency_key="w-3", session=session,
                                    initiator=who.for_contact(contact, via="widget:connect-labs"))

    with as_caller(turn):
        assert _current_page() == []


def test_a_member_in_a_confined_turn_is_asked_about_the_screen_too(ctx, visitor):
    """A caller may hold an account. In THAT turn the question is still "this
    conversation" — their other tabs are a different question."""
    owner, ws, agent = ctx
    _agent, contact = visitor
    turn, session = _widget_turn(agent, contact, VIEW, key="w-4")

    with as_caller(turn, user=owner):
        pages = _current_page()

    assert [p["session_id"] for p in pages] == [str(session.id)]


def test_a_pat_still_answers_about_the_users_own_pages(ctx):
    """The unchanged path: no caller token, so the user-scoped predicate decides."""
    owner, ws, agent = ctx
    from apps.canopy_sessions import page_state
    from apps.canopy_sessions.models import Session

    mine = Session.objects.create(workspace=ws, agent=agent, created_by=owner)
    page_state.set_page_state(mine, VIEW)

    with as_user(owner):
        pages = _current_page()

    assert [p["session_id"] for p in pages] == [str(mine.id)]


# --- a turn with no agent: a repo chat ------------------------------------------------
#
# 2026-09-27: the "who is asking" note on Jonathan's own canopy-web repo chat called
# him a CALLER "not the agent's owner" and told the session not to push or deploy —
# because a turn with no agent fell straight through to CALLER.

def _repo_turn(ws, asker, *, creator=None, runner=None):
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=ws, title="repo chat", project="canopy-web",
                                     created_by=creator)
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=f"repo-{session.pk}", prompt="still going?",
                               claimed_by=runner, **who.for_user(asker, via="chat",
                                                                 assurance="session").fields())
    return turn


def test_the_person_whose_runner_does_the_work_owns_a_repo_chat(ctx):
    from apps.harness.models import Runner

    owner, ws, _agent = ctx
    laptop = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, paired_by=owner)
    env = caller_context.build(_repo_turn(ws, owner, runner=laptop))
    assert env["relationship"] == caller_context.OWNER


def test_the_creator_of_a_repo_chat_owns_it(ctx):
    owner, ws, _agent = ctx
    assert caller_context.build(_repo_turn(ws, owner, creator=owner))["relationship"] == \
        caller_context.OWNER


def test_someone_elses_repo_chat_on_someone_elses_box_is_not_theirs(ctx):
    from apps.harness.models import Runner

    owner, ws, _agent = ctx
    stranger = User.objects.create_user("x", "x@example.org", "pw")
    box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, paired_by=owner)
    env = caller_context.build(_repo_turn(ws, stranger, creator=owner, runner=box))
    assert env["relationship"] == caller_context.CALLER


# --- an agent's own login dispatching onto its owner's runner (canopy-web#1011) ------
#
# 2026-09-28: Jonathan asked an ACE session on emdash to dispatch a validation session
# onto his `haldimagi-mbp-cdp` runner. The dispatch called canopy with ACE's own PAT and
# no agent on the turn, so `_relationship_without_agent` saw a user who neither paired
# the runner nor owns a session — CALLER. The session then refused, correctly by its
# envelope, to merge on the owner's typed "I approve", twice.

def _dispatched_turn(asker, runner):
    # A repo-targeted turn: no agent, no chat session (the shape the 2026-09-28 envelope
    # showed — `agent: null`, `session_id: null`).
    return Turn.objects.create(origin=Turn.ORIGIN_API, idempotency_key=f"dispatch-{runner.pk}",
                               prompt="validate", claimed_by=runner, project="ace",
                               **who.for_user(asker, via="api", assurance="pat").fields())


def test_an_agents_own_login_on_its_owners_runner_is_the_agent_itself(ctx):
    from apps.harness.models import Runner

    owner, _ws, agent = ctx
    agent.user = User.objects.create_user("ace-bot", "ace@dimagi-ai.com", "pw")
    agent.save(update_fields=["user"])
    box = Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, paired_by=owner)
    env = caller_context.build(_dispatched_turn(agent.user, box))
    assert env["relationship"] == caller_context.SYSTEM


def test_an_agents_login_on_someone_elses_runner_stays_a_caller(ctx):
    # The #983 guard, restated for the no-agent path: an agent's login is the agent
    # only where its OWNER's authority already runs.
    from apps.harness.models import Runner

    _owner, _ws, agent = ctx
    agent.user = User.objects.create_user("ace-bot", "ace@dimagi-ai.com", "pw")
    agent.save(update_fields=["user"])
    other = User.objects.create_user("x", "x@example.org", "pw")
    box = Runner.objects.create(name="x-mbp", kind=Runner.EMDASH, paired_by=other)
    env = caller_context.build(_dispatched_turn(agent.user, box))
    assert env["relationship"] == caller_context.CALLER


def test_a_plain_user_on_the_owners_runner_is_still_a_caller(ctx):
    # Only an agent login is lifted — the owner's box does not vouch for strangers.
    from apps.harness.models import Runner

    owner, _ws, _agent = ctx
    stranger = User.objects.create_user("y", "y@example.org", "pw")
    box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, paired_by=owner)
    env = caller_context.build(_dispatched_turn(stranger, box))
    assert env["relationship"] == caller_context.CALLER
