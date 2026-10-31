"""Placement only offers runners that satisfy a conversation's runner requirements."""
from __future__ import annotations

import pytest

from tests.access_helpers import publish
from tests.fixtures.sensitive import vault_key
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import placement as session_placement
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, RunnerFlag
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# A sensitive-data box holds only an agent with a `data: sensitive` capability (dedicated box).
SENSITIVE_IFACE = {"capabilities": {"data": {"callers": ["member"], "sites": ["connect-labs"],
                                       "data": "sensitive", "tools": ["Read"]}}}


def _capable(r):
    Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
    r.refresh_from_db()
    return r


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    vault_key(jj)  # a sensitive conversation is sealed to its people's vault keys
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    publish(echo, SENSITIVE_IFACE)
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


def _sensitive_session(fleet):
    # Started by the person who sends into it: a sensitive conversation's key is its
    # starter's, and nobody becomes its holder by sending (`conversation_keys.require_key`).
    return Session.objects.create(
        agent=fleet["agent"], workspace=fleet["ws"], title="c", created_by=fleet["user"],
        metadata={"runner_requirements": ["sensitive_data"]},
    )


def test_a_ordinary_runner_is_not_a_placement_for_a_sensitive_session(fleet):
    s = _sensitive_session(fleet)
    assert session_placement._placeable_runner(s, str(fleet["laptop"].id)) is None


def test_a_sensitive_runner_is(fleet):
    s = _sensitive_session(fleet)
    RunnerFlag.objects.create(runner=fleet["cloud"], flag="sensitive_data")
    _reports_sensitive(fleet["cloud"])
    got = session_placement._placeable_runner(s, str(fleet["cloud"].id))
    assert got is not None and got.id == fleet["cloud"].id


def test_available_cloud_runner_skips_a_ordinary_box(fleet):
    s = _sensitive_session(fleet)
    assert session_placement.available_cloud_runner(s, user=fleet["user"]) is None
    RunnerFlag.objects.create(runner=fleet["cloud"], flag="sensitive_data")
    _reports_sensitive(fleet["cloud"])
    assert session_placement.available_cloud_runner(s, user=fleet["user"]).id == fleet["cloud"].id


def test_sending_with_an_explicit_ordinary_placement_is_refused(fleet):
    s = _sensitive_session(fleet)
    with pytest.raises(ValueError):
        session_send.send_message(
            session=s, text="hi", user=fleet["user"], placement=str(fleet["laptop"].id)
        )


# --- the refusal names the requirement; the payload carries it -------------------


@pytest.mark.parametrize("move", ["send", "place", "move", "transfer"])
def test_placing_on_a_real_but_ordinary_runner_says_sensitive_is_why(fleet, move):
    """"unknown runner" is a lie when the runner exists: the person picked a box
    their own fleet listed, and needs to hear the conversation requires sensitive-data."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Turn

    s = _sensitive_session(fleet)
    laptop = str(fleet["laptop"].id)
    with pytest.raises(ValueError) as exc:
        if move == "send":
            session_send.send_message(session=s, text="hi", user=fleet["user"],
                                          placement=laptop)
        elif move == "place":
            Turn.objects.create(chat_session=s, prompt="hi", status=Turn.QUEUED)
            session_placement.place_queued_turn(session=s, placement=laptop)
        elif move == "move":
            Turn.objects.create(chat_session=s, prompt="hi", status=Turn.QUEUED)
            session_placement.move_queued_turns(session=s, placement=laptop)
        else:
            RunnerBinding.objects.create(session=s, runner=fleet["cloud"])
            session_placement.transfer_session(session=s, placement=laptop, brief="b")
    assert "requires a sensitive-data runner" in str(exc.value)


def test_a_runner_that_does_not_exist_is_still_unknown(fleet):
    import uuid

    s = _sensitive_session(fleet)
    with pytest.raises(ValueError) as exc:
        session_send.send_message(session=s, text="hi", user=fleet["user"],
                                      placement=str(uuid.uuid4()))
    assert "sensitive-data" not in str(exc.value)


def test_the_session_payload_carries_its_runner_requirements(fleet):
    from django.test import Client

    s = _sensitive_session(fleet)
    plain = Session.objects.create(agent=fleet["agent"], workspace=fleet["ws"], title="p")
    from apps.canopy_sessions.models import SessionParticipant

    for sess in (s, plain):
        SessionParticipant.objects.create(session=sess, user=fleet["user"], role="owner")
    c = Client()
    c.force_login(fleet["user"])
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["runner_requirements"] == ["sensitive_data"]
    assert c.get(f"/api/canopy-sessions/{plain.id}").json()["runner_requirements"] == []


def _reports_sensitive(runner):
    """The box reports sensitive-data mode on its heartbeat: without it a person's `sensitive_data`
    declaration is not in force (runner_requirements.in_force)."""
    from apps.harness.models import Runner as _Runner

    runner.capabilities = {**(runner.capabilities or {}), "data_mode": "sensitive_data"}
    _Runner.objects.filter(pk=runner.pk).update(capabilities=runner.capabilities)
    return runner
