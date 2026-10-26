"""The claim picks a turn's agent CLI with `clis.choose`, records it on the turn and
its conversation, and sends it to the runner; coverage and `turn_reach` agree
(spec 2026-10-26-multi-cli-agents §1)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import claim
from apps.harness import initiator as _initiator
from apps.harness import services as harness
from apps.harness.claiming import claim_payload
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

_BY_CANOPY = _initiator.system(via="test")
CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}


def _ctx(*, agent_clis=("codex",), report=None, models=None):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, clis=list(agent_clis),
                                 cli_models=models or {})
    session = Session.objects.create(workspace=ws, created_by=user, agent=agent, title="t")
    caps = {"sessions": True, "midturn": 1}
    if report is not None:
        caps["clis"] = report
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, host="h", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities=caps)
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return agent, session, runner


def _send(session, key, prompt="hi"):
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key=key, prompt=prompt)
    return turn


def _agent_turn(agent, key="a1"):
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=agent,
                                   origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key=key,
                                   prompt=f"/{agent.slug}:turn")
    return turn


def test_codex_agent_claims_on_codex_box_and_stamps():
    agent, session, runner = _ctx(report={"codex": CONFINING, "claude-code": CONFINING},
                                  models={"codex": "gpt-6.1-sol"})
    turn = _send(session, "k1")
    claimed = claim.claim_next_turn(runner)
    assert claimed == turn
    assert claimed.cli == "codex"
    session.refresh_from_db()
    assert session.cli == "codex"
    payload = claim_payload(claimed)
    assert payload["cli"] == "codex"
    assert payload["cli_model"] == "gpt-6.1-sol"
    event = claimed.events.filter(kind="status").order_by("seq").last()
    assert event.payload["cli"] == "codex"


def test_codex_agent_waits_on_claude_only_box():
    agent, session, runner = _ctx(report={"claude-code": CONFINING})
    _agent_turn(agent)
    assert claim.claim_next_turn(runner) is None


def test_legacy_box_claims_as_before():
    agent, session, runner = _ctx(agent_clis=("claude-code",), report=None)
    turn = _agent_turn(agent)
    claimed = claim.claim_next_turn(runner)
    assert claimed == turn
    assert claimed.cli == "claude-code"
    assert claim_payload(claimed)["cli_model"] == ""


def test_confined_turn_runs_only_where_its_cli_confines():
    agent, session, runner = _ctx(agent_clis=("claude-code",),
                                  report={"claude-code": {"signed_in": True},
                                          "codex": CONFINING})
    turn = _agent_turn(agent)
    Turn.objects.filter(pk=turn.pk).update(capability="ask")
    assert claim.claim_next_turn(runner) is None
    agent.clis = ["claude-code", "codex"]
    agent.save(update_fields=["clis"])
    claimed = claim.claim_next_turn(runner)
    assert claimed == turn and claimed.cli == "codex"


def test_pin_does_not_bypass_cli():
    agent, session, runner = _ctx(report={"claude-code": CONFINING})
    turn = _agent_turn(agent)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=runner)
    assert claim.claim_next_turn(runner) is None


def test_box_signed_out_everywhere_claims_nothing():
    agent, session, runner = _ctx(agent_clis=("claude-code",),
                                  report={"claude-code": {"signed_in": False},
                                          "codex": {"signed_in": False}})
    _agent_turn(agent)
    assert claim.claim_next_turn(runner) is None


def test_rider_runs_on_session_cli():
    agent, session, runner = _ctx(agent_clis=("claude-code", "codex"),
                                  report={"claude-code": CONFINING, "codex": CONFINING})
    Session.objects.filter(pk=session.pk).update(cli="codex")
    first = _send(session, "k1")
    assert claim.claim_next_turn(runner) == first
    follow_up = _send(session, "k2", "and this")
    claimed = claim.claim_next_turn(runner)
    assert claimed == follow_up
    assert claimed.rides_turn_id == first.pk
    assert claimed.cli == "codex"
