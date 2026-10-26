"""A follow-up is delivered INTO a conversation's running turn (canopy-web#1153).

Before this, a message sent while the agent was busy waited in the queue until
the turn ended — in the #1147 incident, 15+ minutes behind a `gh pr checks
--watch`, which is long after a correction like "skip that" stops mattering.
Claude Code already takes a message typed into a busy session (it queues it and
hands it to the model at its next step), so the runner holding the turn now
claims the follow-up at once as a RIDER (`Turn.rides_turn`), types it in, and
the two finish together.

Gated on the runner REPORTING it can (`midturn` on the heartbeat): older runner
code would claim a rider as an ordinary chat turn and bridge the reply twice.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import initiator as _initiator
from apps.harness import services as harness
from apps.harness import claim, routing
from apps.harness import turn_status as ts
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

_BY_CANOPY = _initiator.system(via="test")


def _ctx(*, midturn=True):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    session = Session.objects.create(workspace=ws, created_by=user, agent=agent, title="t")
    runner = Runner.objects.create(
        name="acedimagi-mbp-cdp", kind=Runner.EMDASH, host="h", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "midturn": int(midturn)})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return user, ws, agent, session, runner


def _send(session, key, prompt):
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key=key, prompt=prompt)
    return turn


def _running(session, runner):
    first = _send(session, "k1", "make the slide")
    assert claim.claim_next_turn(runner) == first
    return first


def test_a_follow_up_is_claimed_into_the_running_turn():
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    follow_up = _send(session, "k2", "skip the PR checks")

    # Not "queued behind": the runner holding the turn takes it on its next tick.
    assert routing.blocking_turn(follow_up) is None
    assert ts.resolve(follow_up).state == ts.PICKING_UP

    claimed = claim.claim_next_turn(runner)
    assert claimed == follow_up
    assert claimed.rides_turn_id == first.pk
    first.refresh_from_db()
    assert first.status == Turn.CLAIMED, "both execute: the rider is inside the running turn"
    assert ts.resolve(claimed).state == ts.WORKING
    claim_event = claimed.events.filter(kind="status").order_by("seq").last()
    assert claim_event.payload["rides_turn"] == str(first.pk)


def test_the_claimed_turn_tells_the_runner_what_it_rides():
    from apps.harness.claiming import issue_credentials

    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "and the other deck")
    out = issue_credentials(claim.claim_next_turn(runner))
    rides = out["rides_turn_id"] if isinstance(out, dict) else out.rides_turn_id
    assert str(rides) == str(first.pk)


def test_a_runner_that_does_not_report_midturn_still_queues_it():
    """The cloud runner, and any laptop on older code."""
    _user, _ws, _agent, session, runner = _ctx(midturn=False)
    first = _running(session, runner)
    follow_up = _send(session, "k2", "skip the PR checks")
    assert routing.blocking_turn(follow_up) == first
    assert ts.resolve(follow_up).state == ts.QUEUED_BEHIND
    assert claim.claim_next_turn(runner) is None


def test_a_confined_follow_up_never_rides_into_the_owners_session():
    """A caller's turn runs in its own `cx-` session; typing it into the
    owner's would hand a contact the owner's full profile."""
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    follow_up = _send(session, "k2", "what is this?")
    Turn.objects.filter(pk=follow_up.pk).update(capability="ask")
    follow_up.refresh_from_db()
    assert not routing.may_ride(follow_up, Turn.objects.get(pk=first.pk))
    assert claim.claim_next_turn(runner) is None


def test_another_runner_cannot_ride_a_turn_it_does_not_hold():
    user, ws, agent, session, runner = _ctx()
    _running(session, runner)
    other = Runner.objects.create(
        name="other", kind=Runner.EMDASH, host="o", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "midturn": 1})
    RunnerAssignment.objects.create(agent=agent, runner=other, rank=1)
    _send(session, "k2", "skip the PR checks")
    assert claim.claim_next_turn(other) is None


def test_riders_finish_with_the_turn_they_rode():
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)

    harness.finish_turn(first, status=Turn.DONE, result_note="chat reply bridged")
    rider.refresh_from_db()
    assert rider.status == Turn.DONE
    assert rider.result_note.startswith(f"delivered into the running turn {str(first.pk)[:8]}")
    assert ts.resolve(rider).state == ts.DONE


def test_a_rider_is_not_requeued_when_its_turn_fails_sessionless():
    """Requeueing would type an already-delivered message a second time."""
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)
    harness.finish_turn(first, status=Turn.FAILED, result_note="cdp down")
    first.refresh_from_db()
    rider.refresh_from_db()
    assert first.status == Turn.QUEUED          # the sessionless retry, as before
    assert rider.status == Turn.FAILED


def test_stopping_a_rider_stops_the_turn_it_rode(monkeypatch):
    sent = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: sent.append(m))
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)

    harness.cancel_turn(rider)
    assert first.events.filter(kind="cancel_requested").exists()
    assert {"type": "runner.cancel", "turn_id": str(first.pk)} in sent


def test_a_rider_lease_is_renewed_with_its_turn():
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)
    past = timezone.now() - _dt.timedelta(seconds=5)
    Turn.objects.filter(pk__in=[first.pk, rider.pk]).update(lease_expires_at=past)

    harness.heartbeat(runner, active_turn_ids=[str(first.pk)], midturn=1)
    rider.refresh_from_db()
    assert rider.lease_expires_at > timezone.now()


def test_midturn_is_reported_on_the_heartbeat_not_declared():
    _user, _ws, _agent, _session, runner = _ctx(midturn=False)
    harness.heartbeat(runner, active_turn_ids=[], midturn=1)
    runner.refresh_from_db()
    assert routing.delivers_midturn(runner)
    harness.heartbeat(runner, active_turn_ids=[])          # older code: absent = 0
    runner.refresh_from_db()
    assert not routing.delivers_midturn(runner)


def test_a_follow_up_waits_while_a_dialog_is_up():
    """Claude Code draws the dialog where the composer would be, so typing the
    follow-up would bounce. It waits for the answer, as a person's typing would;
    an option-less notification marker does not hold it."""
    from apps.canopy_sessions.models import RunnerBinding

    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    follow_up = _send(session, "k2", "skip the PR checks")
    binding, _ = RunnerBinding.objects.get_or_create(session=session, defaults={"runner": runner})
    binding.pending_question = {"question": "Proceed?", "options": [{"label": "Yes"}]}
    binding.save(update_fields=["pending_question"])
    assert routing.blocking_turn(follow_up) == first
    assert claim.claim_next_turn(runner) is None

    binding.pending_question = {"question": "Claude needs your attention", "options": []}
    binding.save(update_fields=["pending_question"])
    assert claim.claim_next_turn(runner) == follow_up


def test_a_follow_up_that_cannot_be_typed_mid_turn_waits_for_the_turn_instead():
    """Measured live 2026-10-05: the rider was claimed, every send failed
    COMPOSER_NOT_VISIBLE, and the follow-up ended FAILED — lost, where before
    mid-turn delivery it would only have been late. It now waits, exactly as it
    did before, and is delivered when the running turn ends."""
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)
    attempts = rider.attempts

    harness.finish_turn(rider, status=Turn.FAILED,
                        result_note="chat reuse send failed: COMPOSER_NOT_VISIBLE")
    rider.refresh_from_db()
    assert rider.status == Turn.QUEUED
    assert rider.rides_turn_id is None and rider.attempts == attempts
    assert rider.origin_ref.get(routing.MIDTURN_FAILED) is True

    # It does not ride again — it waits, and says so.
    assert claim.claim_next_turn(runner) is None
    assert routing.blocking_turn(rider) == first
    assert ts.resolve(rider).state == ts.QUEUED_BEHIND

    harness.finish_turn(first, status=Turn.DONE)
    again = claim.claim_next_turn(runner)
    assert again == rider and again.rides_turn_id is None


def test_a_rider_whose_keystrokes_went_out_is_never_requeued():
    """Typed but unconfirmed: the runner reports the session, so the failure is
    terminal — requeueing would type it a second time."""
    _user, _ws, _agent, session, runner = _ctx()
    _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)
    Turn.objects.filter(pk=rider.pk).update(session_key="ace-chat-1")
    rider.refresh_from_db()
    harness.finish_turn(rider, status=Turn.FAILED, result_note="couldn't confirm delivery")
    rider.refresh_from_db()
    assert rider.status == Turn.FAILED


# --- what the composer says (the session's turn status) ----------------------


def test_the_status_of_a_running_turn_says_a_follow_up_would_ride():
    """The composer's placeholder reads this: "it reads this mid-turn"."""
    _user, _ws, _agent, session, runner = _ctx()
    first = Turn.objects.get(pk=_running(session, runner).pk)
    status = ts.resolve(first).as_dict()
    assert status["state"] == ts.WORKING
    assert status["midturn"] is True and status["midturn_blocked"] is None


