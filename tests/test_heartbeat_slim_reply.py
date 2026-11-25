"""A runner that sends `slim_reply: 1` on its heartbeat gets `RunnerHeartbeatSlimOut`:
every field either runner reads off the reply, none of the fleet page's.

The full `RunnerOut` reads each agent the box runs and its GitHub readiness on
every beat (its `health_checks`); a box beating every few seconds paid that walk
for fields it never reads. A runner that does not send the flag (older code) gets
the whole row, as before.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, RunnerProjectRequest
from apps.harness.schemas import RunnerHeartbeatSlimOut
from apps.harness.testing import enrolled
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

#: What the runners read off a heartbeat reply, by the code that reads it.
LAPTOP_READS = {
    "status", "status_note", "ready", "ready_note", "remedy", "paused", "key_waiting",  # server_status.accepted
    "private", "private_runner_id", "name", "host", "id",  # private_runner.observe
    "report_projects",  # server_status.accepted
    "cancel_turn_ids",  # main.honour_stops
    "engine",  # desktop.observe
    "refresh_pending",  # refresh.observe
    "add_projects",  # add_project.observe
    "sync_version",  # runner_sync: `v`
}
CLOUD_READS = {"paused", "paused_note", "expected_canopy_version", "refresh_pending", "cancel_turn_ids",
               "sync_version"}


@pytest.fixture
def box():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    runner = enrolled(Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj@mbp", owner=jj, status=Runner.ONLINE, workspace=ws,
        last_heartbeat_at=timezone.now(), capabilities={"projects": ["a0"]},
        health={"checks": [{"name": "disk", "status": "ok", "detail": ""}],
                "received_at": timezone.now().isoformat()}))
    for i in range(8):
        a = Agent.objects.create(slug=f"a{i}", name=f"A{i}", workspace=ws, owner=jj)
        RunnerAssignment.objects.create(agent=a, runner=runner, rank=0, enabled=True)
    RunnerProjectRequest.objects.create(runner=runner, name="bailey", repo_url="https://github.com/x/bailey",
                                        requested_by=jj)
    c = Client()
    c.force_login(jj)
    return runner, c


def _beat(c, runner, **body):
    with CaptureQueriesContext(connection) as ctx:
        resp = c.post(f"/api/harness/runners/{runner.pk}/heartbeat",
                      data={"active_turn_ids": [], "code_sha": "abc", **body},
                      content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json(), len(ctx.captured_queries)


def test_the_slim_reply_carries_every_field_either_runner_reads(box):
    assert LAPTOP_READS | CLOUD_READS == set(RunnerHeartbeatSlimOut.model_fields)


def test_a_runner_that_opts_in_gets_the_slim_reply_with_the_full_replys_values(box):
    runner, c = box
    full, full_queries = _beat(c, runner)
    slim, slim_queries = _beat(c, runner, slim_reply=1)
    assert "health_checks" in full and "health_checks" not in slim
    assert set(slim) == set(RunnerHeartbeatSlimOut.model_fields)
    assert {k: full[k] for k in slim} == slim
    assert [p["name"] for p in slim["add_projects"]] == ["bailey"]
    assert slim_queries < full_queries, (slim_queries, full_queries)


def test_a_runner_on_older_code_gets_the_whole_row(box):
    runner, c = box
    full, _ = _beat(c, runner)
    assert {"health_checks", "capabilities", "roots", "expected_code_sha"} <= set(full)
    assert "github.a0" in full["health_checks"]
