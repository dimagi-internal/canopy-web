"""Stage 4 of the Turn Context: every refusal says why and how to fix it, from one closed set of codes.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 7. One test per code, each reached the way a real
person reaches it (the rule, a route, the mail channel), then the set itself: closed, every code worded, no banned word.
"""
from __future__ import annotations

import re

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents import refusals, roles, session_context, turn_context
from apps.agents.models import AgentGrant
from apps.canopy_sessions import access as session_access
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.contacts.models import Contact
from apps.harness import initiator as who
from apps.harness import runner_requirements as rr
from apps.harness import services
from apps.harness.channels import email as email_channel
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken

from .fixtures.turn_context import _chat, make_world

pytestmark = pytest.mark.django_db
Code = refusals.Code
_keys = iter(range(10**6))


@pytest.fixture()
def w():
    return make_world()


def _preview(w, user, session, **asker):
    return turn_context.resolve_turn_context(session_context.build(session),
                                             turn_context.Asker(who=user, verified=True, **asker), w.agent)


def _send(user, session, **headers):
    c = Client()
    if not headers:
        c.force_login(user)
    return c.post(f"/api/canopy-sessions/{session.pk}/send", data={"text": "hello"}, content_type="application/json",
                  headers=headers)


def _chat_turn(user, session):
    turn, _ = services.enqueue_turn(session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hello",
                                    idempotency_key=f"r-{next(_keys)}",
                                    initiator=who.for_user(user, via=Turn.ORIGIN_CANOPY_WEB_CHAT, assurance=who.SESSION))
    return turn


def _turn(user, session):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, initiator_kind=who.USER,
                               initiator_user=user, initiator_assurance=who.SESSION, prompt="hi",
                               idempotency_key=f"t-{next(_keys)}")


def _assert_whole(refusal, code):
    """What every refusal holds: its code, plain why, a concrete fix and who can make it."""
    assert refusal.code == code
    assert refusal.why and refusal.why[0].islower() and not refusal.why.endswith(".")
    assert refusal.how_to_fix and refusal.how_to_fix[0].isupper() and refusal.how_to_fix.endswith(".")
    assert refusal.who_can_fix


# --- one per code ----------------------------------------------------------------------------------

def test_not_on_access_list_names_the_list_the_fix_and_who_makes_it(w):
    ctx = turn_context.for_asker(w.agent, w.member, verified=True)
    _assert_whole(ctx.refusal, Code.NOT_ON_ACCESS_LIST)
    assert ctx.refusal.why == "you are not on agent's access list"
    assert "add you to its access list as read only" in ctx.refusal.how_to_fix
    assert ctx.refusal.who_can_fix == "owner@dimagi.com or an admin of agent"
    assert "inbound-mail trigger" not in ctx.refusal.how_to_fix
    # A turn refused on arrival carries the same words as its note.
    turn = _chat_turn(w.member, _chat(w, creator=w.member))
    assert turn.status == Turn.CANCELLED
    assert turn.result_note == f"not run: {ctx.refusal.why}. {ctx.refusal.how_to_fix}"


def test_not_on_access_list_points_a_proven_automated_sender_at_a_trigger(w):
    alarms = Contact.objects.create(email="alarms@monitor.example", workspace=w.ws)
    ctx = turn_context.for_asker(w.agent, alarms, verified=True)
    _assert_whole(ctx.refusal, Code.NOT_ON_ACCESS_LIST)
    assert "inbound-mail trigger on the agent's Triggers tab" in ctx.refusal.how_to_fix
    assert "inbound-mail trigger" not in turn_context.for_asker(w.agent, alarms, verified=False).refusal.how_to_fix


def test_sender_not_proven_is_a_line_that_names_them_only_for_proven_mail(w):
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.DOMAIN, domain="partner.org", role=roles.READ_ONLY,
                              verified=True)
    contact = Contact.objects.create(email="ana@partner.org", workspace=w.ws)
    unproven = turn_context.for_asker(w.agent, contact, verified=False)
    _assert_whole(unproven.refusal, Code.SENDER_NOT_PROVEN)
    assert "did not prove it" in unproven.refusal.why and "DMARC" in unproven.refusal.how_to_fix
    # Nobody's access changes: proven, the same line admits them; another domain is simply not listed.
    assert turn_context.for_asker(w.agent, contact, verified=True).access == turn_context.CONFINED
    other = Contact.objects.create(email="bo@elsewhere.org", workspace=w.ws)
    assert turn_context.for_asker(w.agent, other, verified=False).refusal.code == Code.NOT_ON_ACCESS_LIST
    # The mail channel says it on the turn.
    turn, _ = services.enqueue_turn(agent=w.agent, origin=Turn.ORIGIN_EMAIL, prompt="hello", idempotency_key="unproven",
                                    initiator=who.for_contact(contact, via="email"))
    assert turn.status == Turn.CANCELLED and turn.result_note == unproven.refusal.result_note


