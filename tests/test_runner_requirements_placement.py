"""Placement only offers runners that satisfy a conversation's runner requirements."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, RunnerFlag
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _capable(r):
    Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
    r.refresh_from_db()
    return r


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    _capable(laptop)
    _capable(cloud)
    return {"user": jj, "ws": ws, "agent": echo, "laptop": laptop, "cloud": cloud}


def _zdr_session(fleet):
    return Session.objects.create(
        agent=fleet["agent"], workspace=fleet["ws"], title="c",
        metadata={"runner_requirements": ["zdr"]},
    )


def test_a_non_zdr_runner_is_not_a_placement_for_a_zdr_session(fleet):
    s = _zdr_session(fleet)
    assert session_services._placeable_runner(s, str(fleet["laptop"].id)) is None


def test_a_zdr_runner_is(fleet):
    s = _zdr_session(fleet)
    RunnerFlag.objects.create(runner=fleet["cloud"], flag="zdr")
    got = session_services._placeable_runner(s, str(fleet["cloud"].id))
    assert got is not None and got.id == fleet["cloud"].id


def test_available_cloud_runner_skips_a_non_zdr_box(fleet):
    s = _zdr_session(fleet)
    assert session_services.available_cloud_runner(s) is None
    RunnerFlag.objects.create(runner=fleet["cloud"], flag="zdr")
    assert session_services.available_cloud_runner(s).id == fleet["cloud"].id


def test_sending_with_an_explicit_non_zdr_placement_is_refused(fleet):
    s = _zdr_session(fleet)
    with pytest.raises(ValueError):
        session_services.send_message(
            session=s, text="hi", user=fleet["user"], placement=str(fleet["laptop"].id)
        )
