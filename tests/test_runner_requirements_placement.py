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
        name="jj-mbp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE,
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


# --- the refusal names the requirement; the payload carries it -------------------


@pytest.mark.parametrize("move", ["send", "place", "move", "transfer"])
def test_placing_on_a_real_but_non_zdr_runner_says_zdr_is_why(fleet, move):
    """"unknown runner" is a lie when the runner exists: the person picked a box
    their own fleet listed, and needs to hear the conversation requires ZDR."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Turn

    s = _zdr_session(fleet)
    laptop = str(fleet["laptop"].id)
    with pytest.raises(ValueError) as exc:
        if move == "send":
            session_services.send_message(session=s, text="hi", user=fleet["user"],
                                          placement=laptop)
        elif move == "place":
            Turn.objects.create(chat_session=s, prompt="hi", status=Turn.QUEUED)
            session_services.place_queued_turn(session=s, placement=laptop)
        elif move == "move":
            Turn.objects.create(chat_session=s, prompt="hi", status=Turn.QUEUED)
            session_services.move_queued_turns(session=s, placement=laptop)
        else:
            RunnerBinding.objects.create(session=s, runner=fleet["cloud"])
            session_services.transfer_session(session=s, placement=laptop, brief="b")
    assert "requires a ZDR runner" in str(exc.value)


def test_a_runner_that_does_not_exist_is_still_unknown(fleet):
    import uuid

    s = _zdr_session(fleet)
    with pytest.raises(ValueError) as exc:
        session_services.send_message(session=s, text="hi", user=fleet["user"],
                                      placement=str(uuid.uuid4()))
    assert "ZDR" not in str(exc.value)


def test_the_session_payload_carries_its_runner_requirements(fleet):
    from django.test import Client

    s = _zdr_session(fleet)
    plain = Session.objects.create(agent=fleet["agent"], workspace=fleet["ws"], title="p")
    from apps.canopy_sessions.models import SessionParticipant

    for sess in (s, plain):
        SessionParticipant.objects.create(session=sess, user=fleet["user"], role="owner")
    c = Client()
    c.force_login(fleet["user"])
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["runner_requirements"] == ["zdr"]
    assert c.get(f"/api/canopy-sessions/{plain.id}").json()["runner_requirements"] == []
