"""`clis.choose`: the one rule that picks the CLI a turn runs on
(spec 2026-10-26-multi-cli-agents §1)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import clis
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()

CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}


@pytest.fixture
def ws():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    return Workspace.objects.create(slug="w", display_name="W", created_by=owner,
                                    access_request_domains=[])


def _runner(report):
    return Runner.objects.create(name="r", kind=Runner.CLOUD, status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), capabilities={"clis": report})


def _agent_turn(ws, agent_clis, **kw):
    agent = Agent.objects.create(slug="a", name="A", workspace=ws, clis=agent_clis)
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                               idempotency_key="k", **kw)


def test_agent_order_wins(ws):
    turn = _agent_turn(ws, ["codex", "claude-code"])
    assert clis.choose(turn, _runner({"claude-code": CONFINING, "codex": CONFINING})) == "codex"


def test_falls_to_next_signed_in(ws):
    turn = _agent_turn(ws, ["codex", "claude-code"])
    runner = _runner({"claude-code": CONFINING, "codex": {"signed_in": False}})
    assert clis.choose(turn, runner) == "claude-code"


def test_session_cli_is_sticky(ws):
    agent = Agent.objects.create(slug="a", name="A", workspace=ws, clis=["claude-code", "codex"])
    session = Session.objects.create(agent=agent, workspace=ws, cli="codex")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                               idempotency_key="k")
    runner = _runner({"claude-code": CONFINING, "codex": {"signed_in": False}})
    assert clis.choose(turn, runner) is None


def test_confined_turn_needs_confining_cli(ws):
    turn = _agent_turn(ws, ["codex", "claude-code"], capability="ask")
    runner = _runner({"claude-code": CONFINING, "codex": {"signed_in": True, "profiles": 0}})
    assert clis.choose(turn, runner) == "claude-code"
    runner = _runner({"claude-code": {"signed_in": True, "profiles": 3, "envelope": 1},
                      "codex": {"signed_in": True}})
    assert clis.choose(turn, runner) is None


def test_sensitive_turn_only_on_claude_code(ws, monkeypatch):
    turn = _agent_turn(ws, ["codex"])
    monkeypatch.setattr(rr, "requirements_of", lambda t: frozenset({rr.SENSITIVE_DATA}))
    assert clis.choose(turn, _runner({"codex": CONFINING})) is None
    turn.agent.clis = ["codex", "claude-code"]
    assert clis.choose(turn, _runner({"codex": CONFINING, "claude-code": CONFINING})) == "claude-code"


def test_repo_turn_defaults_to_claude_code(ws):
    turn = Turn.objects.create(project="canopy-web", workspace=ws,
                               origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="k")
    assert clis.allowed(turn) == ["claude-code"]
    assert clis.choose(turn, _runner({"codex": CONFINING})) is None


def test_unknown_or_empty_agent_list_is_default(ws):
    turn = _agent_turn(ws, ["gpt"])
    assert clis.allowed(turn) == ["claude-code"]


def test_ignore_signed_in_counterfactual(ws):
    turn = _agent_turn(ws, ["codex", "claude-code"])
    runner = _runner({"claude-code": {"signed_in": False}, "codex": {"signed_in": False}})
    assert clis.choose(turn, runner) is None
    assert clis.choose(turn, runner, ignore_signed_in=True) == "codex"