def test_profile_not_open_says_which_entry_and_what_opens_it(w):
    named = turn_context.for_asker(w.agent, w.reader, verified=True, requested="bare")
    _assert_whole(named.refusal, Code.PROFILE_NOT_OPEN)
    assert named.refusal.why == "your place on agent's access list does not open the host site 'bare'"
    assert "a line on agent's access list that opens 'bare'" in named.refusal.how_to_fix
    unknown = turn_context.for_asker(w.agent, w.reader, verified=True, requested="nope")
    _assert_whole(unknown.refusal, Code.PROFILE_NOT_OPEN)
    assert unknown.refusal.why == "agent has no host site called 'nope' for your place on its access list to open"
    assert unknown.refusal.how_to_fix.startswith("Send without naming 'nope'")
    # Holding only one host site, and naming nothing on no page: read mode is not theirs.
    bare = turn_context.for_asker(w.agent, w.site_user, verified=True)
    _assert_whole(bare.refusal, Code.PROFILE_NOT_OPEN)
    assert "opens neither read mode" in bare.refusal.why and "read only role" in bare.refusal.how_to_fix
    # Read mode named by either of its names is "read mode", never the stored name.
    read = turn_context.for_asker(w.agent, w.site_user, verified=True, requested="ask")
    assert read.refusal.why == "your place on agent's access list does not open read mode"


