"""The ask a task carries — the supervisor's queue. Dual of harness.Turn.

Was its own `Item` model until 2026-09-19; the guarantees are unchanged.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.agents.models import AgentTask
from apps.harness.models import Turn
from apps.workspaces import services as wsvc


_EXT = iter(range(1, 10_000))


def _ext() -> str:
    """A unique `ext_id` per task these tests create. Tasks are board cards and
    carry one; an ask raised through the service gets it for free."""
    return f"T{next(_EXT)}"


pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(default_workspace):
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def test_item_defaults_to_open_review_with_no_decision(agent):
    item = AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="discard 81 junk emails",
        idempotency_key="k1",
    )
    assert item.ask_state == "open"
    assert item.decision == ""
    assert item.dispatch == []
    assert item.raised_by is None


def test_idempotency_key_is_unique(agent):
    AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="a", idempotency_key="dupe")
    with pytest.raises(IntegrityError):
        AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="b", idempotency_key="dupe")


def test_turn_records_the_item_it_came_from(agent):
    item = AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="a", idempotency_key="k2")
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1", raised_from_task=item,
    )
    assert list(item.dispatched_turns.all()) == [turn]


def test_item_records_the_turn_that_raised_it(agent):
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t2")
    item = AgentTask.objects.create(agent=agent, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="a", idempotency_key="k3", raised_by=turn,
    )
    assert list(turn.raised_tasks.all()) == [item]
