"""Choosing a laptop runner's session runtime: emdash or the Claude desktop app
(canopy-web#1188). The owner or a runner admin flips it on canopy-web; the runner
reads it off its own heartbeat response, so the flip needs no shell on the box.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.events.models import Event
from apps.harness.models import Runner, RunnerAdmin
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def admin():
    return User.objects.create_user("adm", "adm@dimagi.com", "pw")


@pytest.fixture()
def stranger():
    return User.objects.create_user("str", "str@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner, admin, stranger):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (admin, stranger):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return ws


@pytest.fixture()
def runner(owner, admin, workspace):
    r = Runner.objects.create(name="hal-mbp", kind=Runner.EMDASH, owner=owner, workspace=workspace)
    RunnerAdmin.objects.create(runner=r, user=admin, granted_by=owner)
    return r


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _put(c, runner, engine):
    return c.put(f"/api/harness/runners/{runner.id}/engine", json.dumps({"engine": engine}),
                 content_type="application/json")


def test_a_new_runner_runs_emdash(runner):
    assert runner.engine == Runner.ENGINE_EMDASH


def test_the_owner_switches_to_claude_desktop_and_back(owner, runner):
    c = _client(owner)
    r = _put(c, runner, "claude-desktop")
    assert r.status_code == 200 and r.json()["engine"] == "claude-desktop"
    runner.refresh_from_db()
    assert runner.engine == Runner.ENGINE_CLAUDE_DESKTOP
    assert Event.objects.filter(kind="runner.engine_changed").count() == 1
    assert _put(c, runner, "emdash").json()["engine"] == "emdash"


def test_a_runner_admin_may_switch_it(admin, runner):
    assert _put(_client(admin), runner, "claude-desktop").status_code == 200


def test_someone_who_cannot_administer_gets_404(stranger, runner):
    assert _put(_client(stranger), runner, "claude-desktop").status_code == 404
    runner.refresh_from_db()
    assert runner.engine == Runner.ENGINE_EMDASH


def test_an_unknown_runtime_is_422(owner, runner):
    r = _put(_client(owner), runner, "tmux")
    assert r.status_code == 422 and "claude-desktop" in r.json()["detail"]


def test_a_cloud_runner_has_no_runtime_to_choose(owner, workspace):
    cloud = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=workspace)
    assert _put(_client(owner), cloud, "claude-desktop").status_code == 422


def test_the_runner_reads_its_runtime_off_its_heartbeat(owner, runner):
    """The heartbeat response IS the delivery channel: no restart, no shell."""
    raw, _ = PersonalToken.create_for_user(user=owner, label="runner")
    box = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")

    def beat():
        r = box.post(f"/api/harness/runners/{runner.id}/heartbeat",
                     json.dumps({"active_turn_ids": []}), content_type="application/json")
        assert r.status_code == 200
        return r.json()["engine"]

    assert beat() == "emdash"
    _put(_client(owner), runner, "claude-desktop")
    assert beat() == "claude-desktop"
