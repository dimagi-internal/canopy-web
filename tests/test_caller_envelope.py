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
    assert env["relationship"] == caller_context.CONTACT
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
    for user, want in ((owner, "owner"), (mem, "member"), (stranger, "contact")):
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
    laptop = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=owner)
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
    box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=owner)
    env = caller_context.build(_repo_turn(ws, stranger, creator=owner, runner=box))
    assert env["relationship"] == caller_context.CONTACT


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
    box = Runner.objects.create(name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=owner)
    env = caller_context.build(_dispatched_turn(agent.user, box))
    assert env["relationship"] == caller_context.SYSTEM


def test_an_agents_login_on_someone_elses_runner_stays_a_contact(ctx):
    # The #983 guard, restated for the no-agent path: an agent's login is the agent
    # only where its OWNER's authority already runs.
    from apps.harness.models import Runner

    _owner, _ws, agent = ctx
    agent.user = User.objects.create_user("ace-bot", "ace@dimagi-ai.com", "pw")
    agent.save(update_fields=["user"])
    other = User.objects.create_user("x", "x@example.org", "pw")
    box = Runner.objects.create(name="x-mbp", kind=Runner.EMDASH, owner=other)
    env = caller_context.build(_dispatched_turn(agent.user, box))
    assert env["relationship"] == caller_context.CONTACT


def test_a_plain_user_on_the_owners_runner_is_still_a_contact(ctx):
    # Only an agent login is lifted — the owner's box does not vouch for strangers.
    from apps.harness.models import Runner

    owner, _ws, _agent = ctx
    stranger = User.objects.create_user("y", "y@example.org", "pw")
    box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=owner)
    env = caller_context.build(_dispatched_turn(stranger, box))
    assert env["relationship"] == caller_context.CONTACT


# --- the repo-internal ship grant (owner decision, 2026-10-03) -----------------------
#
# Ada's fix dispatches to sibling agents (eva#343, eva#347, canopy#715) each stopped for
# the owner to type "yes merge" though the brief said to merge: the envelope said
# relationship=admin, turn_mode=manual, and manual means the owner approves a merge.
# The grant lifts exactly that — push / PR / merge in the TARGET agent's own repo — when
# another agent's login that holds the target's keys dispatched the turn. Nothing else.

from apps.agents.models import AgentAdmin  # noqa: E402


@pytest.fixture()
def fleet(ctx):
    """`ace` (the target, with a repo) and `ada` (a sibling agent whose login dispatches)."""
    owner, ws, ace = ctx
    ace.repo_url = "https://github.com/dimagi-internal/ace"
    ace.save(update_fields=["repo_url"])
    ada_login = User.objects.create_user("ada-bot", "ada@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=ada_login, workspace=ws, role=WorkspaceMembership.EDITOR)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner, user=ada_login)
    return owner, ws, ace, ada


def _dispatch(agent, user, *, key="d-1", assurance=who.PAT):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt="fix it and merge",
        initiator=who.for_user(user, via="api", assurance=assurance))
    return turn


def test_an_admin_agent_dispatch_carries_a_ship_grant_for_the_targets_own_repo(fleet):
    _owner, _ws, ace, ada = fleet
    AgentAdmin.objects.create(agent=ace, user=ada.user)
    env = caller_context.build(_dispatch(ace, ada.user))
    assert env["relationship"] == caller_context.ADMIN
    grant = env["ship_grant"]
    assert grant["repo"] == "dimagi-internal/ace"
    assert grant["actions"] == ["push", "pull_request", "merge"]
    assert grant["dispatched_by"] == {"email": "ada@dimagi-ai.com", "agent": "ada"}
    assert grant["basis"] == "dispatched by ada@dimagi-ai.com (agent ada), admin of ace"
    assert "send email or messages" in grant["not_granted"]
    # The grant lifts the merge wait; it does not flip the turn to auto.
    assert env["turn_mode"]["mode"] == "manual"


def test_an_agent_login_that_owns_the_target_gets_the_grant_too(fleet):
    _owner, _ws, ace, ada = fleet
    ace.owner = ada.user
    ace.save(update_fields=["owner"])
    env = caller_context.build(_dispatch(ace, ada.user))
    assert env["relationship"] == caller_context.OWNER
    assert env["ship_grant"]["basis"].endswith("owner of ace")