def test_a_rider_reports_the_same_answer_as_the_turn_it_rides():
    """Once a rider is the session's latest turn, the status is the rider's —
    and a second follow-up rides the same running turn."""
    _user, _ws, _agent, session, runner = _ctx()
    _running(session, runner)
    _send(session, "k2", "skip the PR checks")
    rider = claim.claim_next_turn(runner)
    assert rider.rides_turn_id is not None
    assert ts.resolve(rider).midturn is True


def test_the_status_says_why_a_follow_up_would_wait():
    from apps.canopy_sessions.models import RunnerBinding

    # A box that does not deliver mid-turn (the cloud runner).
    _user, _ws, _agent, session, runner = _ctx(midturn=False)
    first = Turn.objects.get(pk=_running(session, runner).pk)
    assert (ts.resolve(first).midturn, ts.resolve(first).midturn_blocked) == (False, "runner")

    # A dialog on the screen: it waits for the answer.
    Runner.objects.filter(pk=runner.pk).update(capabilities={"sessions": True, "midturn": 1})
    binding, _ = RunnerBinding.objects.get_or_create(session=session, defaults={"runner": runner})
    binding.pending_question = {"question": "Proceed?", "options": [{"label": "Yes"}]}
    binding.save(update_fields=["pending_question"])
    first = Turn.objects.get(pk=first.pk)
    assert ts.resolve(first).midturn_blocked == "dialog"

    # A caller's confined turn runs in a session of its own.
    binding.pending_question = None
    binding.save(update_fields=["pending_question"])
    Turn.objects.filter(pk=first.pk).update(capability="ask")
    assert ts.resolve(Turn.objects.get(pk=first.pk)).midturn_blocked == "confined"


