"""A queued turn runs on the rules as they are when it starts, like any other turn.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 3.3 "Two moments" and § 12. The enqueue's
answer is an early one (an immediate refusal, the preview); the claim puts the same question to the rule again
(`turn_context.at_claim`), stamps `Turn.capability` with what it says and runs on that, or closes the turn as a
refusal. One test per thing that can change while a turn waits, each through a real enqueue and a real claim.
"""
from __future__ import annotations

from unittest import mock

import pytest

from apps.agents import refusals, roles, turn_context
from apps.agents.models import AgentGrant
from apps.canopy_sessions.models import Session
from apps.harness import caller_context, claim, claim_payload
from apps.harness import runner_requirements as rr
from apps.harness.models import Turn, TurnEvent

from .fixtures.turn_context import _chat
from .fixtures.turn_context_claims import (
    FACT,
    PROMPT,
    VAULT_VALUE,
    _assert_clean,
    _box,
    _claim,
    _enqueue,
    _ran_as,
    _seed_fact,
    _sensitive_box,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world(claim_world):
    return claim_world

CHANGED = refusals.CHANGED_WHILE_QUEUED


def _row(turn) -> Turn:
    return Turn.objects.get(pk=turn.pk)


def _set_role(world, user, role):
    line = AgentGrant.objects.get(agent=world.agent, user=user)
    line.role = role
    line.save()      # through `save`, which points the line at the role's row


def _refused_at_claim(turn, box, code):
    """The box passes it over, and it is closed as a refusal: never claimed, the note saying why, how to fix it and
    that it changed after it was sent, and the record holding the refusal."""
    assert claim.claim_next_turn(box) is None
    row = _row(turn)
    assert row.status == Turn.CANCELLED and row.claimed_at is None and row.claimed_by_id is None
    assert row.result_note.startswith(refusals.REFUSED_NOTE_PREFIX) and row.result_note.endswith(CHANGED)
    stored = row.resolved_context
    assert stored["moment"] == "claim" and stored["access"] == "refused" and stored["profile"] is None
    assert stored["refusal"]["code"] == code and stored["stamped"] is None
    assert row.result_note == f"{refusals.REFUSED_NOTE_PREFIX} {stored['refusal']['why']}. " \
                              f"{stored['refusal']['how_to_fix']} {CHANGED}"
    # Everything that asks what a turn ran as first asks whether it was refused.
    assert turn_context.was_refused(row) and caller_context.build(row)["access"] == "refused"
    return row


# --- narrower now ------------------------------------------------------------------------------------

def test_a_line_removed_while_it_waited_refuses_it_at_the_claim_with_nothing_seeded(world):
    """The Stage 2 seeded-secret test, for a turn refused at its claim: the record, the note and the ledger hold no
    prompt, no fact about the person and no value."""
    fact = _seed_fact(world, world.reader)
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session, box=world.box)
    assert turn.status == Turn.QUEUED and turn.capability == "ask"
    AgentGrant.objects.filter(agent=world.agent, user=world.reader).delete()

    row = _refused_at_claim(turn, world.box, turn_context.NOT_ON_ACCESS_LIST)
    assert row.resolved_context["queued_as"] == "ask" and row.resolved_context["runner"] is None
    assert "add you to its access list" in row.result_note
    ledger = list(TurnEvent.objects.filter(turn=row).values_list("payload", flat=True))
    assert any(p.get("refused_at_claim") == turn_context.NOT_ON_ACCESS_LIST for p in ledger)
    # The workspace's `offered-nothing` admission rule hears of it, as it does of a refusal at the enqueue.
    assert any(p.get("at") == "refusal" and p.get("admission_rule") for p in ledger)
    _assert_clean(row.resolved_context, VAULT_VALUE)
    _assert_clean({"note": row.result_note, "ledger": ledger}, VAULT_VALUE)
    # Controls: the prompt and the fact are really there to leak.
    assert row.prompt == PROMPT and fact.statement == FACT


