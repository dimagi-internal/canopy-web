import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.harness.dispatch import dispatch
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    task = AgentTask.objects.create(agent=agent, ext_id="T1", title="Send EOI",
                                    ask_kind="review", on_approve=[{"prompt": "/eva:turn send it"}])
    action = AgentTaskAction(agent=agent, task=task, action="approve",
                             comment="yes, cc Neal", by="jj@dimagi.com", by_user=u)
    return u, ws, agent, task, action


def test_dispatch_carries_the_reply_and_is_idempotent(world):
    _u, ws, _agent, task, action = world
    first = dispatch(task, action=action, actor_workspace_ids={ws.pk})
    again = dispatch(task, action=action, actor_workspace_ids={ws.pk})
    assert len(first) == 1 and first[0].pk == again[0].pk
    assert "yes, cc Neal" in first[0].prompt
    assert first[0].raised_from_task_id == task.pk
    assert first[0].idempotency_key == f"task-{task.pk}-0"
    assert first[0].origin_ref["task_title"] == "Send EOI"


def test_unknown_target_raises(world):
    _u, ws, _agent, task, action = world
    task.on_approve = [{"prompt": "x", "target_agent": "nobody"}]
    with pytest.raises(ValueError, match="unknown target_agent"):
        dispatch(task, action=action, actor_workspace_ids={ws.pk})


def test_cross_tenant_target_raises(world):
    u, ws, _agent, task, action = world
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=u)
    Agent.objects.create(slug="hal", name="Hal", workspace=other, owner=u)
    task.on_approve = [{"prompt": "x", "target_agent": "hal"}]
    with pytest.raises(ValueError, match="not a member"):
        dispatch(task, action=action, actor_workspace_ids={ws.pk})
