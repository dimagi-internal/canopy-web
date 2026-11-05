"""Read mode's entry moves from `/<slug>:ask` to `/<slug>:answer-caller` (agents 0131). Delete once 0131
has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0130_project_member")]
AFTER = [("agents", "0131_read_mode_enters_through_answer_caller")]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _read_mode(entry):
    return {"capabilities": {"ask": {"entry": entry, "tools": ["Read"], "write_paths": ["{cwd}/.x-ask/*"]},
                             "connect": {"entry": None, "sites": ["connect-labs"], "tools": ["Read"]}}}


def test_only_an_agent_with_answer_caller_moves_and_only_its_command_name(default_workspace):
    apps = _state(BEFORE)
    Agent, Skill = apps.get_model("agents", "Agent"), apps.get_model("agents", "AgentSkill")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    made = {}
    for slug, entry, skill in [("hal", "/hal:ask --thread {thread_id}", True),
                               ("ace", "/ace:ask", True),
                               ("oolong", "/oolong:ask --thread {thread_id}", False),   # no skill: stays
                               ("eva", "/eva:asking --thread {thread_id}", True),       # not the command
                               ("echo", "/hal:ask --thread {thread_id}", True)]:        # not its own
        made[slug] = Agent.objects.create(slug=slug, name=slug, workspace_id=default_workspace.pk,
                                          owner_id=user.pk, interface=_read_mode(entry))
        if skill:
            Skill.objects.create(agent=made[slug], name="answer-caller")

    apps = _state(AFTER)
    Agent = apps.get_model("agents", "Agent")
    entry = {a.slug: a.interface["capabilities"]["ask"]["entry"] for a in Agent.objects.all()}
    assert entry == {"hal": "/hal:answer-caller --thread {thread_id}", "ace": "/ace:answer-caller",
                     "oolong": "/oolong:ask --thread {thread_id}", "eva": "/eva:asking --thread {thread_id}",
                     "echo": "/hal:ask --thread {thread_id}"}
    hal = Agent.objects.get(slug="hal").interface["capabilities"]
    assert hal["ask"]["write_paths"] == ["{cwd}/.x-ask/*"] and hal["connect"]["entry"] is None
    changes = apps.get_model("events", "SettingChange").objects.filter(operation="read_mode_migration",
                                                                        path="agents/0131")
    assert sorted(c.target for c in changes) == ["ace", "hal"]
    assert changes.get(target="hal").changes[0]["before"]["entry"] == "/hal:ask --thread {thread_id}"
