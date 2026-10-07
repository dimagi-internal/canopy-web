"""agents.0038 moves tasks and board commands to their final shape."""
from __future__ import annotations

import importlib

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.workspaces.models import Workspace

mig = importlib.import_module("apps.agents.migrations.0038_projects_and_tasks_final_shape")

pytestmark = pytest.mark.django_db


def test_command_kinds_map_to_actions():
    assert mig.ACTION_FOR_KIND == {
        "accept": "approve", "decline": "decline", "comment": "reply",
        "dispatch": "dispatch", "done": "done",
    }
    # edit / reassign were field edits — they have no action and are deleted.
    assert "edit" not in mig.ACTION_FOR_KIND and "reassign" not in mig.ACTION_FOR_KIND


def test_comment_text_comes_from_note_or_reason():
    assert mig.comment_from_payload({"note": "looks good"}) == "looks good"
    assert mig.comment_from_payload({"reason": "dup"}) == "dup"
    assert mig.comment_from_payload({}) == ""


def test_final_task_shape_has_no_item_fields():
    names = {f.name for f in AgentTask._meta.get_fields()}
    assert {"ask_kind", "ask_body", "ask_closed_at", "on_approve", "dispatched_at"} <= names
    assert not names & {"uuid", "decision", "comment", "decided_by", "decided_by_user",
                        "decided_at", "ask_dismissed", "dispatch"}


def test_open_ask_is_keyed_on_closed_at():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=u)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    t = AgentTask.objects.create(agent=agent, ext_id="T1", title="x", ask_kind="review")
    assert t.ask_is_open
    t.ask_closed_at = timezone.now()
    assert not t.ask_is_open
    assert AgentTaskAction.APPROVE == "approve"
