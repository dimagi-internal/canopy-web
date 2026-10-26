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
    # Held to Codex only once the box reports a session in it, not at the claim.
    session.refresh_from_db()
    assert session.cli == ""
    from apps.harness import session_sync
    assert session_sync.stamp_turn_session(claimed.pk, runner, "thread-1")
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


# ---- coverage and reach agree with the claim (Task 5) ----
def test_reach_signed_out_codex():
    agent, session, runner = _ctx(agent_clis=("claude-code", "codex"),
                                  report={"claude-code": CONFINING, "codex": {"signed_in": False}})
    Session.objects.filter(pk=session.pk).update(cli="codex")
    turn = _send(session, "k1")
    reach = claim.turn_reach(turn)
    assert reach.kind == claim.SIGNED_OUT
    assert [r.pk for r in reach.runners] == [runner.pk]
    assert reach.cli == "codex"
    assert reach.reason == "no runner has Codex signed in"


def test_reach_live_on_codex():
    agent, session, runner = _ctx(report={"codex": CONFINING})
    turn = _agent_turn(agent)
    assert claim.turn_reach(turn).kind == claim.LIVE


def test_unclaimable_report_matches_claim_for_cli():
    agent, session, runner = _ctx(report={"claude-code": CONFINING})
    turn = _agent_turn(agent)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    stuck = {row["turn_id"]: row for row in claim.unclaimable_queued_turns(runner.owner)}
    assert stuck[str(turn.pk)]["reason"].startswith("it runs on Codex, and none of those runners")
    assert claim.turn_reach(turn).kind == claim.UNROUTED
    runner.capabilities = {**runner.capabilities, "clis": {"claude-code": CONFINING,
                                                           "codex": CONFINING}}
    runner.save(update_fields=["capabilities"])
    assert str(turn.pk) not in {row["turn_id"] for row in claim.unclaimable_queued_turns(runner.owner)}
    assert claim.claim_next_turn(runner) == turn


# ---- review fixes ----
def test_a_conversation_from_before_clis_stays_on_claude_code():
    """Every conversation before this change ran on Claude Code; its history is there."""
    import importlib

    agent, session, runner = _ctx(agent_clis=("codex", "claude-code"),
                                  report={"claude-code": CONFINING, "codex": CONFINING})
    Session.objects.filter(pk=session.pk).update(cli="", cli_session_id="abc")
    mig = importlib.import_module("apps.canopy_sessions.migrations.0060_backfill_session_cli")
    from django.apps import apps as global_apps
    mig.backfill(global_apps, None)
    session.refresh_from_db()
    assert session.cli == "claude-code"
    _send(session, "k1")
    assert claim.claim_next_turn(runner).cli == "claude-code"


def test_a_rider_never_switches_the_running_turns_cli():
    agent, session, runner = _ctx(agent_clis=("codex", "claude-code"),
                                  report={"claude-code": CONFINING, "codex": CONFINING})
    first = _send(session, "k1")
    assert claim.claim_next_turn(runner) == first
    # Claimed by code from before CLIs: nothing stamped on it or its session.
    Turn.objects.filter(pk=first.pk).update(cli="")
    Session.objects.filter(pk=session.pk).update(cli="")
    _send(session, "k2", "and this")
    # Codex is preferred, but the running turn is Claude Code's: it waits, never rides
    # into a live Claude session as a Codex turn.
    assert claim.claim_next_turn(runner) is None


def test_an_agent_turns_session_keeps_its_cli():
    from apps.harness import session_sync

    agent, _session, runner = _ctx(report={"codex": CONFINING, "claude-code": CONFINING})
    turn = _agent_turn(agent)
    claimed = claim.claim_next_turn(runner)
    assert claimed == turn and claimed.cli == "codex"
    linked = Session.objects.create(workspace=agent.workspace, agent=agent, title="run")
    session_sync.link_turn_session(claimed, linked)
    linked.refresh_from_db()
    assert linked.cli == "codex"


def test_a_codex_conversation_is_not_signed_out_by_claudes_login():
    from canopy_transcript import SIGNED_OUT_NOTE

    from apps.canopy_sessions import api as sessions_api
    from apps.canopy_sessions.models import RunnerBinding

    agent, session, runner = _ctx(report={"codex": CONFINING, "claude-code": {"signed_in": False}})
    Runner.objects.filter(pk=runner.pk).update(ready=False, ready_note=SIGNED_OUT_NOTE)
    RunnerBinding.objects.create(session=session, runner=runner, thread_key="tk")
    Session.objects.filter(pk=session.pk).update(cli="codex")
    session.refresh_from_db()
    assert sessions_api._out(session)["runner_signed_out"] is False
    Session.objects.filter(pk=session.pk).update(cli="claude-code")
    session.refresh_from_db()
    assert sessions_api._out(session)["runner_signed_out"] is True


def test_a_fleet_member_signed_in_to_codex_is_still_wakeable():
    from canopy_transcript import SIGNED_OUT_NOTE

    from apps.harness.models import RunnerCredentialSet, RunnerFleet

    agent, session, runner = _ctx(report={"codex": CONFINING, "claude-code": {"signed_in": False}})
    cset = RunnerCredentialSet.objects.create(name="s", workspace=agent.workspace, owner=runner.owner)
    fleet = RunnerFleet.objects.create(name="f", workspace=agent.workspace, owner=runner.owner,
                                       credential_set=cset,
                                       role_arn="arn:x", role_jkt="j", region="us-east-1",
                                       group_name="g", status=RunnerFleet.ACTIVE)
    Runner.objects.filter(pk=runner.pk).update(fleet=fleet, ready=False, ready_note=SIGNED_OUT_NOTE,
                                               power=Runner.POWER_STOPPED)
    runner.refresh_from_db()
    assert runner.wakeable is True
    runner.capabilities = {**runner.capabilities, "clis": {"codex": {"signed_in": False}}}
    assert runner.wakeable is False