def test_a_role_dropped_to_read_only_runs_it_in_read_mode_in_its_own_session(world):
    session = _chat(world, creator=world.editor)
    turn = _enqueue(world.editor, session, box=world.box)
    assert turn.capability == ""
    _set_role(world, world.editor, roles.READ_ONLY)

    got = _claim(turn, world.box)
    assert got.capability == "ask" and _row(turn).capability == "ask"
    stored = _row(turn).resolved_context
    assert (stored["access"], stored["stamped"], stored["session_key_suffix"]) == ("confined", "ask", "#ask")
    assert stored["queued_as"] == "" and stored["standing"]["permissions"] == [roles.READ]
    # Not the conversation's whole-agent runner session, and a caller token, not the agent's whole reach.
    payload = claim_payload.claim_payload(got)
    env = payload["caller_context"]
    assert env["profile"] == "confined" and env["access"] == "confined" and env["capability"]["name"] == "ask"
    assert env["permissions"] == [roles.READ] and payload["mcp_token"]
    # What it ran as and what the rules say now are the same answer.
    assert _ran_as(world.editor, session, turn).json()["differs"] is False


def test_a_narrowed_turn_never_runs_whole_on_a_box_that_cannot_confine_it(world):
    """Fail closed: the narrower answer goes only to a box that can confine it. The box that cannot passes it over
    and writes the new stamp, so every box and every explanation of why it waits reads what it would run as."""
    plain = _box(world, "plain", clis={"claude-code": {"signed_in": True}})
    turn = _enqueue(world.editor, _chat(world, creator=world.editor), box=plain)
    _set_role(world, world.editor, roles.READ_ONLY)

    assert claim.claim_next_turn(plain) is None
    row = _row(turn)
    assert row.status == Turn.QUEUED and row.capability == "ask" and row.claimed_by_id is None
    assert claim.claim_next_turn(plain) is None       # and it stays invisible to that box

    Turn.objects.filter(pk=turn.pk).update(pinned_runner=world.box)
    got = _claim(turn, world.box)
    assert got.capability == "ask"
    assert claim_payload.claim_payload(got)["caller_context"]["profile"] == "confined"
    # It waited under "", and the record says what it ran as instead.
    assert _row(turn).resolved_context["stamped"] == "ask"


def test_a_page_selected_site_whose_line_went_falls_back_to_read_mode_as_a_new_message_would(world):
    """The claim asks what the sender asked, not what they were stamped: someone who named nothing and got the page's
    host site gets read mode once that line goes, exactly as a message sent now would."""
    AgentGrant.objects.create(agent=world.agent, kind=AgentGrant.PERSON, user=world.member, role=roles.READ_ONLY)
    line = AgentGrant.objects.create(agent=world.agent, kind=AgentGrant.PERSON, user=world.member, capability="connect")
    session = _chat(world, creator=world.member, embed_app="connect-labs")
    turn = _enqueue(world.member, session, box=world.box)
    assert turn.capability == "connect"
    line.delete()

    got = _claim(turn, world.box)
    assert got.capability == "ask" and _row(turn).resolved_context["queued_as"] == "connect"


def test_a_host_site_removed_while_it_waited_is_refused_not_run_as_deny_all(world):
    session = _chat(world, creator=world.site_user, embed_app="connect-labs")
    turn = _enqueue(world.site_user, session, box=world.box)
    assert turn.capability == "connect"
    world.agent.interface = {"capabilities": {"ask": world.agent.interface["capabilities"]["ask"]}}
    world.agent.save(update_fields=["interface"])
    row = _refused_at_claim(turn, world.box, turn_context.PROFILE_NOT_OPEN)
    _assert_clean(row.resolved_context, VAULT_VALUE)