def test_a_member_agent_dispatch_gets_no_grant(fleet):
    _owner, _ws, ace, ada = fleet          # ada is an editor of the workspace, not an admin
    env = caller_context.build(_dispatch(ace, ada.user))
    assert env["relationship"] == caller_context.MEMBER
    assert env["ship_grant"] is None


def test_a_human_gets_no_grant_whatever_their_role(fleet):
    owner, ws, ace, _ada = fleet
    human_admin = User.objects.create_user("ha", "ha@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=human_admin, workspace=ws, role=WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=ace, user=human_admin)
    member = User.objects.create_user("hm", "hm@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=ws, role=WorkspaceMembership.EDITOR)
    for i, user in enumerate((owner, human_admin, member)):
        env = caller_context.build(_dispatch(ace, user, key=f"h-{i}"))
        assert env["ship_grant"] is None, user.username


def test_an_unverified_admin_agent_login_gets_no_grant(fleet):
    _owner, _ws, ace, ada = fleet
    AgentAdmin.objects.create(agent=ace, user=ada.user)
    env = caller_context.build(_dispatch(ace, ada.user, assurance=who.HOST_SIGNED))
    assert env["verified"] is False
    assert env["ship_grant"] is None


def test_a_repo_turn_gets_no_grant(fleet):
    from apps.harness.models import Runner

    owner, _ws, ace, ada = fleet
    AgentAdmin.objects.create(agent=ace, user=ada.user)
    box = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=owner)
    env = caller_context.build(_dispatched_turn(ada.user, box))
    assert env["agent"] is None
    assert env["ship_grant"] is None


def test_an_item_dispatch_by_agent_slug_gets_no_grant(fleet):
    # `kind=agent` names a slug, not a login canopy authenticated: it is `system`, and
    # no grant rides on it.
    _owner, _ws, ace, _ada = fleet
    turn, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="item-1",
                                    initiator=who.for_agent("ada", via="item:1"))
    env = caller_context.build(turn)
    assert env["relationship"] == caller_context.SYSTEM
    assert env["ship_grant"] is None


def test_an_agent_dispatching_itself_gets_no_grant(fleet):
    _owner, _ws, ace, _ada = fleet
    ace.user = User.objects.create_user("ace-bot", "ace@dimagi-ai.com", "pw")
    ace.save(update_fields=["user"])
    env = caller_context.build(_dispatch(ace, ace.user))
    assert env["relationship"] == caller_context.SYSTEM
    assert env["ship_grant"] is None


def test_a_target_with_no_repo_gets_no_grant(fleet):
    _owner, _ws, ace, ada = fleet
    AgentAdmin.objects.create(agent=ace, user=ada.user)
    ace.repo_url = ""
    ace.save(update_fields=["repo_url"])
    assert caller_context.build(_dispatch(ace, ada.user))["ship_grant"] is None


def test_an_ordinary_email_turn_has_no_grant(ctx):
    _o, _ws, agent = ctx
    assert caller_context.build(_email(agent, headers=HDRS))["ship_grant"] is None


# --- envelope VERSION 2: one word per meaning (2026-10-04) ----------------------------

def test_version_2_renames_and_readers_accept_both():
    # `caller` -> `contact`, `restricted` -> `confined`; an envelope from an older
    # canopy-web may still sit on a box, so the normalizers read either.
    # 3 (fleet brain) only ADDED `person` and `trigger.kind`; the v2 renames stand.
    assert caller_context.VERSION == 3
    assert caller_context.normalize_relationship("caller") == "contact"
    assert caller_context.normalize_relationship("contact") == "contact"
    assert caller_context.normalize_relationship("owner") == "owner"
    assert caller_context.normalize_profile("restricted") == "confined"
    assert caller_context.normalize_profile("confined") == "confined"
    assert caller_context.normalize_profile("full") == "full"


# --- unproven_member: a member's mail that could not be tied to them (#1265) -------

DKIM_ONLY = [{"name": "Authentication-Results",
              "value": ("mx.google.com; dkim=pass header.i=@mail-provider.example; "
                        "spf=pass smtp.mailfrom=llo-foo.org")}]


@pytest.fixture()
def editor(ctx):
    from allauth.account.models import EmailAddress

    _o, ws, _agent = ctx
    u = User.objects.create_user("fatima", "fatima@llo-foo.org", "pw")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return u


