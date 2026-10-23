"""The data migration that makes "directed only" a person's setting (harness 0107):
each box keeps what it was — a person's override where one was set, else what the
box declared — and the box's declaration is dropped from its capabilities."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# needs_migrations: it walks the real migration graph (ci.yml, `Backend pytest (migrations)`).
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("harness", "0106_runner_slash_commands")]
AFTER = [("harness", "0107_runner_directed")]


def test_each_box_keeps_what_it_was():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Runner = old.get_model("harness", "Runner")
    owner = User.objects.create(username="jj", email="jjackson@dimagi.com")
    test_box = Runner.objects.create(name="cloud-ec2-fork", kind="cloud", owner=owner,
                                     capabilities={"sessions": True, "directed": True})
    overridden = Runner.objects.create(name="cloud-zdr-1", kind="cloud", owner=owner,
                                       capabilities={"directed": True}, directed_setting=False)
    forced = Runner.objects.create(name="cloud-ec2-9", kind="cloud", owner=owner,
                                   capabilities={}, directed_setting=True)
    plain = Runner.objects.create(name="cloud-ec2-2", kind="cloud", owner=owner, capabilities={})

    executor.loader.build_graph()
    executor.migrate(AFTER)
    Runner = executor.loader.project_state(AFTER).apps.get_model("harness", "Runner")
    got = {r.name: (r.directed, "directed" in (r.capabilities or {})) for r in Runner.objects.all()}
    assert got == {test_box.name: (True, False), overridden.name: (False, False),
                   forced.name: (True, False), plain.name: (False, False)}