def test_sensitive_not_open_says_which_entry_handles_the_data(w):
    session = _chat(w, embed_app="data-site", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    ctx = turn_context.for_turn(_turn(w.reader, session), w.agent)
    _assert_whole(ctx.refusal, Code.SENSITIVE_NOT_OPEN)
    assert "only the host site 'data' handles" in ctx.refusal.why and "'data' does not list you" in ctx.refusal.why
    assert "opens 'data'" in ctx.refusal.how_to_fix
    w.agent.interface = {"capabilities": {k: v for k, v in w.agent.interface["capabilities"].items() if k != "data"}}
    w.agent.save(update_fields=["interface"])
    none = turn_context.for_turn(_turn(w.owner, session), w.agent)
    _assert_whole(none.refusal, Code.SENSITIVE_NOT_OPEN)
    assert "agent has none for it" in none.refusal.why and "handles this site's sensitive data" in none.refusal.how_to_fix


def test_sender_blocked_says_so_and_never_why(w):
    contact = Contact.objects.create(email="x@example.org", workspace=w.ws, blocked_at=timezone.now(),
                                     blocked_reason="kept sending spam about loans")
    turn, _ = email_channel.refused_email_turn(w.agent, contact, origin=Turn.ORIGIN_EMAIL, idempotency_key="b",
                                               prompt="hello", origin_ref={}, routing=Turn.ANY)
    refusal = turn_context.blocked_sender(turn, w.agent).refusal
    _assert_whole(refusal, Code.SENDER_BLOCKED)
    assert turn.result_note == refusal.result_note
    assert "spam" not in turn.result_note and "spam" not in str(turn.resolved_context)
    assert refusal.who_can_fix == "an editor or owner of connect"


def test_content_deleted_says_exactly_what_the_send_paths_always_said(w):
    session = _chat(w)
    Session.objects.filter(pk=session.pk).update(content_deleted_at=timezone.now())
    session.refresh_from_db()
    ctx = _preview(w, w.owner, session)
    _assert_whole(ctx.refusal, Code.CONTENT_DELETED)
    assert ctx.refusal.message == session_access.CONTENT_DELETED_REFUSAL
    response = _send(w.owner, session)
    assert response.status_code == 409 and response.json()["detail"] == ctx.refusal.message


def test_cannot_write_names_the_sessions_owner_as_the_fix(w):
    session = _chat(w)
    SessionParticipant.objects.create(session=session, user=w.reader, role=SessionParticipant.VIEWER)
    ctx = _preview(w, w.reader, session)
    _assert_whole(ctx.refusal, Code.CANNOT_WRITE)
    assert ctx.refusal.why == "you can read this session but not act in it"
    assert ctx.refusal.how_to_fix == ("Have owner@dimagi.com make you an editor of it, or start your own conversation "
                                      "with agent.")
    assert ctx.refusal.who_can_fix == "owner@dimagi.com, who owns this session"
    response = _send(w.reader, session)
    assert response.status_code == 403 and response.json()["detail"] == ctx.refusal.message


def test_cli_not_offered_names_the_cli_and_both_fixes(w):
    session = _chat(w)
    Session.objects.filter(pk=session.pk).update(cli="codex")
    session.refresh_from_db()
    ctx = _preview(w, w.owner, session)
    _assert_whole(ctx.refusal, Code.CLI_NOT_OFFERED)
    assert ctx.refusal.why == "this conversation runs on Codex, and Agent is no longer set up for Codex"
    assert ctx.refusal.how_to_fix.startswith("Start a new conversation with Agent, or have owner@dimagi.com")
    response = _send(w.owner, session)
    assert response.status_code == 422 and response.json()["detail"] == ctx.refusal.message


def test_not_in_sensitive_says_who_can_add_them(w):
    session = _chat(w, embed_app="data-site", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    ctx = _preview(w, w.owner, session, chat_surface=False)
    _assert_whole(ctx.refusal, Code.NOT_IN_SENSITIVE)
    assert "only the people in it can send into it" in ctx.refusal.why
    assert ctx.refusal.how_to_fix.startswith("Have owner@dimagi.com add you to it")
    raw, _token = PersonalToken.create_for_user(user=w.owner, label="t")
    response = _send(w.owner, session, Authorization=f"Bearer {raw}")
    assert response.status_code == 403 and response.json()["detail"] == ctx.refusal.message


# --- the set ---------------------------------------------------------------------------------------

def test_every_code_has_its_words_and_nothing_else_does():
    assert set(refusals._WORDS) == set(Code)
    assert turn_context.REFUSAL_CODES == frozenset(Code)
    assert {session_context.CONTENT_DELETED, session_context.CANNOT_WRITE, session_context.CLI_NOT_OFFERED,
            session_context.NOT_IN_SENSITIVE} <= set(Code)


def test_no_refusal_escapes_without_a_code_from_the_set():
    with pytest.raises(ValueError, match="not a refusal code"):
        refusals.Refusal(code="nope", why="something")
    with pytest.raises(ValueError):
        refusals.make("nope")
    with pytest.raises(ValueError):
        turn_context.TurnContext(standing=turn_context.Standing(relationship="member"), access=turn_context.REFUSED,
                                 profile=None, basis="refused", session_key_suffix="", review=turn_context.Review(),
                                 stamped=None, refusal=None, trace=())


def test_every_refusal_the_rule_makes_across_the_world_is_whole(w):
    """Every refused answer for every person, entry and conversation in the shared world carries a whole refusal."""
    sessions = [None, _chat(w), _chat(w, embed_app="connect-labs"),
                _chat(w, embed_app="data-site", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})]
    contact = Contact.objects.create(email="c@example.org", workspace=w.ws)
    seen = set()
    for person in (w.owner, w.admin, w.editor, w.reader, w.site_user, w.stranger, w.member, contact):
        for session in sessions:
            for requested in (None, "ask", "read", "connect", "stock", "data", "bare", "nope"):
                for verified in (True, False):
                    if session is None:
                        ctx = turn_context.for_asker(w.agent, person, verified=verified, requested=requested)
                    else:
                        ctx = turn_context.resolve_turn_context(
                            session_context.build(session), turn_context.Asker(who=person, verified=verified),
                            w.agent, requested=requested)
                    if ctx.refused:
                        _assert_whole(ctx.refusal, ctx.refusal.code)
                        seen.add(ctx.refusal.code)
    assert seen >= {Code.NOT_ON_ACCESS_LIST, Code.PROFILE_NOT_OPEN, Code.SENSITIVE_NOT_OPEN, Code.CANNOT_WRITE,
                    Code.NOT_IN_SENSITIVE}


_BANNED = re.compile(r"\b(doors?|ask|asks|asked|asking|capabilit(?:y|ies))\b", re.IGNORECASE)
_FACTS = [{}, {"automated": True}, {"name": "ask"}, {"name": "read"}, {"name": "connect", "published": True},
          {"name": "nope", "published": False}, {"name": "session_review"}, {"entry": None}, {"entry": "data"},
          {"cli": "Codex"}]


@pytest.mark.parametrize("code", list(Code))
def test_no_refusal_says_door_ask_or_capability(w, code):
    session = _chat(w)
    for facts in _FACTS:
        for subject in ("", "Ana"):
            for agent in (w.agent, None):
                r = refusals.make(code, agent=agent, session=session, subject=subject, **facts)
                for text in (r.why, r.how_to_fix, r.who_can_fix):
                    assert not _BANNED.search(text), (code, facts, text)


@pytest.mark.parametrize("code", list(Code))
def test_a_refusal_said_about_someone_else_never_says_you(w, code):
    session = _chat(w)
    for facts in _FACTS:
        about = refusals.make(code, agent=w.agent, session=session, **facts).about("Ana Lima")
        assert about.code == code
        for text in (about.why, about.how_to_fix, about.who_can_fix):
            assert not re.search(r"\byou(r)?\b", text, re.IGNORECASE), (code, facts, text)
    # Said about them, a refusal that names who it is about names them.
    said = refusals.make(code, agent=w.agent, session=session).about("Ana Lima")
    if re.search(r"\byou(r)?\b", " ".join((refusals.make(code, agent=w.agent, session=session).why,
                                             refusals.make(code, agent=w.agent, session=session).how_to_fix))):
        assert "Ana Lima" in f"{said.why} {said.how_to_fix}"


# --- the add-participant notice ---------------------------------------------------------------------

def _add(by, session, email, role="editor"):
    c = Client()
    c.force_login(by)
    response = c.post(f"/api/canopy-sessions/{session.pk}/participants", data={"email": email, "role": role},
                      content_type="application/json")
    assert response.status_code == 200, response.content
    return {row["email"]: row for row in response.json()}


def test_whoever_adds_someone_who_cannot_take_a_turn_is_told_why_about_them(w):
    session = _chat(w, creator=w.reader)
    rows = _add(w.reader, session, w.member.email)
    told = rows[w.member.email]["turn_refusal"]
    assert told["code"] == Code.NOT_ON_ACCESS_LIST
    assert told["why"] == "member@dimagi.com is not on agent's access list"
    assert "add member@dimagi.com to its access list" in told["how_to_fix"] and "you" not in told["how_to_fix"]
    # The reader may not edit the agent's access list, so no fix is offered to them; the words name who can.
    assert rows[w.member.email]["access_list_agent"] is None
    # Someone who can take a turn here is not flagged.
    assert _add(w.reader, session, w.editor.email)[w.editor.email]["turn_refusal"] is None


def test_an_adder_who_may_edit_the_access_list_is_offered_the_line(w):
    session = _chat(w, creator=w.reader)
    SessionParticipant.objects.create(session=session, user=w.owner, role=SessionParticipant.EDITOR)
    rows = _add(w.owner, session, w.member.email)
    assert rows[w.member.email]["turn_refusal"]["code"] == Code.NOT_ON_ACCESS_LIST
    assert rows[w.member.email]["access_list_agent"] == "agent"
    # The line it offers is the fix: read only lets them take a turn, and the notice clears.
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.PERSON, user=w.member, role=roles.READ_ONLY)
    c = Client()
    c.force_login(w.owner)
    again = {r["email"]: r for r in c.get(f"/api/canopy-sessions/{session.pk}/participants").json()}
    assert again[w.member.email]["turn_refusal"] is None


