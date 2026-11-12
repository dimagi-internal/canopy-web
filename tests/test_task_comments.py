"""Task comments: anyone who can read a task may comment on it, and a comment is only
words. It is not a `reply`: it answers no ask, moves nothing, queues nothing for the agent
and never starts a turn."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask, AgentTaskAction, TaskEvent
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
J = "application/json"


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="home", display_name="Home", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    viewer = User.objects.create_user("addy", "addy@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    editor = User.objects.create_user("gina", "gina@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    stranger = User.objects.create_user("x", "x@else.com", "pw")
    other = Workspace.objects.create(slug="else", display_name="Else", created_by=stranger)
    WorkspaceMembership.objects.create(user=stranger, workspace=other, role=WorkspaceMembership.OWNER)
    login = User.objects.create_user("bailey-login", "bailey@marshellis.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    bailey = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=owner, user=login)
    task = AgentTask.objects.create(workspace=ws, title="Wash the car", agent=bailey, status=AgentTask.IN_PROGRESS,
                                    ask_kind=AgentTask.ASK_QUESTION, ask_body="Which car?")
    return dict(owner=owner, ws=ws, viewer=viewer, editor=editor, stranger=stranger, login=login, bailey=bailey,
                task=task)


def _comment(c, key, body="Couldn't do it: the car wash was closed"):
    return c.post(f"/api/tasks/{key}/actions", {"action": "comment", "comment": body}, content_type=J)


def test_a_reader_comments_and_it_is_in_the_history(w):
    r = _comment(_client(w["viewer"]), w["task"].key)
    assert r.status_code == 200, r.content
    assert r.json()["action"]["action"] == "comment" and r.json()["turn_ids"] == []
    event = TaskEvent.objects.get(task=w["task"], kind=TaskEvent.COMMENT)
    assert event.by_user == w["viewer"]
    detail = _client(w["owner"]).get(f"/api/tasks/{w['task'].key}/").json()
    [shown] = [e for e in detail["events"] if e["kind"] == "comment"]
    assert shown["comment"] == "Couldn't do it: the car wash was closed" and shown["by_email"] == "addy@dimagi.com"


def test_a_comment_starts_no_turn_answers_no_ask_and_queues_nothing(w):
    for user in (w["owner"], w["editor"], w["viewer"]):
        assert _comment(_client(user), w["task"].key).status_code == 200
    task = AgentTask.objects.get(pk=w["task"].pk)
    assert not Turn.objects.exists()
    assert task.ask_is_open and task.status == AgentTask.IN_PROGRESS and task.dispatched_at is None
    assert not AgentTaskAction.objects.filter(status=AgentTaskAction.PENDING).exists()
    assert not TaskEvent.objects.filter(task=task, kind=TaskEvent.FIELD).exists()


def test_an_agents_login_comments_as_the_agent(w):
    assert _comment(_client(w["login"]), w["task"].key, "Done the inside, outside tomorrow").status_code == 200
    event = TaskEvent.objects.get(task=w["task"], kind=TaskEvent.COMMENT)
    assert event.by_agent == w["bailey"]
    assert not Turn.objects.exists()


def test_a_comment_needs_words(w):
    assert _comment(_client(w["owner"]), w["task"].key, "   ").status_code == 422


def test_a_non_reader_gets_404(w):
    assert _comment(_client(w["stranger"]), w["task"].key).status_code == 404
    assert not TaskEvent.objects.filter(kind=TaskEvent.COMMENT).exists()


def test_a_personal_projects_task_404s_to_a_workspace_editor_and_its_lead_comments(w):
    mine = AgentProject.objects.create(workspace=w["ws"], handle="mine", name="Mine", owner_user=w["owner"],
                                       visibility=AgentProject.PERSONAL)
    task = AgentTask.objects.create(workspace=w["ws"], title="Private", project=mine)
    assert _comment(_client(w["editor"]), task.key).status_code == 404
    assert _comment(_client(w["owner"]), task.key).status_code == 200


def test_a_finished_task_still_takes_comments(w):
    AgentTask.objects.filter(pk=w["task"].pk).update(status=AgentTask.DONE)
    assert _comment(_client(w["viewer"]), w["task"].key).status_code == 200
    assert AgentTask.objects.get(pk=w["task"].pk).status == AgentTask.DONE


def test_a_snoozed_task_stays_snoozed(w):
    import datetime as dt

    from django.utils import timezone

    until = timezone.now() + dt.timedelta(days=3)
    AgentTask.objects.filter(pk=w["task"].pk).update(snoozed_until=until)
    _comment(_client(w["viewer"]), w["task"].key)
    assert AgentTask.objects.get(pk=w["task"].pk).snoozed_until == until
