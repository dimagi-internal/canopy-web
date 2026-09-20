"""The migration that moves every `Item` onto a task.

Run against the real models rather than through the migration runner: the
function only uses fields both worlds share, and what matters here is the
MAPPING — which column becomes which, and what survives. It runs on the fleet's
live rows at deploy, where the two things that must not break are the ids
(links, Ada's stored references) and the turn→ask edge.
"""
from __future__ import annotations

import importlib

import pytest
from django.apps import apps as real_apps
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent, AgentTask
from apps.harness.models import Item, Turn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

migration = importlib.import_module("apps.agents.migrations.0030_items_become_tasks")


@pytest.fixture()
def agent():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def _item(agent, **kw):
    kw.setdefault("title", "an ask")
    kw.setdefault("kind", Item.REVIEW)
    kw.setdefault("origin", Turn.ORIGIN_API)
    kw.setdefault("idempotency_key", f"k-{kw['title']}")
    return Item.objects.create(agent=agent, **kw)


def _run():
    migration.items_to_tasks(real_apps, None)


def test_an_open_item_becomes_a_suggested_task_with_its_ask(agent):
    item = _item(agent, body="please look", batch_key="audit-1")

    _run()

    task = AgentTask.objects.get()
    assert (task.title, task.ask_kind, task.ask_body) == ("an ask", "review", "please look")
    assert task.status == AgentTask.SUGGESTED
    assert task.ask_is_open and task.batch_key == "audit-1"
    assert task.idempotency_key == item.idempotency_key


def test_the_id_survives_so_old_links_still_resolve(agent):
    """A task's uuid IS the item's id — that is what the field is for."""
    item = _item(agent)

    _run()

    assert AgentTask.objects.get().uuid == item.id


def test_the_turn_that_the_item_dispatched_still_points_at_it(agent):
    """`Turn.raised_from` pointed at the item; the edge moves to the task, or
    "what approved this work" is lost when the item table goes."""
    item = _item(agent)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API,
                               idempotency_key="t1", raised_from=item)

    _run()

    turn.refresh_from_db()
    task = AgentTask.objects.get()
    assert turn.raised_from_task_id == task.pk
    assert list(task.dispatched_turns.all()) == [turn]


@pytest.mark.parametrize(
    "state,decision,dispatched,expected_status,expected_state",
    [
        ("open", "", False, AgentTask.SUGGESTED, "open"),
        ("dismissed", "", False, AgentTask.DECLINED, "dismissed"),
        ("decided", "skip", False, AgentTask.DECLINED, "decided"),
        ("decided", "defer", False, AgentTask.SUGGESTED, "decided"),
        ("decided", "implement", True, AgentTask.IN_PROGRESS, "decided"),
        # A question is decided by its answer and never carries a verb.
        ("decided", "", True, AgentTask.IN_PROGRESS, "decided"),
    ],
)
def test_each_item_state_lands_in_the_right_column(agent, state, decision, dispatched,
                                                   expected_status, expected_state):
    _item(agent, state=state, decision=decision,
          decided_at=None if state == "open" else timezone.now(),
          dispatched_at=timezone.now() if dispatched else None)

    _run()

    task = AgentTask.objects.get()
    assert (task.status, task.ask_state) == (expected_status, expected_state)


def test_created_at_is_preserved_so_the_inbox_does_not_read_as_new(agent):
    old = timezone.now() - timezone.timedelta(days=30)
    item = _item(agent)
    Item.objects.filter(pk=item.pk).update(created_at=old)

    _run()

    assert AgentTask.objects.get().created_at == old


def test_ext_ids_continue_from_the_agents_existing_board(agent):
    """A migrated ask must not collide with a task the agent already has, nor
    with the next one it creates."""
    AgentTask.objects.create(agent=agent, ext_id="T1", title="existing work")
    Agent.objects.filter(pk=agent.pk).update(task_seq=1)
    _item(agent, title="first"), _item(agent, title="second")

    _run()

    migrated = list(AgentTask.objects.exclude(ext_id="T1").order_by("ext_id"))
    assert [t.ext_id for t in migrated] == ["T2", "T3"]
    agent.refresh_from_db()
    assert agent.task_seq == 3          # the next task the agent creates is T4


def test_running_it_twice_changes_nothing(agent):
    """Migrations get re-run — on a retried deploy, or a squashed history."""
    _item(agent)

    _run()
    _run()

    assert AgentTask.objects.count() == 1


def test_reversing_removes_the_tasks_and_leaves_the_items(agent):
    _item(agent)
    _run()

    migration.tasks_back_to_items(real_apps, None)

    assert AgentTask.objects.count() == 0
    assert Item.objects.count() == 1
