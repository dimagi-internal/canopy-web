"""Who a card is sent to: the people accountable for the decision, never everyone.

In order, the first rule that names anyone wins:
  1. the person the task is parked on or assigned to;
  2. the project's owner and the check-in lead (who may decide its proposals);
  3. the owner of the agent that raised it;
  4. the workspace's owners.
An agent's own login is never a decider. Anyone else in the workspace can still open the
card and decide it (the same tier as the board's Approve button) — they are just not pushed.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.agents.models import Agent, AgentTask
from apps.workspaces import services as wsvc

MAX_DECIDERS = 5


def _people(ids) -> list:
    users = list(get_user_model().objects.filter(pk__in=[i for i in ids if i], is_active=True).order_by("pk"))
    bots = set(Agent.objects.filter(user_id__in=[u.pk for u in users]).values_list("user_id", flat=True))
    return [u for u in users if u.pk not in bots]


def for_task(task: AgentTask) -> list:
    from apps.agents import proposals

    named = _people([task.waiting_on_user_id, task.assignee_user_id])
    if named:
        return named[:MAX_DECIDERS]
    project_ids: list = []
    if task.project_id is not None:
        project_ids = [task.project.owner_user_id, *sorted(proposals._checkin_leads(task))]
    people = _people(project_ids)
    if people:
        return people[:MAX_DECIDERS]
    agent = task.agent or task.created_by_agent
    people = _people([agent.owner_id] if agent is not None else [])
    if people:
        return people
    return _people(sorted(wsvc.owner_user_ids(task.workspace)))[:MAX_DECIDERS]
