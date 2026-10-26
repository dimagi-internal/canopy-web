"""A runner administrator renames a runner (`PUT /api/harness/runners/{id}/name`).

The name is what people see. Routing, pins and the vault follow the runner's id,
and the box keeps the name it paired as in `Runner.paired_as`, which pairing also
matches on — so a replaced instance still re-attaches to a renamed runner.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.events.models import SettingChange
from apps.harness import unit_pairing
from apps.harness.models import Runner, RunnerAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def box():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, host="cloud-ec2-2", owner=jj,
                                   workspace=dimagi, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   machine_id="arn:aws:iam::1:role/box")
    return {"jj": jj, "dimagi": dimagi, "runner": runner}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _rename(user, runner, name):
    return _client(user).put(f"/api/harness/runners/{runner.pk}/name", {"name": name},
                             content_type="application/json")


def test_the_owner_renames_it_and_the_change_is_audited(box):
    resp = _rename(box["jj"], box["runner"], "  cloud-ec2-live ")
    assert resp.status_code == 200, resp.content
    assert resp.json()["name"] == "cloud-ec2-live"
    runner = Runner.objects.get(pk=box["runner"].pk)
    assert runner.name == "cloud-ec2-live" and runner.paired_as == "cloud-ec2-2"
    assert resp.json()["paired_as"] == "cloud-ec2-2"
    change = SettingChange.objects.filter(path__endswith="/name").latest("id")
    assert "cloud-ec2-2" in str(change.changes) and "cloud-ec2-live" in str(change.changes)


def test_only_the_owner_or_a_runner_admin_may_rename_it(box):
    mia = get_user_model().objects.create_user(username="mia", email="mia@dimagi.com")
    WorkspaceMembership.objects.create(workspace=box["dimagi"], user=mia, role=WorkspaceMembership.EDITOR)
    assert _rename(mia, box["runner"], "mine").status_code in (403, 404)
    assert Runner.objects.get(pk=box["runner"].pk).name == "cloud-ec2-2"
    RunnerAdmin.objects.create(runner=box["runner"], user=mia, granted_by=box["jj"])
    assert _rename(mia, box["runner"], "cloud-ec2-live").status_code == 200


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_a_blank_or_overlong_name_is_refused(box, name):
    assert _rename(box["jj"], box["runner"], name).status_code == 422
    assert Runner.objects.get(pk=box["runner"].pk).name == "cloud-ec2-2"


def test_a_renamed_unit_is_still_found_by_the_name_it_paired_as(box):
    _rename(box["jj"], box["runner"], "cloud-ec2-live")
    found = unit_pairing._machine_runner("arn:aws:iam::1:role/box", "no-such-key", "cloud-ec2-2")
    assert found is not None and found.pk == box["runner"].pk