def test_a_member_whose_mail_is_only_dkim_is_named_but_stays_a_contact(ctx, editor):
    _o, _ws, agent = ctx
    turn = _email(agent, headers=DKIM_ONLY)
    assert turn.initiator_assurance == Contact.AUTH_DKIM
    env = caller_context.build(turn)
    assert env["relationship"] == caller_context.CONTACT        # nothing granted
    assert env["verified"] is False
    um = env["unproven_member"]
    assert um["email"] == "fatima@llo-foo.org"
    assert um["role"] == WorkspaceMembership.EDITOR
    assert um["this_message_grade"] == Contact.AUTH_DKIM
    assert um["needs"] == [Contact.AUTH_DMARC, Contact.AUTH_DKIM_ALIGNED]
    assert "DKIM" in um["note"] and "not their access" in um["note"]


def test_a_member_whose_mail_is_aligned_resolves_as_a_member_with_no_note(ctx, editor):
    _o, _ws, agent = ctx
    env = caller_context.build(_email(agent, headers=HDRS))
    assert env["relationship"] == caller_context.MEMBER
    assert env["unproven_member"] is None


def test_a_non_member_with_dkim_mail_gets_no_note(ctx):
    from allauth.account.models import EmailAddress

    _o, _ws, agent = ctx
    u = User.objects.create_user("fatima", "fatima@llo-foo.org", "pw")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    env = caller_context.build(_email(agent, headers=DKIM_ONLY))
    assert env["relationship"] == caller_context.CONTACT
    assert env["unproven_member"] is None


def test_an_unknown_address_gets_no_note(ctx):
    _o, _ws, agent = ctx
    assert caller_context.build(_email(agent, headers=DKIM_ONLY))["unproven_member"] is None


def test_a_non_email_turn_gets_no_note(ctx, editor):
    _o, _ws, agent = ctx
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="api-1",
                                 initiator=who.for_user(editor, via="chat", assurance=who.SESSION))
    assert caller_context.build(t)["unproven_member"] is None


def test_a_blocked_member_address_gets_no_note(ctx, editor):
    _o, ws, agent = ctx
    c = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    from django.utils import timezone

    Contact.objects.filter(pk=c.pk).update(blocked_at=timezone.now(), blocked_reason="spam")
    turn = _email(agent, headers=DKIM_ONLY)
    assert turn.status == Turn.CANCELLED
    assert caller_context.build(turn)["unproven_member"] is None


# --- ...and logged on the turn's event ledger, where an owner reads its history ----

def _unproven_events(turn):
    return list(turn.events.filter(kind=caller_context.UNPROVEN_MEMBER_EVENT))


def test_an_unproven_member_is_logged_once_on_the_turn(ctx, editor):
    _o, _ws, agent = ctx
    turn = _email(agent, headers=DKIM_ONLY)
    [event] = _unproven_events(turn)
    assert event.payload == caller_context.build(turn)["unproven_member"]
    assert event.payload["role"] == WorkspaceMembership.EDITOR
    # The runner re-posting the same unread message is a replay: no second line.
    again = _email(agent, headers=DKIM_ONLY)
    assert again.pk == turn.pk and len(_unproven_events(turn)) == 1


def test_no_log_line_when_there_is_nothing_to_say(ctx, editor):
    _o, _ws, agent = ctx
    aligned = _email(agent, key="aligned", headers=HDRS)          # resolved as the member
    stranger = _email(agent, key="stranger", headers=DKIM_ONLY, **{"from": "x@else.example"})
    for t in (aligned, stranger):
        assert _unproven_events(t) == []


def test_a_runner_cannot_post_the_log_line_itself():
    from apps.harness.api import ALLOWED_EVENT_KINDS

    assert caller_context.UNPROVEN_MEMBER_EVENT not in ALLOWED_EVENT_KINDS


def test_a_failed_log_line_never_fails_the_enqueue(ctx, editor, monkeypatch):
    _o, _ws, agent = ctx

    def boom(turn):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(caller_context, "unproven_member", boom)
    turn = _email(agent, headers=DKIM_ONLY)
    assert turn.pk and _unproven_events(turn) == []


