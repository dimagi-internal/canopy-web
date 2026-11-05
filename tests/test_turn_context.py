"""What the Turn Context promises beyond answering as `decide` did: `tests/test_turn_context_equivalence.py`
holds that. Here: it writes nothing, `None` never means the whole agent, each stage's own shape, and each
send block against the route that refuses it. Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md`.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from canopy_runner import session_naming
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.agents import answering_snapshot, interface, roles, session_context, turn_context
from apps.agents.credentials import for_turn as credentials_for_turn
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.agents.standings import Standings
from apps.canopy_sessions.models import REVIEW_METADATA_KEY, Session, SessionParticipant
from apps.harness import caller_context, sensitivity
from apps.harness import initiator as who
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import declare

pytestmark = pytest.mark.django_db

M = WorkspaceMembership
CAPS = {
    "ask": {"tools": ["Read"], "description": "Ask"},
    "connect": {"tools": ["Read"], "sites": ["connect-labs"]},
    "stock": {"tools": ["Read"], "pages": ["stock://*"]},
    "data": {"tools": ["Read"], "sites": ["data-site"], "data": "sensitive"},
    "bare": {"tools": ["Read"]},
}


@pytest.fixture()
def w():
    make = lambda name: get_user_model().objects.create_user(name, f"{name}@dimagi.com", "pw")  # noqa: E731
    owner, admin, editor, reader, door, stranger, member = map(
        make, ("owner", "admin", "editor", "reader", "door", "stranger", "member"))
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    for user in (owner, admin, editor, reader, door, member):
        M.objects.create(user=user, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="agent", name="Agent", workspace=ws, owner=owner, turn_mode="auto",
                                 interface={"capabilities": CAPS})
    AgentAdmin.objects.create(agent=agent, user=admin, granted_by=owner)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=editor, role=roles.EDITOR)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=door, capability="connect")
    return SimpleNamespace(ws=ws, agent=agent, owner=owner, admin=admin, editor=editor, reader=reader, door=door,
                           stranger=stranger, member=member)


def _chat(w, creator=None, **metadata):
    return Session.objects.create(agent=w.agent, workspace=w.ws, created_by=creator or w.owner, metadata=metadata)


_keys = iter(range(10**6))


def _turn(w, user, session=None, **extra):
    base = {"chat_session": session} if session is not None else {"agent": w.agent}
    return Turn.objects.create(**base, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, initiator_kind=who.USER, initiator_user=user,
                               initiator_assurance=who.SESSION, prompt="hi", idempotency_key=f"k-{next(_keys)}", **extra)


def _every_fact(ctx):
    """Read everything a Turn Context and its Session Context can be asked, so nothing read lazily escapes."""
    for attr in ("standing", "access", "profile", "basis", "session_key_suffix", "review", "stamped", "refusal", "trace",
                 "credentials"):
        getattr(ctx, attr)
    if ctx.profile is not None:
        ctx.profile.as_envelope()
    sc = ctx._session_ctx
    for attr in ("runner_bound", "sensitive_requirement", "audience", "participants", "session_grants", "send_blocks"):
        getattr(sc, attr)


# --- the old names are gone ------------------------------------------------------------------------

def test_nothing_of_the_old_rule_is_left_behind():
    from apps.agents import access

    for name in ("decide", "decide_for_turn", "Decision", "NONE", "_decide", "_stamp", "_from_access_list"):
        assert not hasattr(access, name), name


def test_nothing_compares_a_turn_context_to_the_old_word_for_refused():
    # `.access == "none"` would now be false for everyone, which reads as allowing them.
    from tests._source import ROOT

    wrong = []
    for path in (ROOT / "apps").rglob("*.py"):
        text = path.read_text()
        if "turn_context" in text and re.search(r"""\.access\s*(==|!=)\s*["']none["']""", text):
            wrong.append(str(path))
    assert not wrong


# --- it writes nothing ----------------------------------------------------------------------------

def test_the_resolver_writes_nothing_whoever_is_asking_and_however_it_is_asked(w):
    runner = Runner.objects.create(name="box", kind=Runner.EMDASH, owner=w.owner, workspace=w.ws)
    plain, sensitive = _chat(w), _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    site = _chat(w, embed_app="connect-labs")
    review = _chat(w, **{REVIEW_METADATA_KEY: str(plain.pk)})
    gone = _chat(w)
    gone.content_deleted_at = timezone.now()
    gone.save(update_fields=["content_deleted_at"])
    repo = Session.objects.create(workspace=w.ws, created_by=w.owner, project="repo")
    SessionParticipant.objects.create(session=plain, user=w.member, role=SessionParticipant.EDITOR)
    people = (w.owner, w.admin, w.editor, w.reader, w.door, w.stranger, w.member)
    turns = [_turn(w, user, session) for user in people for session in (plain, sensitive, site, review)]
    turns += [_turn(w, user) for user in people]
    repo_turn = _turn(w, w.owner, repo)
    asked = 0
    with connection.execute_wrapper(answering_snapshot._refuse_writes):
        for turn in turns:
            agent = turn.acting_agent
            for ask in (None, "ask", "connect", "data", "retired"):
                _every_fact(turn_context.for_turn(turn, agent, ask))
                asked += 1
            for stamp in ("", "ask", "connect", "retired"):
                ctx = turn_context.resolve_turn_context(session_context.build(None, turn, agent=agent),
                                                        turn_context.Asker.of_turn(turn), agent, turn=turn, stamp=stamp,
                                                        runner=runner)
                _every_fact(ctx)
                asked += 1
        for user in people:
            _every_fact(turn_context.for_asker(w.agent, user, verified=True))
            for session in (plain, sensitive, site, review, gone):   # a preview, stage 0 included
                _every_fact(turn_context.resolve_turn_context(session_context.build(session),
                                                              turn_context.Asker(who=user, verified=True), w.agent))
                asked += 1
        _every_fact(turn_context.for_turn(repo_turn, None))
    assert asked > 200


# --- `None` is never the whole agent --------------------------------------------------------------

def test_the_only_way_to_the_whole_agent_is_an_explicit_kind(w):
    # Every answer the rule can give, and each holds: full exactly on `whole_agent`, and no profile when refused.
    chat = _chat(w)
    sensitive = _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    seen = set()
    for user in (w.owner, w.editor, w.reader, w.door, w.stranger):
        for session in (chat, sensitive):
            turn = _turn(w, user, session)
            for ask in (None, "ask", "connect", "data", "retired"):
                ctx = turn_context.for_turn(turn, w.agent, ask)
                seen.add((ctx.access, ctx.profile.kind if ctx.profile else None))
                if ctx.access == turn_context.FULL:
                    assert ctx.profile.kind == turn_context.WHOLE_AGENT and ctx.stamped == ""
                elif ctx.access == turn_context.CONFINED:
                    assert ctx.profile.kind != turn_context.WHOLE_AGENT and ctx.stamped and ctx.session_key_suffix
                else:
                    assert ctx.profile is None and ctx.stamped is None and ctx.refusal is not None
    assert {a for a, _k in seen} == {turn_context.FULL, turn_context.CONFINED, turn_context.REFUSED}


@pytest.mark.parametrize("access, kind", [
    (turn_context.FULL, None), (turn_context.FULL, turn_context.READ_MODE), (turn_context.FULL, turn_context.DENY_ALL),
    (turn_context.CONFINED, None), (turn_context.CONFINED, turn_context.WHOLE_AGENT),
    (turn_context.CONFINED, turn_context.NO_AGENT), (turn_context.REFUSED, turn_context.WHOLE_AGENT),
])
def test_a_turn_context_cannot_be_built_saying_otherwise(access, kind):
    profile = turn_context.Profile(kind=kind) if kind else None
    refusal = None if access != turn_context.REFUSED else turn_context.Refusal(code="x", why="y")
    with pytest.raises(ValueError):
        turn_context.TurnContext(standing=turn_context.Standing(relationship="member"), access=access, profile=profile,
                                 basis="b", session_key_suffix="", review=turn_context.Review(), stamped="", refusal=refusal,
                                 trace=())


def test_a_refusal_with_no_reason_or_a_reason_with_no_refusal_cannot_be_built():
    standing = turn_context.Standing(relationship="member")
    with pytest.raises(ValueError):
        turn_context.TurnContext(standing=standing, access=turn_context.REFUSED, profile=None, basis="refused",
                                 session_key_suffix="", review=turn_context.Review(), stamped=None, refusal=None, trace=())
    with pytest.raises(ValueError):
        turn_context.TurnContext(standing=standing, access=turn_context.FULL,
                                 profile=turn_context.Profile(kind=turn_context.WHOLE_AGENT), basis="b",
                                 session_key_suffix="", review=turn_context.Review(), stamped="",
                                 refusal=turn_context.Refusal(code="x", why="y"), trace=())


def _at_claim(w, turn, stamp):
    return turn_context.resolve_turn_context(session_context.build(None, turn, agent=w.agent),
                                             turn_context.Asker.of_turn(turn), w.agent, turn=turn, stamp=stamp)


def test_a_stamp_that_no_longer_resolves_is_deny_all_never_the_whole_agent(w):
    turn = _turn(w, w.owner, _chat(w), capability="retired")
    ctx = _at_claim(w, turn, "retired")
    assert (ctx.access, ctx.profile.kind, ctx.stamped) == (turn_context.CONFINED, turn_context.DENY_ALL, "retired")
    assert ctx.profile.tools == [] and ctx.profile.bash == [] and ctx.profile.write_paths == []
    # The owner holds the whole agent, and it changes nothing: the stamp wins.
    assert ctx.standing.agent_role == roles.OWNER
    envelope = caller_context.build(turn)
    assert envelope["profile"] == "confined" and envelope["capability"]["tools"] == []
    assert ctx.profile.as_envelope() == envelope["capability"]


def test_a_sensitive_turn_stamped_with_a_read_mode_that_has_gone_is_deny_all(w):
    declare(w.agent, None)
    turn = _turn(w, w.reader, _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]}), capability="ask")
    ctx = _at_claim(w, turn, "ask")
    assert ctx.profile.kind == turn_context.DENY_ALL and ctx.profile.tools == []
    assert ctx.profile.as_envelope() == caller_context.build(turn)["capability"]
    # The same stamp in an ordinary conversation is canopy's default read profile.
    plain = _turn(w, w.reader, _chat(w), capability="ask")
    assert _at_claim(w, plain, "ask").profile.kind == turn_context.READ_MODE