def test_an_editor_added_to_the_owners_own_session_is_flagged_like_anyone_else(w):
    # Before 2026-11-06 an editor here was a session writer and never flagged; being in it now gives nothing.
    rows = _add(w.owner, _chat(w), w.member.email)
    assert rows[w.member.email]["turn_refusal"]["code"] == Code.NOT_ON_ACCESS_LIST
    assert rows[w.member.email]["access_list_agent"] == "agent"


def test_a_viewer_is_told_the_role_not_the_access_list(w):
    session = _chat(w)
    rows = _add(w.owner, session, w.member.email, role="viewer")
    told = rows[w.member.email]
    assert told["turn_refusal"]["code"] == Code.CANNOT_WRITE and told["access_list_agent"] is None
    assert told["turn_refusal"]["why"] == "member@dimagi.com can read this session but not act in it"


def test_nobody_who_may_not_add_people_is_told_about_anyone(w):
    session = _chat(w, creator=w.reader)
    _add(w.reader, session, w.member.email)
    SessionParticipant.objects.create(session=session, user=w.editor, role=SessionParticipant.VIEWER)
    c = Client()
    c.force_login(w.editor)
    rows = c.get(f"/api/canopy-sessions/{session.pk}/participants").json()
    assert all(row["turn_refusal"] is None and row["access_list_agent"] is None for row in rows)
