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


# --- ext_ids made safe before the case-insensitive unique constraint ----------


def test_safe_ext_ids_leaves_clean_ids_alone():
    assert mig.safe_ext_ids([(1, 7, "T1"), (2, 7, "T2"), (3, 8, "T1")]) == {}


def test_safe_ext_ids_renames_later_case_collisions():
    # Oldest keeps it; later ones get -2, -3 … unique ignoring case.
    rows = [(1, 7, "T1"), (2, 7, "t1"), (3, 7, "T1")]
    assert mig.safe_ext_ids(rows) == {2: "t1-2", 3: "T1-3"}


def test_safe_ext_ids_never_takes_an_id_a_later_row_already_has():
    rows = [(1, 7, "T2"), (2, 7, "t2"), (3, 7, "T2-2")]
    assert mig.safe_ext_ids(rows) == {2: "t2-3"}


def test_safe_ext_ids_replaces_slashes_and_resolves_what_that_collides_with():
    rows = [(1, 7, "a/b"), (2, 7, "A-B"), (3, 7, "x/y/z")]
    assert mig.safe_ext_ids(rows) == {1: "a-b", 2: "A-B-2", 3: "x-y-z"}


def test_safe_ext_ids_is_per_agent():
    assert mig.safe_ext_ids([(1, 7, "T1"), (2, 8, "t1")]) == {}


def test_safe_ext_ids_keeps_within_the_column_length():
    long = "x" * 64
    out = mig.safe_ext_ids([(1, 7, long), (2, 7, long.upper())])
    assert out == {2: "X" * 62 + "-2"} and len(out[2]) == 64