def test_a_whole_agent_turn_stays_whole_at_claim_though_its_asker_has_since_lost_it(w):
    turn = _turn(w, w.editor, _chat(w))
    AgentGrant.objects.filter(agent=w.agent, user=w.editor).delete()
    ctx = _at_claim(w, turn, "")
    assert ctx.access == turn_context.FULL and ctx.profile.kind == turn_context.WHOLE_AGENT
    assert ctx.basis == "refused" and ctx.refusal is None      # what the envelope says of them, today


def test_only_an_explicit_kind_has_no_lists_and_an_absent_list_elsewhere_denies(w):
    whole = turn_context.for_asker(w.agent, w.owner, verified=True).profile
    assert whole.kind == turn_context.WHOLE_AGENT and whole.tools is None and whole.as_envelope() is None
    door = turn_context.for_asker(w.agent, w.door, verified=True, requested="connect").profile
    assert door.kind == turn_context.CONNECTED_SITE and door.site == "connect-labs" and door.bash in (None, [])
    assert "mcp__*canopy-web__site_call" in door.tools


# --- each kind and each stage ---------------------------------------------------------------------

def test_each_profile_kind(w):
    review = _chat(w, **{REVIEW_METADATA_KEY: "x"})
    kind = lambda ctx: ctx.profile.kind  # noqa: E731
    assert kind(turn_context.for_asker(w.agent, w.reader, verified=True)) == turn_context.READ_MODE
    assert kind(turn_context.for_asker(w.agent, w.door, verified=True, requested="connect")) == turn_context.CONNECTED_SITE
    assert kind(turn_context.for_turn(_turn(w, w.owner, review), w.agent)) == turn_context.SESSION_REVIEW
    assert kind(turn_context.for_asker(w.agent, w.owner, verified=True)) == turn_context.WHOLE_AGENT
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.PERSON, user=w.member, capability="bare")
    bare = turn_context.for_asker(w.agent, w.member, verified=True, requested="bare").profile
    assert bare.kind == turn_context.CONNECTED_SITE and bare.site is None        # a row with no site keeps the kind
    assert turn_context.PROFILE_KINDS == {"whole_agent", "read_mode", "connected_site", "session_review", "deny_all", "no_agent"}


