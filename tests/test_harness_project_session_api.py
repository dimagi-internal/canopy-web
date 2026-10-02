"""The runner-facing resolve/record-session endpoints for PROJECT targets.

An agent session is tenant-gated by _agent_or_404; a project session has no
agent, so these endpoints gate on the caller's own workspace. A non-member must
get 404, never another user's rolling summary.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture
def canopy(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def runner(owner, canopy):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=owner,
        workspace=canopy, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web"]},
    )


@pytest.fixture
def owner_client(owner):
    c = Client()
    c.force_login(owner)
    return c


def test_member_records_and_resolves_a_project_session(owner_client, runner, canopy):
    rec = owner_client.post(
        f"/api/harness/runners/{runner.id}/record-session",
        {"project": "canopy-web", "workspace": "canopy",
         "thread_key": "phone:jj:canopy-web", "session_key": "task-1", "summary": "ctx"},
        content_type="application/json",
    )
    assert rec.status_code == 200, rec.content

    binding = RunnerBinding.objects.get(session__project="canopy-web")
    assert binding.session.workspace_id == canopy.slug  # stamped from the caller's tenant

    res = owner_client.post(
        f"/api/harness/runners/{runner.id}/resolve-session",
        {"project": "canopy-web", "workspace": "canopy", "thread_key": "phone:jj:canopy-web"},
        content_type="application/json",
    )
    assert res.json()["session_key"] == "task-1"


def test_a_caller_with_no_workspace_cannot_record_a_project_session(owner):
    """The gate: no resolvable tenant → 404, and no link is written. A runner
    with a null workspace + null owner is the legacy-ungated path _runner_or_404
    still allows, so the block must come from _project_workspace_or_404."""
    loner = User.objects.create_user("loner", "loner@example.org", "pw")
    runner = Runner.objects.create(
        name="loner-box", kind=Runner.EMDASH, host="h", status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(),
    )
    c = Client()
    c.force_login(loner)

    resp = c.post(
        f"/api/harness/runners/{runner.id}/record-session",
        {"project": "canopy-web", "thread_key": "phone:jj:canopy-web", "session_key": "t"},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert not RunnerBinding.objects.filter(session__project="canopy-web").exists()


def test_another_tenant_cannot_resolve_a_project_thread_it_guesses(owner_client, runner, canopy):
    """Even holding the exact thread_key, a runner scoped to a different workspace
    resolves new_thread — the summary never crosses the tenant boundary."""
    owner_client.post(
        f"/api/harness/runners/{runner.id}/record-session",
        {"project": "canopy-web", "workspace": "canopy",
         "thread_key": "phone:jj:canopy-web", "session_key": "secret", "summary": "secret context"},
        content_type="application/json",
    )

    mallory = User.objects.create_user("mallory", "mallory@example.org", "pw")
    m_ws = Workspace.objects.create(slug="mallory", display_name="M", created_by=mallory)
    WorkspaceMembership.objects.create(user=mallory, workspace=m_ws, role=WorkspaceMembership.OWNER)
    m_runner = Runner.objects.create(
        name="m-box", kind=Runner.EMDASH, host="mh", owner=mallory, workspace=m_ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    mc = Client()
    mc.force_login(mallory)

    # Mallory names Jonathan's workspace she is NOT a member of -> 404, no leak.
    resp = mc.post(
        f"/api/harness/runners/{m_runner.id}/resolve-session",
        {"project": "canopy-web", "workspace": "canopy", "thread_key": "phone:jj:canopy-web"},
        content_type="application/json",
    )
    assert resp.status_code == 404

    # And naming her OWN workspace finds nothing (the link lives in canopy).
    resp2 = mc.post(
        f"/api/harness/runners/{m_runner.id}/resolve-session",
        {"project": "canopy-web", "workspace": "mallory", "thread_key": "phone:jj:canopy-web"},
        content_type="application/json",
    )
    assert resp2.status_code == 200
    assert resp2.json()["new_thread"] is True
    assert resp2.json()["summary"] == ""