def test_a_settled_turn_says_nothing_about_riding():
    _user, _ws, _agent, session, runner = _ctx()
    first = _running(session, runner)
    Turn.objects.filter(pk=first.pk).update(status=Turn.DONE, finished_at=timezone.now())
    status = ts.resolve(Turn.objects.get(pk=first.pk))
    assert (status.midturn, status.midturn_blocked) == (False, None)


def test_a_claimed_rider_is_listed_as_in_the_turn_not_queued():
    """The queue row says it went into the agent's current turn rather than
    "queued", until the transcript row carrying it lands."""
    from apps.canopy_sessions import send as session_send
    from apps.canopy_sessions import services as chat

    user, _ws, _agent, session, runner = _ctx()
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    _m, first = session_send.send_message(session=session, text="go", user=user, client_id="c1")
    assert claim.claim_next_turn(runner) == first
    session_send.send_message(session=session, text="skip the checks", user=user, client_id="c2")
    states = {q["text"]: q["state"] for q in session_send.queued_messages(session)}
    assert states["skip the checks"] == "queued"
    assert claim.claim_next_turn(runner).rides_turn_id == first.pk
    states = {q["text"]: q["state"] for q in session_send.queued_messages(session)}
    assert states["skip the checks"] == "midturn"
    assert states["go"] == "delivering"


def test_a_follow_up_the_agent_took_mid_turn_lands_and_leaves_the_queue():
    """Claude Code records a message it takes mid-turn only as a `queued_command`
    attachment. The runner ships that as the user row; the server links it to
    its Turn, so the send stops reading "sending to agent…" and stays in the
    conversation after the turn ends."""
    from canopy_transcript import conversational_messages

    from apps.canopy_sessions import send as session_send
    from apps.canopy_sessions import services as chat
    from apps.canopy_sessions.models import Message
    from apps.canopy_sessions.transcript_ingest import persist_transcript_rows

    user, _ws, _agent, session, runner = _ctx()
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    _m, first = session_send.send_message(session=session, text="go", user=user, client_id="c1")
    assert claim.claim_next_turn(runner) == first
    _m, follow = session_send.send_message(session=session, text="owner, not admin", user=user,
                                           client_id="c2")
    assert claim.claim_next_turn(runner) == follow
    records = [
        {"type": "user", "promptSource": "typed", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "on it"}]}},
        {"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": "owner, not admin", "commandMode": "prompt",
            "origin": {"kind": "human"}, "humanTurn": True}},
    ]
    persist_transcript_rows(session, conversational_messages(records, -1))

    assert session_send.queued_messages(session) == []
    row = Message.objects.get(session=session, source_turn_id=follow.pk)
    assert (row.role, row.plaintext, row.author["user_id"]) == (Message.USER, "owner, not admin", user.pk)