def test_an_entry_on_two_sites_names_the_one_this_conversation_is_on(w):
    w.agent.interface["capabilities"]["two"] = {"tools": ["Read"], "sites": ["site-a", "site-b"]}
    w.agent.save(update_fields=["interface"])
    turn = _turn(w, w.owner, _chat(w, embed_app="site-b"))
    assert turn_context.for_turn(turn, w.agent, "two").access == turn_context.FULL   # the owner keeps the whole agent
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.PERSON, user=w.member, capability="two")
    on_b = turn_context.for_turn(_turn(w, w.member, _chat(w, embed_app="site-b")), w.agent, "two")
    assert (on_b.profile.kind, on_b.profile.site) == (turn_context.CONNECTED_SITE, "site-b")
    elsewhere = turn_context.for_asker(w.agent, w.member, verified=True, requested="two")
    assert elsewhere.profile.site == "site-a"


def test_a_read_mode_profile_says_which_default_it_ran_under(w):
    declare(w.agent, None)
    profile = turn_context.for_asker(w.agent, w.reader, verified=True).profile
    assert profile.kind == turn_context.READ_MODE and profile.name == "read"
    assert profile.default_profile_version == interface.READ_PROFILE_VERSION
    assert profile.as_envelope() == interface.profile(w.agent, "read")