def test_a_conversation_that_became_sensitive_refuses_a_turn_with_no_sensitive_entry(world):
    box = _sensitive_box(world)
    for who_ in (world.owner, world.reader):
        session = _chat(world, creator=who_)
        turn = _enqueue(who_, session, box=world.box)
        assert turn.status == Turn.QUEUED
        Session.objects.filter(pk=session.pk).update(metadata={rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
        Turn.objects.filter(pk=turn.pk).update(pinned_runner=box)
        row = _refused_at_claim(turn, box, turn_context.SENSITIVE_NOT_OPEN)
        # A sensitive turn keeps no prompt, and keeps canopy's own refusal: it holds nothing the turn read.
        assert row.prompt == "" and "sensitive data" in row.result_note
        Turn.objects.filter(chat_session=session).delete()


def test_a_whole_agent_turn_moved_onto_a_sensitive_site_runs_confined_to_that_sites_entry(world):
    session = _chat(world)
    turn = _enqueue(world.owner, session)
    assert turn.capability == ""
    Session.objects.filter(pk=session.pk).update(
        metadata={"embed_app": "data-site", rr.METADATA_KEY: [rr.SENSITIVE_DATA]})
    box = _sensitive_box(world)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=box)

    got = _claim(turn, box)
    assert got.capability == "data"
    stored = _row(turn).resolved_context
    assert (stored["access"], stored["profile"]["kind"], stored["session_key_suffix"]) == \
           ("confined", "host_site", "#data")
    assert claim_payload.claim_payload(got)["caller_context"]["capability"]["name"] == "data"


def test_read_mode_changed_while_it_waited_runs_the_read_mode_there_is_now(world):
    declare_none = {"capabilities": {k: v for k, v in world.agent.interface["capabilities"].items() if k != "ask"}}
    world.agent.interface = declare_none
    world.agent.save(update_fields=["interface"])
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session, box=world.box)
    assert turn.capability == "read"                           # canopy's default read mode
    # What saving the Read mode setting writes (`answering.set_read_mode`, which this fixture's older entries refuse).
    world.agent.interface["capabilities"]["ask"] = {"description": "Answers from the runbook", "tools": ["Read", "Grep"]}
    world.agent.save(update_fields=["interface"])

    got = _claim(turn, world.box)
    assert got.capability == "ask"                              # the agent's own, as a message sent now gets
    stored = _row(turn).resolved_context
    assert stored["queued_as"] == "read" and stored["session_key_suffix"] == "#ask"
    assert stored["profile"]["tools"] == ["Read", "Grep"]
    assert claim_payload.claim_payload(got)["caller_context"]["capability"]["tools"] == ["Read", "Grep"]


def test_a_turn_whose_agent_was_deleted_while_it_waited_is_refused_not_run_without_an_agent(world):
    for who_, stamp in ((world.owner, ""), (world.reader, "ask")):
        session = _chat(world, creator=who_)
        turn = _enqueue(who_, session, box=world.box)
        assert turn.capability == stamp
        Session.objects.filter(pk=session.pk).update(agent=None)
        row = _refused_at_claim(turn, world.box, refusals.Code.AGENT_GONE)
        assert "deleted" in row.result_note
        _assert_clean(row.resolved_context, VAULT_VALUE)


def test_a_turn_with_no_agent_from_the_start_is_what_it_was(world):
    """A person's own repo or project chat never had an agent: nothing is asked again, and nothing is refused."""
    session = Session.objects.create(workspace=world.ws, created_by=world.owner, metadata={})
    turn = _enqueue(world.owner, session, box=world.box)
    assert turn_context.QUEUED_FOR not in (turn.origin_ref or {})
    ctx = turn_context.at_claim(_row(turn))
    assert (ctx.access, ctx.profile.kind) == (turn_context.FULL, turn_context.NO_AGENT)


# --- wider now -----------------------------------------------------------------------------------------

def test_a_role_widened_while_it_waited_runs_the_whole_agent(world):
    session = _chat(world, creator=world.reader)
    turn = _enqueue(world.reader, session, box=world.box)
    assert turn.capability == "ask"
    _set_role(world, world.reader, roles.EDITOR)

    got = _claim(turn, world.box)
    assert got.capability == ""
    stored = _row(turn).resolved_context
    assert (stored["access"], stored["stamped"], stored["session_key_suffix"]) == ("full", "", "")
    assert stored["queued_as"] == "ask"
    env = claim_payload.claim_payload(got)["caller_context"]
    assert env["profile"] == "full" and env["access"] == "full" and roles.WRITE in env["permissions"]


