"""The `runner_code` alert source: a live runner executing code that isn't what shipped."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_runner_code as src
from apps.harness.models import Runner, RunnerAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_runner_code"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def box(monkeypatch, settings):
    settings.RUNNER_CODE_SHA = "b" * 40
    settings.RUNNER_CODE_COMMITTED_AT = 2000
    jj, admin, other = _user("jj"), _user("ad"), _user("other")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    for u in (jj, admin, other):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.EDITOR)
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
                                   workspace=ws, last_heartbeat_at=timezone.now(), capabilities={},
                                   code_version="0.1.0", code_sha="a" * 40, code_committed_at=1000)
    RunnerAdmin.objects.create(runner=runner, user=admin)
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    # Long past the deploy: the updater has had its chance.
    monkeypatch.setattr(src, "STARTED_AT", timezone.now() - timedelta(days=1))
    return {"jj": jj, "admin": admin, "other": other, "runner": runner}


def _set(runner, **fields):
    Runner.objects.filter(pk=runner.pk).update(**fields)
    runner.refresh_from_db()


def test_outdated_live_runner_raises_for_its_admins(box):
    (r,) = src.reconcile(timezone.now())
    assert r.subject_key == f"runner_code:{box['runner'].pk}"
    assert r.title == "jj-mbp is running an older runner than shipped"
    assert src.UPDATE_CMD in r.body and "aaaaaaaaaaaa" in r.body and "bbbbbbbbbbbb" in r.body
    alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    assert a.severity == "action"
    assert [x.id for x in src.actions(a, box["jj"])] == ["open"]
    assert [x.id for x in src.actions(a, box["admin"])] == ["open"]
    assert src.actions(a, box["other"]) == []


def test_wrong_branch_outranks_outdated(box):
    _set(box["runner"], code_branch="feature/x")
    (r,) = src.reconcile(timezone.now())
    assert r.title == 'jj-mbp is running branch "feature/x", not main'
    assert src.BRANCH_CMD in r.body


@pytest.mark.parametrize("fields", [
    {"code_sha": "b" * 40},                          # current
    {"code_sha": ""},                                # unknown
    {"code_committed_at": 3000},                     # ahead of the deploy
])
def test_nothing_to_fix_raises_nothing(box, fields):
    _set(box["runner"], **fields)
    assert src.reconcile(timezone.now()) == []


def test_unknown_order_counts_as_outdated(box):
    _set(box["runner"], code_committed_at=0)
    assert len(src.reconcile(timezone.now())) == 1


def test_quiet_runner_resolves_and_coming_back_reopens(box):
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.OPEN
    _set(box["runner"], last_heartbeat_at=timezone.now() - timedelta(minutes=10))
    assert src.reconcile(timezone.now()) == []
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.RESOLVED
    _set(box["runner"], last_heartbeat_at=timezone.now())
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.OPEN


def test_update_resolves(box):
    alerts.tick(timezone.now())
    _set(box["runner"], code_sha="b" * 40)
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.RESOLVED


def test_paused_runner_still_raises_but_private_one_does_not(box):
    _set(box["runner"], paused=True)
    assert len(src.reconcile(timezone.now())) == 1
    _set(box["runner"], paused=False, workspace=None)
    assert src.reconcile(timezone.now()) == []


def test_fresh_deploy_gives_the_updater_its_grace(box, monkeypatch):
    monkeypatch.setattr(src, "STARTED_AT", timezone.now())
    assert src.reconcile(timezone.now()) == []
    later = timezone.now() + src.OUTDATED_GRACE + timedelta(minutes=1)
    assert len(src.reconcile(later)) == 1


def test_open_alert_stays_open_through_a_new_deploys_grace(box, monkeypatch, settings):
    alerts.tick(timezone.now())
    monkeypatch.setattr(src, "STARTED_AT", timezone.now())
    settings.RUNNER_CODE_SHA = "c" * 40
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.OPEN


def test_wrong_branch_has_no_grace(box, monkeypatch):
    monkeypatch.setattr(src, "STARTED_AT", timezone.now())
    _set(box["runner"], code_branch="feature/x")
    assert len(src.reconcile(timezone.now())) == 1