def test_a_review_chat_refuses_what_it_would_refuse_and_narrows_the_rest(w):
    review = _chat(w, **{REVIEW_METADATA_KEY: "x"})
    assert turn_context.for_turn(_turn(w, w.stranger, review), w.agent).refused
    assert turn_context.for_turn(_turn(w, w.door, review), w.agent).refused    # `read` opens it; a door line does not
    ctx = turn_context.for_turn(_turn(w, w.owner, review), w.agent)
    assert ctx.review.may_request_auto is False and ctx.review.may_pin_runner is False


def test_the_session_grant_is_a_named_rule_in_the_trace(w):
    session = _chat(w)
    SessionParticipant.objects.create(session=session, user=w.stranger, role=SessionParticipant.EDITOR)
    ctx = turn_context.for_turn(_turn(w, w.stranger, session), w.agent)
    assert ctx.basis == "session:editor" and ctx.standing.source == "session"
    assert [g.kind for g in ctx._session_ctx.session_grants] == [session_context.SESSION_WRITER]
    assert turn_context.TraceEntry("session_grants", session_context.SESSION_WRITER,
                                   ctx._session_ctx.session_grants[0].source) in ctx.trace


def test_a_session_someone_else_started_grants_nothing(w):
    session = _chat(w, creator=w.member)
    SessionParticipant.objects.create(session=session, user=w.stranger, role=SessionParticipant.EDITOR)
    assert session_context.build(session).session_grants == ()
    assert turn_context.for_turn(_turn(w, w.stranger, session), w.agent).refused


