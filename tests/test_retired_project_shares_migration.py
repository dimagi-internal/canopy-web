"""Each live project session share is recorded in the settings audit before its table is dropped
(canopy_sessions 0069). Delete once that migration has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

_AGENTS = ("agents", "0134_drop_interface_source")       # the state the project row is written in
BEFORE = [("canopy_sessions", "0068_session_relation"), _AGENTS]
AFTER = [("canopy_sessions", "0069_record_retired_project_session_shares"), _AGENTS]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_a_live_share_is_recorded_and_a_revoked_one_is_not(default_workspace):
    apps = _state(BEFORE)
    Project, Share = apps.get_model("agents", "AgentProject"), apps.get_model("canopy_sessions", "ProjectSessionShare")
    User = get_user_model()
    lili, ann = User.objects.create_user("lili", "lili@dimagi.com", "pw"), User.objects.create_user("ann", "ann@dimagi.com", "pw")
    project = Project.objects.create(workspace_id=default_workspace.pk, handle="supply", name="Supply",
                                     repo_slug="connect-labs", visibility="workspace")
    live = Share.objects.create(user_id=lili.pk, project_id=project.pk)
    Share.objects.create(user_id=ann.pk, project_id=project.pk, revoked_at=timezone.now())

    apps = _state(AFTER)
    changes = apps.get_model("events", "SettingChange").objects.filter(operation="project_session_share_retired")
    assert changes.count() == 1
    row = changes.get()
    assert row.workspace_id == default_workspace.pk
    assert row.target == f"user:{lili.pk} / project:{project.pk}"
    assert row.changes[0]["before"]["shared"] is True and row.changes[0]["after"]["shared"] is False
    assert "lili" not in str(row.changes) + row.summary          # ids only
    assert live.pk


def test_with_no_live_share_nothing_is_recorded():
    _state(BEFORE)
    apps = _state(AFTER)
    assert not apps.get_model("events", "SettingChange").objects.filter(operation="project_session_share_retired").exists()
