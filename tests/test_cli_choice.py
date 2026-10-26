"""A conversation, a turn or a schedule picks its agent CLI; the agent's list is
only which CLIs it is set up for, its first the default
(spec 2026-10-26-multi-cli-agents §1)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import claim, clis
from apps.harness import initiator as _initiator
from apps.harness import schedule_turns
from apps.harness import services as harness
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

_BY_CANOPY = _initiator.system(via="test")
CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}
BOTH = {"claude-code": CONFINING, "codex": CONFINING}


def _ctx(agent_clis=("claude-code", "codex")):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, clis=list(agent_clis),
                                 owner=user)
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, host="h", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "midturn": 1, "clis": BOTH})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return user, ws, agent, runner


# ---- the rule ----
def test_refusal_names_what_the_agent_is_not_set_up_for():
    _user, _ws, agent, _runner = _ctx(agent_clis=("claude-code",))
    assert clis.refusal(agent, "") is None
    assert clis.refusal(agent, "claude-code") is None
    assert clis.refusal(agent, "codex") == (
        "Ace isn't set up to run on Codex: add Codex under its Settings → Agent CLIs first")
    assert clis.refusal(None, "codex") == "a chat without an agent runs on Claude Code only"


def test_a_turns_own_cli_beats_the_agents_default():
    _user, _ws, agent, runner = _ctx()
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=agent, cli="codex",
                                   origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="k",
                                   prompt="/ace:turn")
    assert turn.cli == "codex"
    assert clis.allowed(turn) == ["codex"]
    assert claim.claim_next_turn(runner).cli == "codex"


def test_without_a_choice_the_agents_first_cli_runs():
    _user, _ws, agent, runner = _ctx()
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=agent,
                                   origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="k")
    assert claim.claim_next_turn(runner) == turn
    turn.refresh_from_db()
    assert turn.cli == "claude-code"


# ---- a conversation ----
def test_a_chat_started_on_codex_stays_on_codex(client):
    user, ws, agent, runner = _ctx()
    client.force_login(user)
    resp = client.post("/api/w/w1/canopy-sessions/", data={"agent_slug": "ace", "cli": "codex"},
                       content_type="application/json")
    assert resp.status_code in (200, 201), resp.content
    body = resp.json()
    assert body["cli"] == "codex"
    session = Session.objects.get(pk=body["id"])
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key="k1", prompt="draft the post")
    assert claim.claim_next_turn(runner).cli == "codex"
    turn.refresh_from_db()
    assert turn.cli == "codex"


def test_a_chat_on_a_cli_the_agent_lacks_is_refused(client):
    user, ws, agent, _runner = _ctx(agent_clis=("claude-code",))
    client.force_login(user)
    resp = client.post("/api/w/w1/canopy-sessions/", data={"agent_slug": "ace", "cli": "codex"},
                       content_type="application/json")
    assert resp.status_code == 422
    assert "isn't set up to run on Codex" in resp.content.decode()
    assert not Session.objects.exists()


def test_a_chat_without_a_choice_is_decided_by_its_first_turn(client):
    user, ws, agent, _runner = _ctx(agent_clis=("codex", "claude-code"))
    client.force_login(user)
    resp = client.post("/api/w/w1/canopy-sessions/", data={"agent_slug": "ace"},
                       content_type="application/json")
    assert resp.status_code in (200, 201), resp.content
    assert resp.json()["cli"] == ""


# ---- an API turn ----
def test_an_api_turn_may_name_its_cli(client):
    user, ws, agent, _runner = _ctx()
    client.force_login(user)
    resp = client.post("/api/w/w1/harness/turns/", data={
        "agent_slug": "ace", "origin": "api", "idempotency_key": "x", "cli": "codex"},
        content_type="application/json")
    assert resp.status_code in (200, 201), resp.content
    assert Turn.objects.get(idempotency_key="x").cli == "codex"
    resp = client.post("/api/w/w1/harness/turns/", data={
        "project": "canopy-web", "origin": "api", "idempotency_key": "y", "cli": "codex"},
        content_type="application/json")
    assert resp.status_code == 422


# ---- a schedule ----
def test_a_schedule_fires_on_its_cli(client):
    user, ws, agent, runner = _ctx()
    client.force_login(user)
    resp = client.post("/api/w/w1/agents/ace/schedules/", data={
        "name": "Weekly post", "prompt": "/ace:turn", "cron": "0 9 * * 5", "cli": "codex"},
        content_type="application/json")
    assert resp.status_code in (200, 201), resp.content
    assert resp.json()["cli"] == "codex"
    schedule = AgentSchedule.objects.get(name="Weekly post")
    turn, _ = schedule_turns.fire_schedule(schedule, timezone.now().replace(microsecond=0))
    assert turn.cli == "codex"
    turn = schedule_turns.run_schedule_now(schedule)
    assert turn.cli == "codex"


def test_a_schedule_on_a_cli_the_agent_lacks_is_refused(client):
    user, ws, agent, _runner = _ctx(agent_clis=("claude-code",))
    client.force_login(user)
    resp = client.post("/api/w/w1/agents/ace/schedules/", data={
        "name": "Weekly post", "prompt": "/ace:turn", "cron": "0 9 * * 5", "cli": "codex"},
        content_type="application/json")
    assert resp.status_code == 422
