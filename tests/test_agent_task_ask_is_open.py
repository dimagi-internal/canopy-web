"""An agent task's ask is open until it is closed — keyed on `ask_closed_at`."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def test_open_ask_is_keyed_on_closed_at():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=u)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    t = AgentTask.objects.create(agent=agent, ext_id="T1", title="x", ask_kind="review")
    assert t.ask_is_open
    t.ask_closed_at = timezone.now()
    assert not t.ask_is_open
    assert AgentTaskAction.APPROVE == "approve"
