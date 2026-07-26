"""Model-level invariants for the agent-execution harness."""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, Turn, TurnEvent
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    # Homed: Agent.workspace is NOT NULL (agents/0013).
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=a_workspace())


def _runner(**kw):
    defaults = dict(name="jj-mbp", kind=Runner.EMDASH, capabilities={"agents": ["echo"]})
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def test_turn_defaults_to_queued():
    t = Turn.objects.create(agent=_agent(), origin=Turn.ORIGIN_BOARD, idempotency_key="k1")
    assert t.status == Turn.QUEUED
    assert t.routing == Turn.PREFER_LOCAL


def test_queued_turns_stack_freely():
    a = _agent()
    Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k1")
    Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k2")  # no raise
    assert Turn.objects.filter(agent=a, status=Turn.QUEUED).count() == 2


def test_one_executing_turn_per_agent():
    a = _agent()
    Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k1", status=Turn.CLAIMED)
    with pytest.raises(IntegrityError):
        Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k2", status=Turn.RUNNING)


def test_terminal_turn_frees_the_execution_slot():
    a = _agent()
    t = Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k1", status=Turn.RUNNING)
    t.status = Turn.DONE
    t.save()
    Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k2", status=Turn.CLAIMED)  # no raise


def test_idempotency_key_unique():
    a = _agent()
    t = Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k1")
    t.status = Turn.DONE
    t.save()
    with pytest.raises(IntegrityError):
        Turn.objects.create(agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="k1")


def test_turn_event_seq_unique_per_turn():
    t = Turn.objects.create(agent=_agent(), origin=Turn.ORIGIN_BOARD, idempotency_key="k1")
    TurnEvent.objects.create(turn=t, seq=1, kind="status", payload={"s": "claimed"})
    with pytest.raises(IntegrityError):
        TurnEvent.objects.create(turn=t, seq=1, kind="status", payload={})


def test_finish_turn_accepts_missed():
    """MISSED is a terminal status distinct from LOST (infra failure)."""
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=a_workspace())
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_CRON, idempotency_key="k1", status=Turn.RUNNING
    )
    out = services.finish_turn(turn, status=Turn.MISSED, result_note="superseded")

    assert out.status == Turn.MISSED
    assert Turn.MISSED in Turn.TERMINAL
    assert out.finished_at is not None
    assert out.result_note == "superseded"


def test_runner_workspace_defaults_to_null_and_accepts_a_slug():
    """Nullable for migration safety — existing runners predate tenancy. The API
    assigns one at pairing (Task 2 step 5); the model must permit both."""
    bare = Runner.objects.create(name="legacy", kind=Runner.EMDASH)
    assert bare.workspace_id is None

    from django.contrib.auth.models import User
    from apps.workspaces.models import Workspace

    owner = User.objects.create_user("ws-owner", "ws-owner@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    homed = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, workspace=ws)
    assert homed.workspace_id == "canopy"  # PK is the slug, not an int
