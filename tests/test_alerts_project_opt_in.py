"""The `canopy_projects_off` alert source: a runner's owner has opted no repo into
canopy, so the box takes no repo work and mirrors nothing — said to them alone."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_project_opt_in as src
from apps.harness.models import ProjectOptIn, Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = [pytest.mark.django_db, pytest.mark.explicit_opt_in]

SOURCE = "apps.harness.alerts_project_opt_in"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def box(monkeypatch):
    jj, other = _user("jj"), _user("other")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    for u in (jj, other):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
                                   workspace=ws, last_heartbeat_at=timezone.now(),
                                   capabilities={"projects": ["canopy-web", "blog"]})
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    return {"jj": jj, "other": other, "ws": ws, "runner": runner}


def _open():
    return list(Alert.objects.filter(kind=src.KIND, status=Alert.OPEN))


def test_an_owner_who_chose_nothing_is_told_and_only_they_see_it(box):
    alerts.tick(timezone.now())
    [a] = _open()
    assert a.subject_key == f"user:{box['jj'].pk}"
    assert a.title == "Canopy is off in every repo on jj-mbp"
    assert f"runner:{box['runner'].pk}" in a.scopes
    assert [x.id for x in src.actions(a, box["jj"])] == ["choose", "dismiss"]
    assert src.actions(a, box["other"]) == []


@pytest.mark.parametrize("choice", [{"everything": True}, {"projects": ["blog"]}])
def test_choosing_anything_resolves_it(box, choice):
    alerts.tick(timezone.now())
    assert _open()
    ProjectOptIn.objects.create(user=box["jj"], **choice)
    alerts.tick(timezone.now())
    assert _open() == []


def test_a_box_that_reports_no_repo_raises_nothing(box):
    Runner.objects.filter(pk=box["runner"].pk).update(capabilities={})
    alerts.tick(timezone.now())
    assert _open() == []


def test_a_retired_box_raises_nothing(box):
    Runner.objects.filter(pk=box["runner"].pk).update(status=Runner.RETIRED)
    alerts.tick(timezone.now())
    assert _open() == []


def test_two_boxes_are_one_alert(box):
    Runner.objects.create(name="jj-mini", kind=Runner.EMDASH, owner=box["jj"], workspace=box["ws"],
                          capabilities={"projects": ["ace"]})
    alerts.tick(timezone.now())
    [a] = _open()
    assert a.title == "Canopy is off in every repo on jj-mbp, jj-mini"