# --- the STANDING ship grant (owner decision, 2026-10-08) ----------------------------
#
# A scheduled Eva turn found and tested a one-line fix, then held the push because a
# `manual` turn files push / merge beside send / publish. The owner lists the repos the
# agent may ship in (`Agent.ship_repos`); its OWN turns then carry a grant for exactly
# those, and nothing else changes: the turn is still manual for mail and publishing.

def _schedule(agent, owner, key="s-1"):
    turn, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                    idempotency_key=key,
                                    initiator=who.system(via="schedule:3", accountable=owner))
    return turn


def test_a_scheduled_turn_carries_the_owners_standing_grant(ctx):
    owner, _ws, agent = ctx
    agent.ship_repos = ["dimagi-internal/ace", "dimagi-internal/chrome-sales"]
    agent.save(update_fields=["ship_repos"])
    env = caller_context.build(_schedule(agent, owner))
    assert env["relationship"] == caller_context.SYSTEM
    grant = env["ship_grant"]
    assert grant["repos"] == ["dimagi-internal/ace", "dimagi-internal/chrome-sales"]
    assert grant["repo"] == "dimagi-internal/ace"     # older hooks read only this key
    assert grant["actions"] == ["push", "pull_request", "merge"]
    assert grant["basis"] == "standing grant set on ace (owner jj@dimagi.com)"
    assert "send email or messages" in grant["not_granted"]
    # It lifts the ship wait only; the turn stays manual for everything else.
    assert env["turn_mode"]["mode"] == "manual"


def test_no_listed_repos_means_no_standing_grant(ctx):
    owner, _ws, agent = ctx
    assert caller_context.build(_schedule(agent, owner))["ship_grant"] is None


def test_the_owner_and_an_admin_get_the_standing_grant(fleet):
    owner, ws, ace, ada = fleet
    ace.ship_repos = ["dimagi-internal/ace"]
    ace.save(update_fields=["ship_repos"])
    human_admin = User.objects.create_user("ha2", "ha2@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=human_admin, workspace=ws, role=WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=ace, user=human_admin)
    for i, user in enumerate((owner, human_admin)):
        env = caller_context.build(_dispatch(ace, user, key=f"st-{i}"))
        assert env["ship_grant"]["repos"] == ["dimagi-internal/ace"], user.username


def test_a_member_or_a_contact_never_gets_the_standing_grant(fleet):
    _owner, _ws, ace, ada = fleet          # ada's login is a workspace editor = member
    ace.ship_repos = ["dimagi-internal/ace"]
    ace.save(update_fields=["ship_repos"])
    env = caller_context.build(_dispatch(ace, ada.user, key="mem"))
    assert env["relationship"] == caller_context.MEMBER
    assert env["ship_grant"] is None
    assert caller_context.build(_email(ace, key="e-st", headers=HDRS))["ship_grant"] is None


def test_an_unverified_owner_gets_no_standing_grant(fleet):
    owner, _ws, ace, _ada = fleet
    ace.ship_repos = ["dimagi-internal/ace"]
    ace.save(update_fields=["ship_repos"])
    env = caller_context.build(_dispatch(ace, owner, key="uv", assurance=who.HOST_SIGNED))
    assert env["verified"] is False
    assert env["ship_grant"] is None


def test_an_item_dispatch_by_slug_gets_no_standing_grant(fleet):
    _owner, _ws, ace, _ada = fleet
    ace.ship_repos = ["dimagi-internal/ace"]
    ace.save(update_fields=["ship_repos"])
    turn, _ = services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="item-st",
                                    initiator=who.for_agent("ada", via="item:1"))
    assert caller_context.build(turn)["ship_grant"] is None


def test_a_dispatch_grant_and_a_standing_grant_merge(fleet):
    _owner, _ws, ace, ada = fleet
    AgentAdmin.objects.create(agent=ace, user=ada.user)
    ace.ship_repos = ["dimagi-internal/canopy", "dimagi-internal/ace"]
    ace.save(update_fields=["ship_repos"])
    grant = caller_context.build(_dispatch(ace, ada.user, key="mg"))["ship_grant"]
    assert grant["repo"] == "dimagi-internal/ace"
    assert grant["repos"] == ["dimagi-internal/ace", "dimagi-internal/canopy"]
    assert grant["dispatched_by"]["agent"] == "ada"
    assert "standing grant set on ace" in grant["basis"]
