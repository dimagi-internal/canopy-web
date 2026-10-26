"""Which agent CLI a turn may run on, and what a runner reports it has
(spec 2026-10-26-multi-cli-agents)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import clis
from apps.harness.models import Runner, RunnerLogin, Turn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()


def _ws():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    return Workspace.objects.create(slug="w", display_name="W", created_by=owner,
                                    access_request_domains=[])


def test_new_agent_defaults_to_claude_code():
    agent = Agent.objects.create(slug="a", name="A", workspace=_ws())
    agent.refresh_from_db()
    assert agent.clis == ["claude-code"]
    assert agent.cli_models == {}


def test_turn_and_session_cli_start_blank():
    ws = _ws()
    agent = Agent.objects.create(slug="a", name="A", workspace=ws)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                               idempotency_key="k")
    session = Session.objects.create(agent=agent, workspace=ws)
    assert turn.cli == ""
    assert session.cli == ""


def test_runner_login_cli_defaults_to_claude_code():
    runner = Runner.objects.create(name="r", kind=Runner.CLOUD, last_heartbeat_at=timezone.now())
    login = RunnerLogin.objects.create(runner=runner, key="default")
    assert login.cli == clis.CLAUDE_CODE == "claude-code"
    assert clis.CLIS == ("claude-code", "codex")
    assert clis.SENSITIVE_CLIS == frozenset({"claude-code"})
