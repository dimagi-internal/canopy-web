"""Stage 2 of the Turn Context: what a turn ran as is written down, the context routes serve it, and the
envelope reads it. Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 5."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.admission.models import AdmissionRule
from apps.agents import context_views, turn_context
from apps.agents.credentials import for_turn as credentials_for_turn
from apps.agents.models import AgentCredential, AgentGrant
from apps.api.api import api
from apps.canopy_sessions.models import REVIEW_METADATA_KEY, Session, SessionParticipant
from apps.contacts import people
from apps.contacts import services as contacts_mod
from apps.contacts.models import Contact
from apps.harness import caller_context, claim, claim_payload, services, turn_snapshot
from apps.harness import initiator as who
from apps.harness import runner_requirements as rr
from apps.harness.channels import email as email_channel
from apps.harness.models import Runner, RunnerFlag, Turn
from apps.harness.testing import enrolled
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership

from .fixtures.turn_context import _chat, make_world

pytestmark = pytest.mark.django_db

CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}
PROMPT = "SEEDEDPROMPTMARKER what are the KC metrics"
FACT = "SEEDEDPERSONFACT leads the KC metrics"
VAULT_VALUE = "zk1:SEEDEDVAULTVALUE"
NOTE = "SEEDEDCONTACTNOTE"
SEEDS = ("SEEDEDPROMPTMARKER", "SEEDEDPERSONFACT", "SEEDEDVAULTVALUE", "SEEDEDCONTACTNOTE", "SEEDEDBLOCKREASON")
_keys = iter(range(10**6))


@pytest.fixture()
def world():
    w = make_world()
    # The owner runs the box: a viewer's box serves no workspace.
    WorkspaceMembership.objects.filter(user=w.owner, workspace=w.ws).update(role=WorkspaceMembership.OWNER)
    AgentCredential.objects.create(agent=w.agent, name="SEEDED_NAME", value_zk=VAULT_VALUE)
    w.box = _box(w, "box-one")
    return w


def _box(w, name, **caps):
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=w.owner, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        ready=True, capabilities={"sessions": True, "midturn": 1, "clis": {"claude-code": CONFINING}, **caps}))


def _sensitive_box(w):
    box = _box(w, "sensitive-box", data_mode="sensitive_data")
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=w.owner)
    return box


def _seed_fact(w, user):
    """A fact the person has made available, so the envelope's `person` block really carries it."""
    response = _browser(user).put("/api/people/me/agent-memory/", {"use": {"available": True, "default": True}},
                                  content_type="application/json")
    assert response.status_code == 200, response.content
    return people.record_fact(person=contacts_mod.person_for(user=user), workspace=w.ws, kind="role", statement=FACT, basis="declared")


def _enqueue(user, session=None, *, agent=None, box=None, capability=None, prompt=PROMPT, origin=Turn.ORIGIN_CANOPY_WEB_CHAT):
    turn, _ = services.enqueue_turn(
        **({"session": session} if session is not None else {"agent": agent}), origin=origin, prompt=prompt,
        idempotency_key=f"s-{next(_keys)}", capability=capability, pinned_runner=box,
        initiator=who.for_user(user, via=origin, assurance=who.SESSION))
    return turn


def _claim(turn, box):
    got = claim.claim_next_turn(box)
    assert got is not None and got.pk == turn.pk, f"{turn.pk} was not claimed: {turn.status} {turn.result_note}"
    return got


def _browser(user):
    c = Client()
    c.force_login(user)
    return c


def _context(user, session, c=None):
    return (c or _browser(user)).get(f"/api/canopy-sessions/{session.pk}/context")


def _ran_as(user, session, turn, c=None):
    return (c or _browser(user)).get(f"/api/canopy-sessions/{session.pk}/turns/{turn.pk}/context")


