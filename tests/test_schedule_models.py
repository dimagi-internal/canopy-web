"""Model-level tests for AgentSchedule — the recurring-turn declaration."""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn

pytestmark = pytest.mark.django_db


@pytest.fixture()
def agent(default_workspace):
    return Agent.objects.create(slug="eva", name="Eva", workspace=default_workspace)


def test_schedule_defaults(agent):
    s = AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="/eva:goal-review",
        cron="0 9 1 * *", timezone="America/New_York",
    )
    assert s.enabled is True
    assert s.routing == Turn.PREFER_LOCAL
    assert s.grace_minutes == 120
    assert s.notify == ["inbox"]
    assert s.last_slot is None
    assert s.agent_slug == "eva"
    assert isinstance(s.pk, int)  # NeedsYouItem.ref_id is typed int


def test_schedule_name_unique_per_agent(agent):
    AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="p", cron="0 9 1 * *",
        timezone="UTC",
    )
    with pytest.raises(IntegrityError):
        AgentSchedule.objects.create(
            agent=agent, name="Goal review", prompt="p2", cron="0 9 2 * *",
            timezone="UTC",
        )