def test_a_session_grant_is_only_for_the_person_it_names(w):
    session = _chat(w)
    SessionParticipant.objects.create(session=session, user=w.member, role=SessionParticipant.EDITOR)
    assert turn_context.for_turn(_turn(w, w.member, session), w.agent).access == turn_context.FULL
    assert turn_context.for_turn(_turn(w, w.stranger, session), w.agent).refused


def test_a_view_judging_many_pairs_reads_nothing_per_pair(w, django_assert_num_queries):
    # Handed what `Standings` read once, the rule itself asks the database nothing, even for a line
    # that names a workspace, whose answer is the person's role there.
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.WORKSPACE, subject_workspace=w.ws, role=roles.READ_ONLY)
    standings = Standings([w.owner, w.reader, w.stranger], [w.agent])
    asked = [(user, standings.of(user, w.agent), standings.lines_of(w.agent)) for user in (w.owner, w.reader, w.stranger)]
    w.agent.owner  # noqa: B018 — the refusal names them, and that is not the rule's to read
    with django_assert_num_queries(0):
        for user, standing, lines in asked:
            turn_context.for_asker(w.agent, user, verified=True, standing=standing, lines=lines)


def test_a_viewer_row_grants_nothing(w):
    session = _chat(w)
    SessionParticipant.objects.create(session=session, user=w.stranger, role=SessionParticipant.VIEWER)
    assert session_context.build(session).session_grants == ()


def test_the_trace_names_every_rule_it_asked_ending_at_the_deciding_one(w):
    ctx = turn_context.for_turn(_turn(w, w.reader, _chat(w)), w.agent)
    assert [e.rule for e in ctx.trace] == ["trusted_without_a_list", "session_grants", "access_list", "entry", "sensitive"]
    assert ctx.trace[-2].outcome == "ask"
    assert [e.rule for e in turn_context.for_asker(w.agent, w.owner, verified=True).trace] == ["trusted_without_a_list", "sensitive"]
    assert turn_context.for_asker(w.agent, w.owner, verified=True).trace[0].outcome == "owner"


def test_the_review_rule_runs_before_the_sensitive_rule_in_that_order_in_the_trace(w):
    ctx = turn_context.for_turn(_turn(w, w.owner, _chat(w, **{REVIEW_METADATA_KEY: "x"})), w.agent)
    assert [e.rule for e in ctx.trace] == ["trusted_without_a_list", "review_chat", "sensitive"]


def test_every_refusal_carries_a_code_from_the_closed_set_and_who_can_fix_it(w):
    cases = [turn_context.for_asker(w.agent, w.stranger, verified=True),
             turn_context.for_asker(w.agent, w.reader, verified=True, requested="bare"),
             turn_context.for_turn(_turn(w, w.reader, _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})), w.agent)]
    assert [c.refusal.code for c in cases] == [turn_context.NOT_ON_ACCESS_LIST, turn_context.PROFILE_NOT_OPEN,
                                               turn_context.SENSITIVE_NOT_OPEN]
    for ctx in cases:
        assert ctx.refusal.code in turn_context.REFUSAL_CODES and ctx.refusal.why
        assert w.owner.email in ctx.refusal.who_can_fix


def test_the_suffix_is_what_the_runner_keys_a_session_with(w):
    turn = _turn(w, w.reader, _chat(w))
    ctx = turn_context.for_turn(turn, w.agent)
    turn.capability = ctx.stamped
    env = caller_context.build(turn)
    assert session_naming.restricted_key({"caller_context": env}, "thread") == f"thread{ctx.session_key_suffix}"
    whole = _turn(w, w.owner, _chat(w))
    assert turn_context.for_turn(whole, w.agent).session_key_suffix == ""
    assert session_naming.restricted_key({"caller_context": caller_context.build(whole)}, "thread") == "thread"


