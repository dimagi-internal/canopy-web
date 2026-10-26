"""What holds a turn or a conversation to an agent CLI, and what lets go
(`apps/harness/clis.py`): the claim's pick is not a hold, a requeue picks again,
a conversation is held once a session exists in a CLI's store, an agent that
drops a CLI stops the work on it, and a CLI nothing has cannot be chosen."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import placement
from apps.canopy_sessions.models import Session
from apps.harness import claim, clis, fleets, session_sync
from apps.harness import initiator as _initiator
from apps.harness import services as harness
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

_BY_CANOPY = _initiator.system(via="test")
CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}


def _ctx(*, agent_clis=("codex", "claude-code"), report=None):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, clis=list(agent_clis),
                                 owner=user)
    session = Session.objects.create(workspace=ws, created_by=user, agent=agent, title="t")
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, host="h", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "midturn": 1,
                      "clis": report or {"codex": CONFINING, "claude-code": CONFINING}})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return user, agent, session, runner


def _send(session, key):
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key=key, prompt="hi")
    return turn


# ---- bug: a failed start held the conversation to the CLI that failed ----
def test_a_turn_that_never_started_leaves_its_conversation_free():
    _u, agent, session, runner = _ctx()
    turn = _send(session, "k1")
    assert claim.claim_next_turn(runner).cli == "codex"
    # Codex never started a session: the runner fails it with no session key.
    harness.finish_turn(Turn.objects.get(pk=turn.pk), status=Turn.FAILED, result_note="codex: auth")
    turn.refresh_from_db()
    session.refresh_from_db()
    assert turn.status == Turn.QUEUED
    assert (turn.cli, session.cli) == ("", "")
    # Codex is still the agent's first choice; but with Codex gone from the box the
    # turn now runs on Claude Code, as the agent allows.
    runner.capabilities = {**runner.capabilities, "clis": {"claude-code": CONFINING}}
    runner.save(update_fields=["capabilities"])
    assert claim.claim_next_turn(runner).cli == "claude-code"


def test_a_lost_fleet_turn_is_picked_again():
    _u, agent, session, runner = _ctx()
    turn = _send(session, "k1")
    claim.claim_next_turn(runner)
    assert fleets.requeue_lost(Turn.objects.get(pk=turn.pk))
    turn.refresh_from_db()
    assert (turn.status, turn.cli) == (Turn.QUEUED, "")


def test_a_requested_cli_survives_a_requeue():
    _u, agent, _session, runner = _ctx()
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=agent, requested_cli="claude-code",
                                   origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="s1")
    assert claim.claim_next_turn(runner).cli == "claude-code"
    harness.finish_turn(Turn.objects.get(pk=turn.pk), status=Turn.FAILED, result_note="x")
    turn.refresh_from_db()
    assert (turn.requested_cli, turn.cli) == ("claude-code", "")
    assert clis.allowed(turn) == ["claude-code"]


def test_a_conversation_is_held_when_its_session_is_reported():
    _u, agent, session, runner = _ctx()
    turn = _send(session, "k1")
    claim.claim_next_turn(runner)
    session.refresh_from_db()
    assert session.cli == ""
    session_sync.stamp_turn_session(turn.pk, runner, "thread-1")
    session.refresh_from_db()
    assert session.cli == "codex"
    # First write wins: a later report never moves it.
    clis.pin_conversation([session.pk], "claude-code")
    session.refresh_from_db()
    assert session.cli == "codex"


def test_a_finished_turn_holds_its_conversation():
    _u, agent, session, runner = _ctx()
    turn = _send(session, "k1")
    claim.claim_next_turn(runner)
    harness.finish_turn(Turn.objects.get(pk=turn.pk), status=Turn.DONE)
    session.refresh_from_db()
    assert session.cli == "codex"


# ---- bug: dropping a CLI from an agent did not stop work on it ----
def test_dropping_a_cli_stops_its_conversations(client):
    user, agent, session, runner = _ctx()
    Session.objects.filter(pk=session.pk).update(cli="codex")
    session.refresh_from_db()
    Agent.objects.filter(pk=agent.pk).update(clis=["claude-code"])
    session.agent.refresh_from_db()
    assert clis.conversation_refusal(session).startswith("this conversation runs on Codex")
    turn = _send(session, "k1")
    assert clis.allowed(turn) == []
    assert claim.claim_next_turn(runner) is None
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(user)}
    assert "no longer set up for Codex" in rows[str(turn.pk)]["reason"]
    client.force_login(user)
    resp = client.post(f"/api/w/w1/canopy-sessions/{session.pk}/send",
                       data={"text": "more"}, content_type="application/json")
    assert resp.status_code == 422
    assert "no longer set up for Codex" in resp.content.decode()


def test_a_cli_a_schedule_runs_on_cannot_be_dropped(client):
    user, agent, _session, _runner = _ctx()
    AgentSchedule.objects.create(agent=agent, name="Weekly post", prompt="/ace:turn",
                                 cron="0 9 * * 5", cli="codex")
    client.force_login(user)
    resp = client.patch("/api/w/w1/agents/ace/clis", data={"clis": ["claude-code"]},
                        content_type="application/json")
    assert resp.status_code == 422
    assert "Weekly post" in resp.content.decode()


# ---- bug: Codex could be chosen with nothing to run it ----
def test_a_cli_no_runner_has_cannot_be_added_or_chosen(client):
    user, agent, session, runner = _ctx(agent_clis=("claude-code",),
                                        report={"claude-code": CONFINING})
    client.force_login(user)
    resp = client.patch("/api/w/w1/agents/ace/clis", data={"clis": ["claude-code", "codex"]},
                        content_type="application/json")
    assert resp.status_code == 422
    assert "no runner serving this workspace has Codex" in resp.content.decode()
    # An agent declared on Codex in its repo still cannot have Codex chosen for it.
    Agent.objects.filter(pk=agent.pk).update(clis=["claude-code", "codex"])
    agent.refresh_from_db()
    assert clis.refusal(agent, "codex") == clis.not_available("codex")
    # Once a box has it, signed in or not, it can.
    runner.capabilities = {**runner.capabilities,
                           "clis": {"claude-code": CONFINING, "codex": {"signed_in": False}}}
    runner.save(update_fields=["capabilities"])
    assert clis.refusal(agent, "codex") is None


# ---- bug: fork and transfer ignored the CLI ----
def test_a_box_without_the_conversations_cli_is_no_placement():
    _u, agent, session, runner = _ctx(report={"claude-code": CONFINING})
    Session.objects.filter(pk=session.pk).update(cli="codex")
    session.refresh_from_db()
    assert placement._placeable_runner(session, runner.pk) is None
    err = placement._placement_refused(session, runner.pk, "unknown runner")
    assert "runs on Codex" in str(err)


# ---- the advice a waiting turn gives ----
def test_a_caller_turn_on_a_box_that_cannot_confine_says_so():
    user, agent, _session, runner = _ctx(
        agent_clis=("codex",), report={"codex": {"signed_in": True}, "claude-code": CONFINING})
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=agent,
                                   origin=Turn.ORIGIN_API, idempotency_key="c1")
    turn.capability = "ask"
    assert clis.blocker(turn, [runner]).startswith("it is a caller's turn")


def test_a_box_naming_only_unknown_clis_says_so():
    _u, _agent, _session, runner = _ctx()
    harness.heartbeat(runner, active_turn_ids=[], clis={"gemini": {"signed_in": True}})
    runner.refresh_from_db()
    assert runner.capabilities["clis"] == {}
    assert runner.capabilities["clis_unknown"] == ["gemini"]
    from apps.harness.schemas import RunnerOut
    out = RunnerOut.resolve_agent_clis(runner)
    assert out["gemini"].known is False