def _keys_in(doc):
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield key
            yield from _keys_in(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _keys_in(value)


def _assert_clean(doc, *values):
    text = json.dumps(doc, default=str)
    for seed in (*SEEDS, *values):
        assert seed not in text, seed
    assert "person" not in set(_keys_in(doc))


# --- the seeded secret: a real claim, of every kind of profile --------------------------------------

def _whole_agent(w):
    return _chat(w), w.owner, w.box, "whole_agent"


def _read_mode(w):
    return _chat(w, creator=w.reader), w.reader, w.box, "read_mode"


def _default_read_mode(w):
    w.agent.interface = {"capabilities": {k: v for k, v in w.agent.interface["capabilities"].items() if k != "ask"}}
    w.agent.save(update_fields=["interface"])
    return _chat(w, creator=w.reader), w.reader, w.box, "read_mode"


def _connected_site(w):
    return _chat(w, creator=w.door, embed_app="connect-labs"), w.door, w.box, "connected_site"


def _session_review(w):
    return _chat(w, **{REVIEW_METADATA_KEY: "00000000-0000-0000-0000-000000000009"}), w.owner, w.box, "session_review"


def _sensitive_entry(w):
    session = _chat(w, embed_app="data-site", **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    return session, w.owner, _sensitive_box(w), "connected_site"


def _no_agent(w):
    return Session.objects.create(workspace=w.ws, created_by=w.owner, project="repo"), w.owner, w.box, "no_agent"


@pytest.mark.parametrize("make", [_whole_agent, _read_mode, _default_read_mode, _connected_site, _session_review,
                                  _sensitive_entry, _no_agent], ids=lambda f: f.__name__.strip("_"))
def test_a_claim_records_what_it_ran_as_and_nothing_seeded(world, make, grants_on_turn):
    session, user, box, kind = make(world)
    _seed_fact(world, user)
    turn = _enqueue(user, session, box=box)
    assert turn.status == Turn.QUEUED, turn.result_note
    got = _claim(turn, box)
    payload = claim_payload.claim_payload(got)
    # The world is seeded: the envelope really does hold the person, and the prompt is on the turn.
    if session.agent_id and rr.SENSITIVE_DATA not in rr.requirements_of_session(session):
        assert FACT in json.dumps(payload["caller_context"]["person"]), "the seed never reached the envelope"
    assert PROMPT in payload["prompt"] or rr.SENSITIVE_DATA in rr.requirements_of_session(session)

    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["moment"] == "claim" and stored["profile"]["kind"] == kind
    assert stored["runner"]["name"] == box.name
    minted = [v for v in (payload.get("mcp_token"), payload.get("chat_key")) if v]
    _assert_clean(stored, *minted)

    for body in (_ran_as(user, session, turn).json(), _context(user, session).json()):
        _assert_clean(body, *minted)
    body = _ran_as(user, session, turn).json()
    assert body["status"] == "recorded" and body["ran_as"]["profile"]["kind"] == kind
    assert _context(user, session).json()["turn_context"] is not None


def test_a_stamp_that_no_longer_resolves_is_recorded_as_deny_all_with_nothing_seeded(world):
    session = _chat(world, creator=world.door, embed_app="connect-labs")
    turn = _enqueue(world.door, session, box=world.box)
    assert turn.capability == "connect"
    world.agent.interface = {"capabilities": {"ask": world.agent.interface["capabilities"]["ask"]}}
    world.agent.save(update_fields=["interface"])
    got = _claim(turn, world.box)
    payload = claim_payload.claim_payload(got)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["profile"]["kind"] == "deny_all" and stored["profile"]["tools"] == [] and stored["access"] == "confined"
    assert payload["caller_context"]["profile"] == "confined" and payload["caller_context"]["capability"]["tools"] == []
    _assert_clean(stored, payload["mcp_token"])


def test_a_refused_turn_stores_its_refusal_with_nothing_seeded(world):
    session = _chat(world, creator=world.stranger)
    turn = _enqueue(world.stranger, session, capability="connect")
    assert turn.status == Turn.CANCELLED
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["moment"] == "enqueue" and stored["access"] == "refused" and stored["profile"] is None
    assert stored["refusal"]["code"] == turn_context.NOT_ON_ACCESS_LIST and stored["requested"] == "connect"
    assert world.owner.email in stored["refusal"]["how_to_fix"] and world.owner.email in stored["refusal"]["who_can_fix"]
    assert {line["gets"] for line in stored["credentials"]} == {credentials_for_turn.NO}
    _assert_clean(stored)


def test_a_blocked_senders_mail_stores_that_and_never_why_they_were_blocked(world):
    contact = Contact.objects.create(email="x@example.org", workspace=world.ws, blocked_at=timezone.now(), notes=NOTE,
                                     blocked_reason="SEEDEDBLOCKREASON")
    turn, _ = email_channel.refused_email_turn(world.agent, contact, origin=Turn.ORIGIN_EMAIL, idempotency_key="blocked",
                                               prompt=PROMPT, origin_ref={}, routing=Turn.ANY)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["access"] == "refused" and stored["refusal"]["code"] == turn_context.SENDER_BLOCKED
    assert stored["refusal"]["who_can_fix"]
    _assert_clean(stored)
    # Stage 4: its note is built from the refusal, and never says why they were blocked either.
    assert turn.result_note.startswith("not run: the sender is blocked. ")
    _assert_clean({"note": Turn.objects.get(pk=turn.pk).result_note})


def test_a_refusals_note_and_every_channels_delivery_hold_no_prompt_person_fact_or_secret(world):
    """Stage 4: `result_note`, the Slack reply, a send door's 403 and the people panel's notice say the refusal, and
    nothing a person wrote, nothing canopy knows about them and no value."""
    from apps.slack import services as slack_services

    fact = _seed_fact(world, world.member)
    Contact.objects.create(email=world.member.email, workspace=world.ws, notes=NOTE)
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.member, session)
    assert turn.status == Turn.CANCELLED and turn.result_note.startswith("not run: you are not on")
    with mock.patch.object(slack_services, "agent_list", return_value=""):
        slack = slack_services.refusal_message(world.agent, Turn.objects.get(pk=turn.pk), named=False, installation=None)
    assert "you are not on agent's access list" in slack and "add you to its access list" in slack
    SessionParticipant.objects.create(session=session, user=world.member, role=SessionParticipant.VIEWER)
    door = _browser(world.member).post(f"/api/canopy-sessions/{session.pk}/send", data={"text": PROMPT},
                                         content_type="application/json")
    assert door.status_code == 403 and "read this session but not act in it" in door.json()["detail"]
    panel = _browser(world.reader).get(f"/api/canopy-sessions/{session.pk}/participants").json()
    assert any(row["turn_refusal"] for row in panel)
    for delivered in (Turn.objects.get(pk=turn.pk).result_note, slack, door.json(), panel):
        _assert_clean({"delivered": delivered}, VAULT_VALUE)
    # Controls: the prompt, the fact and the note are really there to leak, so their absence above is real.
    assert Turn.objects.get(pk=turn.pk).prompt == PROMPT
    assert fact.statement == FACT and Contact.objects.filter(email=world.member.email, notes=NOTE).exists()


# --- who sees what ----------------------------------------------------------------------------------

def test_someone_who_cannot_read_the_session_gets_no_context_of_it(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    for person in (world.stranger, world.member, world.reader):
        assert _context(person, session).status_code == 404
        assert _ran_as(person, session, turn).status_code == 404


def test_a_reader_of_the_session_gets_it_and_a_turn_they_may_not_read_is_withheld(world):
    session = _chat(world)
    SessionParticipant.objects.create(session=session, user=world.reader, role=SessionParticipant.VIEWER)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    body = _context(world.reader, session).json()
    assert body["session_context"]["session_id"] == str(session.pk)
    assert body["turn_context"]["refusal"]["code"] == "cannot_write"    # they can read it, and it says why not more
    # A chat's log is its readers', so they see this turn: the other leg, below, is a turn of the agent's own.
    assert _ran_as(world.reader, session, turn).json()["status"] == "recorded"


def test_a_turn_of_another_session_is_not_found_through_this_one(world):
    mine, other = _chat(world), _chat(world)
    turn = _enqueue(world.owner, other, box=world.box)
    assert _ran_as(world.owner, mine, turn).status_code == 404


def test_a_snapshot_follows_the_turn_log_rule_not_the_session_rule(world):
    # An agent turn that ran in a session the reader may read: reading the session opens nothing of the turn.
    session = _chat(world)
    SessionParticipant.objects.create(session=session, user=world.reader, role=SessionParticipant.VIEWER)
    turn = _enqueue(world.owner, agent=world.agent, box=world.box)
    _claim(turn, world.box)
    Turn.objects.filter(pk=turn.pk).update(canopy_session=session)
    body = _ran_as(world.reader, session, turn).json()
    assert body["status"] == "withheld" and body["ran_as"] is None and body["now"] is None
    assert _ran_as(world.owner, session, turn).json()["status"] == "recorded"


def _sensitive_world(w):
    # A thread an agent's admin may read and is not in, with one of its people in it.
    session = Session.objects.create(agent=w.agent, workspace=w.ws, created_by=w.owner, origin=Session.ORIGIN_RUNNER,
                                     metadata={"embed_app": "data-site", rr.METADATA_KEY: [rr.SENSITIVE_DATA]},
                                     page_state={"resource": "patient://SEEDEDPAGE"})
    box = _sensitive_box(w)
    turn = _enqueue(w.owner, session, box=box)
    _claim(turn, box)
    return session, turn


def test_a_reader_outside_a_sensitive_conversation_gets_its_facts_and_no_turn_of_it(world):
    session, turn = _sensitive_world(world)
    assert _context(world.admin, session).status_code == 200    # they can read it: this is what is being narrowed
    body = _context(world.admin, session).json()
    assert body["turn_context"] is None and "only the people in it" in body["withheld"]
    assert body["session_context"]["sensitive"] is True
    assert "SEEDEDPAGE" not in json.dumps(body)         # its page is its own people's
    ran = _ran_as(world.admin, session, turn).json()
    assert ran["status"] == "withheld" and ran["ran_as"] is None and ran["now"] is None
    assert "stamped" not in json.dumps(ran)


def test_its_own_people_get_both_with_no_words(world):
    session, turn = _sensitive_world(world)
    SessionParticipant.objects.create(session=session, user=world.member, role=SessionParticipant.VIEWER)
    for person in (world.owner, world.member):
        body = _context(person, session).json()
        assert body["turn_context"] is not None and body["withheld"] == ""
        assert body["session_context"]["page"] == "patient://SEEDEDPAGE"
    ran = _ran_as(world.owner, session, turn).json()
    assert ran["status"] == "recorded"
    _assert_clean(ran)


def test_its_own_person_through_a_token_gets_no_turn_of_it(world):
    # A token is the door an AI reads through, whoever it reads as.
    session, turn = _sensitive_world(world)
    raw, _token = PersonalToken.create_for_user(user=world.owner, label="t")
    c = Client(headers={"Authorization": f"Bearer {raw}"})
    assert _context(world.owner, session, c).json()["turn_context"] is None
    assert _ran_as(world.owner, session, turn, c).json()["status"] == "withheld"


def test_a_turn_sensitive_through_its_parent_is_held_to_its_sessions_people(world):
    parent = _chat(world, **{rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    session = Session.objects.create(agent=world.agent, workspace=world.ws, created_by=world.owner, origin=Session.ORIGIN_RUNNER)
    turn = Turn.objects.create(agent=world.agent, canopy_session=session, parent_session=parent, origin=Turn.ORIGIN_API,
                               initiator_kind=who.USER, initiator_user=world.owner, initiator_assurance=who.SESSION, prompt="hi",
                               idempotency_key="child")
    assert _ran_as(world.admin, session, turn).json()["status"] == "withheld"


# --- what is stored, and when ------------------------------------------------------------------------

def test_a_reclaim_overwrites_so_it_holds_the_run_that_last_happened(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    first = Turn.objects.get(pk=turn.pk).resolved_context
    assert first["runner"]["name"] == "box-one"

    second = _box(world, "box-two")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.QUEUED, claimed_by=None, lease_expires_at=None, pinned_runner=second)
    _claim(turn, second)
    last = Turn.objects.get(pk=turn.pk).resolved_context
    assert last["runner"]["name"] == "box-two" and last["recorded_at"] != first["recorded_at"]


def test_the_enqueue_stamp_wins_at_claim_and_says_so(world):
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session, box=world.box)
    assert turn.capability == "ask"
    AgentGrant.objects.filter(agent=world.agent, user=world.reader).delete()    # no longer admitted
    got = _claim(turn, world.box)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["access"] == "confined" and stored["stamped"] == "ask" and stored["session_key_suffix"] == "#ask"
    assert caller_context.build(got)["capability"]["name"] == "ask"
    body = _ran_as(world.reader, session, turn).json()
    assert body["ran_as"]["access"] == "confined" and body["now"]["access"] == "refused" and body["differs"] is True


def test_nothing_about_a_turn_changes_who_it_runs_as_when_it_agrees(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    body = _ran_as(world.owner, session, turn).json()
    assert body["differs"] is False and body["now"]["review"]["mode"] == turn_context.PENDING


def test_a_turn_with_no_record_says_not_recorded_and_what_the_rules_say(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    Turn.objects.filter(pk=turn.pk).update(resolved_context=None)
    body = _ran_as(world.owner, session, turn).json()
    assert body["status"] == "not_recorded" and body["note"] == "not recorded" and body["ran_as"] is None
    assert body["differs"] is False and body["now"]["access"] == "full"


def test_a_queued_turn_has_no_record_yet(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session)
    assert Turn.objects.get(pk=turn.pk).resolved_context is None


def test_an_admission_rule_at_the_claim_is_recorded_beside_the_turn_context(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    rule = AdmissionRule.objects.create(workspace=world.ws, agent=world.agent, action=AdmissionRule.REJECT, enabled=True,
                                        runner_kind=Runner.CLOUD, reason="no cloud")
    assert claim.claim_next_turn(world.box) is None
    row = Turn.objects.get(pk=turn.pk)
    assert row.status == Turn.CANCELLED
    stored = row.resolved_context
    assert stored["moment"] == "claim" and stored["access"] == "full"
    assert stored["admission"]["outcome"] == "rejected" and stored["admission"]["rule"] == rule.pk
    assert stored["admission"]["mode"] == "auto"    # the agent's, and the owner's turn: it would have run in it
    _assert_clean(stored)


def test_an_admission_rule_at_enqueue_is_recorded_too(world):
    rule = AdmissionRule.objects.create(workspace=world.ws, agent=world.agent, action=AdmissionRule.REJECT, enabled=True,
                                        reason="closed")
    turn = _enqueue(world.owner, _chat(world))
    row = Turn.objects.get(pk=turn.pk)
    assert row.status == Turn.CANCELLED and row.resolved_context["moment"] == "enqueue"
    assert row.resolved_context["admission"]["rule"] == rule.pk


def test_the_mode_it_records_is_the_one_the_claim_stamped_not_the_agents_switch(world):
    # The agent is `auto`, and a non-admin's turn runs manual: only the claim's stamp knows which.
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    got = _claim(turn, world.box)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert got.turn_mode and stored["review"]["mode"] == got.turn_mode
    assert stored["review"]["basis"] == got.turn_mode_basis
    assert caller_context.build(got)["turn_mode"]["mode"] == stored["review"]["mode"]


def test_a_rule_that_raises_the_mode_is_recorded_as_having_done_so(world):
    world.agent.turn_mode = "manual"
    world.agent.save(update_fields=["turn_mode"])
    AdmissionRule.objects.create(workspace=world.ws, agent=world.agent, action=AdmissionRule.AUTO, enabled=True,
                                 runner_kind=Runner.CLOUD, reason="cloud may")
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    got = _claim(turn, world.box)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert got.turn_mode == "auto" and stored["review"]["mode"] == "auto"
    assert stored["admission"]["outcome"] == "raised_to_auto"


# --- what the fan-out says is what the claim does ------------------------------------------------------

def _gets(stored) -> dict:
    return {line["name"]: line["gets"] for line in stored["credentials"]}


@pytest.mark.parametrize("make", [_whole_agent, _read_mode, _connected_site, _session_review, _no_agent],
                         ids=lambda f: f.__name__.strip("_"))
def test_the_caller_token_it_states_is_the_one_the_claim_mints(world, make):
    session, user, box, _kind = make(world)
    turn = _enqueue(user, session, box=box)
    got = _claim(turn, box)
    payload = claim_payload.claim_payload(got)
    stated = _gets(Turn.objects.get(pk=turn.pk).resolved_context)["caller_token"]
    assert stated == (credentials_for_turn.YES if payload.get("mcp_token") else credentials_for_turn.NO)


def test_an_editors_whole_agent_claim_states_it_reads_as_them(world):
    session = _chat(world, creator=world.editor)
    turn = _enqueue(world.editor, session, box=world.box)
    payload = claim_payload.claim_payload(_claim(turn, world.box))
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["access"] == "full" and _gets(stored)["caller_token"] == credentials_for_turn.YES and payload["mcp_token"]


def test_what_a_box_hands_out_is_no_longer_pending_once_a_box_holds_the_turn(world):
    world.agent.user = world.member
    world.agent.save(update_fields=["user"])
    turn = _enqueue(world.owner, _chat(world), box=world.box)
    _claim(turn, world.box)
    gets = _gets(Turn.objects.get(pk=turn.pk).resolved_context)
    assert gets["canopy_login"] == credentials_for_turn.YES and gets["agent_vault"] == credentials_for_turn.YES


def test_a_turn_with_no_agent_gets_no_agents_vault_at_its_claim_or_in_a_preview(world):
    session = Session.objects.create(workspace=world.ws, created_by=world.owner, project="repo")
    preview = _context(world.owner, session).json()["turn_context"]
    assert _gets(preview)["agent_vault"] == credentials_for_turn.NO and _gets(preview)["canopy_login"] == credentials_for_turn.NO
    assert preview["standing"]["relationship"] == "owner"
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["profile"]["kind"] == "no_agent" and _gets(stored)["agent_vault"] == credentials_for_turn.NO
    assert stored["standing"]["relationship"] == "owner"     # their box, known only at the claim


# --- the envelope ------------------------------------------------------------------------------------

def test_the_envelope_says_access_and_keeps_the_field_runners_read(world):
    whole = _enqueue(world.owner, _chat(world))
    read = _enqueue(world.reader, _chat(world, creator=world.reader))
    refused = _enqueue(world.stranger, _chat(world, creator=world.stranger))
    fields = [(caller_context.build(t)["profile"], caller_context.build(t)["access"]) for t in (whole, read, refused)]
    assert fields == [("full", "full"), ("confined", "confined"), ("full", "refused")]


def test_a_turn_refused_before_this_record_still_says_refused(world):
    refused = _enqueue(world.stranger, _chat(world, creator=world.stranger))
    Turn.objects.filter(pk=refused.pk).update(resolved_context=None)
    assert caller_context.build(Turn.objects.get(pk=refused.pk))["access"] == "refused"


def test_a_stamped_turn_whose_agent_has_gone_is_never_the_whole_of_anything(world):
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session)
    assert turn.capability == "ask"
    Session.objects.filter(pk=session.pk).update(agent=None)
    row = Turn.objects.get(pk=turn.pk)
    ctx = turn_context.of_turn(row, None)
    assert ctx.profile.kind == turn_context.DENY_ALL and ctx.access == turn_context.CONFINED
    assert ctx.session_key_suffix == "#ask"
    env = caller_context.build(row)
    assert env["profile"] == "confined" and env["access"] == "confined"


def test_a_session_key_suffix_is_empty_for_the_whole_agent_at_a_claim(world):
    turn = _enqueue(world.owner, _chat(world), box=world.box)
    _claim(turn, world.box)
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["session_key_suffix"] == "" and stored["profile"]["tools"] is None


def test_a_claim_asks_the_rule_once_for_the_record_and_the_envelope(world):
    turn = _enqueue(world.reader, _chat(world, creator=world.reader), box=world.box)
    got = _claim(turn, world.box)
    with mock.patch.object(turn_context, "of_turn", side_effect=AssertionError("asked again")):
        assert caller_context.build(got)["capability"]["name"] == "ask"


# --- a record never stops a claim, and never widens one ---------------------------------------------------

def test_a_snapshot_that_cannot_be_built_does_not_stop_the_claim_or_widen_it(world):
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session, box=world.box)
    with mock.patch.object(context_views, "snapshot", side_effect=RuntimeError("boom")):
        got = _claim(turn, world.box)
    assert got.status == Turn.CLAIMED and got.capability == "ask"
    stored = Turn.objects.get(pk=turn.pk).resolved_context
    assert stored["error"] and "access" not in stored
    env = claim_payload.claim_payload(got)["caller_context"]
    assert env["profile"] == "confined" and env["capability"]["name"] == "ask"
    assert _ran_as(world.reader, session, turn).json()["status"] == "unavailable"


def test_a_failed_snapshot_does_not_leave_the_last_runs_to_pass_for_this_one(world):
    turn = _enqueue(world.owner, _chat(world), box=world.box)
    _claim(turn, world.box)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.QUEUED, claimed_by=None, lease_expires_at=None)
    with mock.patch.object(context_views, "snapshot", side_effect=RuntimeError("boom")):
        _claim(turn, world.box)
    assert "error" in Turn.objects.get(pk=turn.pk).resolved_context


def test_an_envelope_that_cannot_read_the_turn_context_fails_rather_than_says_full(world):
    # The claim's own answer is not there, the rule raises: nothing falls back to the whole agent.
    turn = _enqueue(world.reader, _chat(world, creator=world.reader))
    with (mock.patch.object(turn_context, "of_turn", side_effect=RuntimeError("boom"))), pytest.raises(RuntimeError):
        caller_context.build(Turn.objects.get(pk=turn.pk))


def test_a_refusal_stands_when_its_record_cannot_be_built(world):
    with mock.patch.object(context_views, "snapshot", side_effect=RuntimeError("boom")):
        turn = _enqueue(world.stranger, _chat(world, creator=world.stranger))
    assert turn.status == Turn.CANCELLED and turn.capability == ""
    row = Turn.objects.get(pk=turn.pk)
    assert row.resolved_context["error"] and caller_context.build(row)["access"] == "refused"


def test_a_failure_inside_the_record_is_rolled_back_to_its_own_savepoint(world):
    # What keeps it from poisoning a transaction around it on PostgreSQL. SQLite does not poison, so what this
    # watches is the savepoint itself: what the failed attempt wrote is undone, and what the caller wrote stands.
    turn = _enqueue(world.owner, _chat(world))
    Turn.objects.filter(pk=turn.pk).update(result_note="the caller's")

    def broken(*_a, **_k):
        Turn.objects.filter(pk=turn.pk).update(result_note="half a record")
        raise RuntimeError("boom")

    with mock.patch.object(context_views, "snapshot", side_effect=broken):
        turn_snapshot.record(turn, turn_snapshot.CLAIM, runner=world.box)
    row = Turn.objects.get(pk=turn.pk)
    assert row.result_note == "the caller's" and row.resolved_context["error"]


def test_recording_costs_a_bounded_number_of_queries_on_the_claims_hot_path(world, django_assert_max_num_queries):
    # An empty poll never gets here; this is per claimed turn, and the envelope built next reuses the answer.
    turn = _enqueue(world.editor, _chat(world, creator=world.editor), box=world.box)
    with django_assert_max_num_queries(45):
        turn_snapshot.record(turn, turn_snapshot.CLAIM, runner=world.box)
    assert turn.resolved_context["access"] == "full"


# --- reading writes nothing ----------------------------------------------------------------------------

#: Being signed in writes these, on any request (rolling sessions, `CLAUDE.md`).
_SIGN_IN_BOOKKEEPING = ("django_session",)


def _written_tables(queries) -> set:
    out = set()
    for query in queries:
        sql = query["sql"]
        if re.match(r"^\s*(insert|update|delete|replace)\b", sql, re.I):
            out.add(re.search(r'"(\w+)"', sql).group(1))
    return out - set(_SIGN_IN_BOOKKEEPING)


@pytest.mark.parametrize("make", [_whole_agent, _read_mode, _connected_site, _session_review, _no_agent],
                         ids=lambda f: f.__name__.strip("_"))
def test_a_preview_and_a_snapshot_read_write_nothing(world, make, grants_on_turn):
    session, user, box, _kind = make(world)
    _seed_fact(world, user)
    turn = _enqueue(user, session, box=box)
    _claim(turn, box)
    c = _browser(user)
    before = Turn.objects.filter(pk=turn.pk).values().get()
    with CaptureQueriesContext(connection) as seen:
        assert _context(user, session, c).status_code == 200
        assert _ran_as(user, session, turn, c).status_code == 200
    assert not _written_tables(seen.captured_queries)
    assert Turn.objects.filter(pk=turn.pk).values().get() == before


def test_building_the_envelope_does_write_so_the_detector_would_see_a_route_that_did(world, grants_on_turn):
    # The control for the test above: the one known writer among these neighbours, on the same seeded world.
    session, user, box, _kind = _whole_agent(world)
    _seed_fact(world, user)
    turn = _enqueue(user, session, box=box)
    with CaptureQueriesContext(connection) as seen:
        caller_context.build(Turn.objects.get(pk=turn.pk))
    assert _written_tables(seen.captured_queries)


def test_the_write_detector_sees_a_write(world):
    with CaptureQueriesContext(connection) as seen:
        Turn.objects.filter(pk=_enqueue(world.owner, _chat(world)).pk).update(prompt="x")
    assert "harness_turn" in _written_tables(seen.captured_queries)


# --- where it is served, and where it is not ---------------------------------------------------------

def test_no_turn_route_spreads_the_snapshot(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session, box=world.box)
    _claim(turn, world.box)
    c = _browser(world.owner)
    for url in (f"/api/canopy-sessions/{session.pk}/info", f"/api/harness/turns/{turn.pk}", f"/api/canopy-sessions/{session.pk}",
                "/api/harness/turns/?limit=5"):
        response = c.get(url)
        assert response.status_code == 200, url
        assert "resolved_context" not in response.content.decode(), url


@contextlib.contextmanager
def _as_user(user):
    access = AccessToken(token="t", client_id=str(user.pk), scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
    token = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def test_both_are_mcp_tools_and_a_sensitive_conversation_stays_withheld_there(world):
    operations = {op["operationId"] for methods in api.get_openapi_schema()["paths"].values() for op in methods.values()}
    assert {"get_session_context", "get_session_turn_context"} <= operations
    ordinary = _chat(world)
    sensitive, _turn = _sensitive_world(world)
    with _as_user(world.owner):
        got = async_to_sync(mcp.call_tool)("get_session_context", {"session_id": str(ordinary.pk)}).structured_content
        assert got["turn_context"]["access"] == "full"
        got = async_to_sync(mcp.call_tool)("get_session_context", {"session_id": str(sensitive.pk)}).structured_content
        assert got["turn_context"] is None and got["withheld"]


def test_the_view_is_an_allowlist_of_named_fields(world):
    ctx = turn_context.for_turn(_enqueue(world.owner, _chat(world)), world.agent)
    assert set(context_views.turn_context(ctx)) == {
        "standing", "access", "basis", "profile", "session_key_suffix", "credentials", "review", "stamped", "refusal", "trace"}
    stamp = dt.datetime(2026, 11, 5, tzinfo=dt.UTC)
    assert set(context_views.snapshot(ctx, moment="claim", at=stamp)) >= {"version", "moment", "recorded_at", "runner", "admission"}