def test_turn_mode_is_pending_until_the_claim_then_what_the_rules_say(w):
    turn = _turn(w, w.owner, _chat(w))
    assert turn_context.for_turn(turn, w.agent).review.mode == turn_context.PENDING
    runner = Runner.objects.create(name="box", kind=Runner.EMDASH, owner=w.owner, workspace=w.ws)
    ctx = turn_context.resolve_turn_context(session_context.build(None, turn, agent=w.agent),
                                            turn_context.Asker.of_turn(turn), w.agent, turn=turn, runner=runner)
    assert ctx.review.mode == "auto" and ctx.review.basis
    turn.turn_mode, turn.turn_mode_basis = "manual", "stamped at claim"
    assert turn_context.for_turn(turn, w.agent).review.mode == "manual"
    assert turn_context.for_turn(turn, w.agent).review.basis == "stamped at claim"


# --- the Session Context --------------------------------------------------------------------------

def test_the_session_context_holds_the_facts_and_nothing_about_the_asker(w):
    session = _chat(w, embed_app="connect-labs", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    session.page_state = {"resource": "stock://item"}
    session.save(update_fields=["page_state"])
    SessionParticipant.objects.create(session=session, user=w.reader, role=SessionParticipant.VIEWER)
    turn = _turn(w, w.owner, session)
    sc = session_context.build(session)
    assert (sc.session_id, sc.agent, sc.workspace, sc.site, sc.page) == (session.pk, "agent", "connect", "connect-labs", "stock://item")
    assert sc.channel == turn.origin and sc.sensitive and sc.sensitive_requirement == rr.SENSITIVE_DATA
    assert (w.reader.pk, SessionParticipant.VIEWER) in sc.participants
    assert sc.audience["creator"] == {w.owner.pk}
    assert not sc.is_review and sc.review_ask is None


def test_a_turn_with_no_session_has_its_own_channel_and_sensitivity(w):
    parent = _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    turn = Turn.objects.create(agent=w.agent, parent_session=parent, origin=Turn.ORIGIN_EMAIL, initiator_kind=who.SYSTEM,
                               prompt="hi", idempotency_key="sessionless")
    sc = session_context.build(None, turn)
    assert sc.session_id is None and sc.channel == Turn.ORIGIN_EMAIL and sc.agent == "agent" and sc.sensitive


def test_a_review_chat_says_which_session_it_is_about(w):
    sc = session_context.build(_chat(w, **{REVIEW_METADATA_KEY: "the-session"}))
    assert sc.is_review and sc.review_ask == "the-session"


def test_an_empty_session_context_is_one_shape_with_nothing_in_it(w):
    sc = session_context.empty(channel="email")
    assert sc.session_id is None and sc.channel == "email" and not sc.sensitive and not sc.is_review
    assert sc.session_grants == () and sc.send_blocks == () and sc.participants == () and sc.audience == {}


def test_sensitive_never_reverts_with_the_turn(w):
    session = _chat(w, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    assert session_context.build(None, _turn(w, w.owner, session)).sensitive
    assert sensitivity.is_sensitive(session)


# --- stage 0, each block pinned to the route that refuses it ---------------------------------------

def _send(user, session):
    c = Client()
    c.force_login(user)
    return c.post(f"/api/canopy-sessions/{session.pk}/send", data={"text": "hello"}, content_type="application/json")


def _preview(w, user, session):
    return turn_context.resolve_turn_context(session_context.build(session),
                                             turn_context.Asker(who=user, verified=True), w.agent)


def test_deleted_content_refuses_as_the_send_route_does(w):
    session = _chat(w)
    session.content_deleted_at = timezone.now()
    session.save(update_fields=["content_deleted_at"])
    ctx = _preview(w, w.owner, session)
    assert ctx.refused and ctx.refusal.code == session_context.CONTENT_DELETED
    response = _send(w.owner, session)
    assert response.status_code == 409 and response.json()["detail"] == ctx.refusal.why
    # Who they are is still true: only this message cannot be taken.
    assert ctx.standing.agent_role == roles.OWNER and ctx.profile is None


def test_reading_without_writing_refuses_as_the_send_route_does(w):
    session = _chat(w)
    SessionParticipant.objects.create(session=session, user=w.reader, role=SessionParticipant.VIEWER)
    ctx = _preview(w, w.reader, session)
    assert ctx.refused and ctx.refusal.code == session_context.CANNOT_WRITE
    response = _send(w.reader, session)
    assert response.status_code == 403 and response.json()["detail"] == ctx.refusal.why


def test_a_cli_the_agent_no_longer_runs_refuses_as_the_send_route_does(w):
    session = _chat(w)
    Session.objects.filter(pk=session.pk).update(cli="codex")
    session.refresh_from_db()
    ctx = _preview(w, w.owner, session)
    assert ctx.refused and ctx.refusal.code == session_context.CLI_NOT_OFFERED
    response = _send(w.owner, session)
    assert response.status_code == 422 and response.json()["detail"] == ctx.refusal.why


def test_a_sensitive_conversation_refuses_a_door_that_is_not_its_chat_surface_as_the_send_route_does(w):
    session = _chat(w, embed_app="data-site", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    assert not _preview(w, w.owner, session).refused      # its own person, at its own chat surface
    ctx = turn_context.resolve_turn_context(session_context.build(session),
                                            turn_context.Asker(who=w.owner, verified=True, chat_surface=False), w.agent)
    assert ctx.refused and ctx.refusal.code == session_context.NOT_IN_SENSITIVE
    raw, _token = PersonalToken.create_for_user(user=w.owner, label="t")
    response = Client().post(f"/api/canopy-sessions/{session.pk}/send", data={"text": "hello"},
                             content_type="application/json", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403 and response.json()["detail"] == ctx.refusal.why


def test_a_real_turn_is_past_its_door_so_stage_0_never_refuses_it(w):
    # An email or a schedule reaches a session these routes would refuse. They run today, and still do.
    session = _chat(w)
    turn = _turn(w, w.owner, session)
    session.content_deleted_at = timezone.now()
    session.save(update_fields=["content_deleted_at"])
    turn.refresh_from_db()
    assert turn_context.for_turn(turn, w.agent).access == turn_context.FULL
    assert session_context.build(session).send_blocks[0].code == session_context.CONTENT_DELETED


def test_a_question_with_no_session_skips_stage_0(w):
    ctx = turn_context.for_asker(w.agent, w.owner, verified=True)
    assert "send_blocks" not in [e.rule for e in ctx.trace]


def test_a_preview_asks_the_rule_of_the_session_it_is_about(w):
    site = _chat(w, embed_app="connect-labs")
    SessionParticipant.objects.create(session=site, user=w.reader, role=SessionParticipant.EDITOR)
    assert _preview(w, w.reader, site).basis == "session:editor"
    other = _chat(w, creator=w.member, embed_app="connect-labs")
    SessionParticipant.objects.create(session=other, user=w.door, role=SessionParticipant.EDITOR)
    ctx = _preview(w, w.door, other)
    assert ctx.stamped == "connect" and ctx.profile.site == "connect-labs"


def test_a_contacts_preview_is_not_held_to_a_signed_in_persons_write_rule(w):
    from apps.contacts.models import Contact

    session = _chat(w)
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.CONTACT, role=roles.READ_ONLY, verified=False)
    ctx = turn_context.resolve_turn_context(session_context.build(session), turn_context.Asker(who=Contact(email="a@b.org")),
                                            w.agent)
    assert ctx.access == turn_context.CONFINED


# --- what a turn gets: the statement asks the functions that decide it ------------------------------

def _lines(ctx) -> dict:
    return {line.name: line for line in ctx.credentials}


def test_only_the_whole_agent_gets_the_login_and_the_github_identity(w):
    whole = _lines(turn_context.for_turn(_turn(w, w.owner, _chat(w)), w.agent))
    assert whole["canopy_login"].gets == credentials_for_turn.NO     # this agent has no login of its own
    assert "no canopy login" in whole["canopy_login"].why
    assert whole["github_identity"].gets == credentials_for_turn.NO and "delegated" in whole["github_identity"].why
    assert whole["caller_token"].gets == credentials_for_turn.NO     # the owner's turn acts as the agent
    assert whole["agent_vault"].gets == credentials_for_turn.PENDING

    w.agent.user = w.member
    w.agent.save(update_fields=["user"])
    assert _lines(turn_context.for_turn(_turn(w, w.owner, _chat(w)), w.agent))["canopy_login"].gets == credentials_for_turn.PENDING

    confined = _lines(turn_context.for_turn(_turn(w, w.reader, _chat(w)), w.agent))
    assert confined["canopy_login"].gets == credentials_for_turn.NO and "confined" in confined["canopy_login"].why
    assert confined["github_identity"].gets == credentials_for_turn.NO and "confined" in confined["github_identity"].why
    assert confined["agent_vault"].gets == credentials_for_turn.NO
    assert confined["caller_token"].gets == credentials_for_turn.YES


def test_an_editors_whole_agent_turn_reads_as_its_asker(w):
    ctx = turn_context.for_turn(_turn(w, w.editor, _chat(w)), w.agent)
    assert ctx.access == turn_context.FULL
    assert _lines(ctx)["caller_token"].gets == credentials_for_turn.YES and "asker" in _lines(ctx)["caller_token"].why


def test_a_review_gets_no_github_identity(w):
    from apps.harness import turn_asker

    turn = _turn(w, w.owner, _chat(w), origin_ref={turn_asker.READS_FOR: w.reader.pk})
    assert "review" in _lines(turn_context.for_turn(turn, w.agent))["github_identity"].why


def test_the_agents_own_login_at_a_site_is_withheld_on_its_page(w):
    caps = {**CAPS, "connect": {**CAPS["connect"], "own_login": {"connect-labs": ["LABS_TOKEN"]}}}
    w.agent.interface = {"capabilities": caps}
    w.agent.save(update_fields=["interface"])
    ctx = turn_context.for_turn(_turn(w, w.owner, _chat(w, embed_app="connect-labs")), w.agent)
    assert _lines(ctx)["LABS_TOKEN"].gets == credentials_for_turn.NO


def test_a_refused_turn_gets_nothing_and_no_line_holds_a_value(w):
    ctx = turn_context.for_turn(_turn(w, w.stranger, _chat(w)), w.agent)
    assert {line.gets for line in ctx.credentials} == {credentials_for_turn.NO}
    assert {"name", "kind", "gets", "why"} == {f.name for f in credentials_for_turn.CredentialLine.__dataclass_fields__.values()}


def test_a_preview_promises_nothing_it_cannot_know(w):
    whole = _lines(turn_context.resolve_turn_context(session_context.build(_chat(w)), turn_context.Asker(who=w.owner, verified=True), w.agent))
    assert whole["canopy_login"].gets == credentials_for_turn.PENDING
    confined_chat = _chat(w)
    SessionParticipant.objects.create(session=confined_chat, user=w.reader, role=SessionParticipant.VIEWER)
    assert _lines(turn_context.for_asker(w.agent, w.reader, verified=True))["canopy_login"].gets == credentials_for_turn.NO


def test_the_statement_does_not_stamp_the_turn_it_asks_about(w):
    turn = _turn(w, w.reader, _chat(w))
    turn_context.for_turn(turn, w.agent).credentials
    assert turn.capability == ""


def test_a_turn_with_no_agent_gets_what_a_project_turn_gets(w):
    repo = Session.objects.create(workspace=w.ws, created_by=w.owner, project="repo")
    ctx = turn_context.for_turn(_turn(w, w.owner, repo), None)
    lines = _lines(ctx)
    assert lines["canopy_login"].gets == credentials_for_turn.NO and "no agent" in lines["canopy_login"].why
    assert lines["caller_token"].gets == credentials_for_turn.YES