# --- what does not change ----------------------------------------------------------------------------

def test_a_turn_whose_answer_did_not_change_records_no_queued_stamp(world):
    turn = _enqueue(world.reader, _chat(world, creator=world.reader), box=world.box)
    got = _claim(turn, world.box)
    assert got.capability == "ask" and _row(turn).resolved_context["queued_as"] is None


def test_the_enqueues_question_is_the_servers_never_a_callers(world):
    session = _chat(world, creator=world.member, embed_app="connect-labs")
    AgentGrant.objects.create(agent=world.agent, kind=AgentGrant.PERSON, user=world.member, role=roles.READ_ONLY)
    from apps.harness import initiator as who
    from apps.harness import services

    turn, _ = services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hi", idempotency_key="forged",
        origin_ref={turn_context.QUEUED_FOR: {"agent": world.agent.pk, "profile": "connect"}},
        initiator=who.for_user(world.member, via="t", assurance=who.SESSION))
    assert turn.origin_ref[turn_context.QUEUED_FOR] == {"agent": world.agent.pk, "profile": None}
    # A turn with no agent writes none, so a forged one would be all there is: it is dropped on the way in.
    own = Session.objects.create(workspace=world.ws, created_by=world.owner, metadata={})
    agentless, _ = services.enqueue_turn(
        session=own, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hi", idempotency_key="forged-2",
        origin_ref={turn_context.QUEUED_FOR: {"agent": world.agent.pk, "profile": None}},
        initiator=who.for_user(world.owner, via="t", assurance=who.SESSION))
    assert turn_context.QUEUED_FOR not in agentless.origin_ref
    assert not turn_context.at_claim(Turn.objects.get(pk=agentless.pk)).refused


def test_a_rule_that_cannot_be_asked_at_the_claim_leaves_the_turn_waiting_never_run_on_its_old_stamp(world):
    turn = _enqueue(world.reader, _chat(world, creator=world.reader), box=world.box)
    with mock.patch.object(turn_context, "at_claim", side_effect=RuntimeError("boom")):
        assert claim.claim_next_turn(world.box) is None
    assert _row(turn).status == Turn.QUEUED


def test_slack_says_a_claim_refusal_in_the_thread_and_privately_to_its_sender(world, django_capture_on_commit_callbacks):
    from apps.slack import services as slack_services
    from apps.slack import status as slack_status
    from apps.slack.models import SlackInstallation, SlackUserLink, SlackWorkspaceLink

    installation = SlackInstallation.objects.create(team_id="T1", bot_token="xoxb-1", bot_user_id="UBOT")
    SlackWorkspaceLink.objects.create(installation=installation, workspace=world.ws)
    SlackUserLink.objects.create(installation=installation, slack_user_id="U-READER", user=world.reader)
    session = _chat(world, creator=world.reader, **{slack_services.SLACK_THREAD_KEY: "T1:C1:1.0",
                                                     "slack_team": "T1", "slack_channel": "C1",
                                                     "slack_thread_ts": "1.0"})
    with mock.patch("apps.slack.client.post_message", return_value="2.0"), \
            mock.patch("apps.slack.client.update_message"):
        turn = _enqueue(world.reader, session, box=world.box, origin=Turn.ORIGIN_SLACK)
        AgentGrant.objects.filter(agent=world.agent, user=world.reader).delete()
        with mock.patch("apps.slack.client.post_ephemeral") as ephemeral, \
                django_capture_on_commit_callbacks(execute=True):
            assert claim.claim_next_turn(world.box) is None
    row = _row(turn)
    assert row.status == Turn.CANCELLED
    ephemeral.assert_called_once()
    said = ephemeral.call_args.kwargs
    assert said["user"] == "U-READER" and said["channel"] == "C1" and said["thread_ts"] == "1.0"
    assert "you are not on agent's access list" in said["text"] and CHANGED in said["text"]
    _assert_clean({"said": said["text"]}, VAULT_VALUE)
    text, _blocks = slack_status.render(row, reach=None, cloud=None)
    assert "changed while it waited" in text
