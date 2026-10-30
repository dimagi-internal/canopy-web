"""A session with no runner binding still says which box ran its turns.

A caller's confined turn is never bound to its box (the cloud runner skips
record-session so nothing can resume into it), so a Slack or email caller's
session has no `runner_name`. The turn knows which box claimed it; the session
row carries that as `turn_runner_name` and the IDE reads it instead of "web".
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.api import _out
from apps.canopy_sessions.models import Session
from apps.canopy_sessions.transcript_read import with_driving_turn
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def unbound():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj)
    session = Session.objects.create(workspace=ws, agent=agent, origin=Session.ORIGIN_WEB, title="ask")
    return {"jj": jj, "ws": ws, "agent": agent, "session": session}


def _claimed(unbound, box, *, at):
    return Turn.objects.create(
        chat_session=unbound["session"], origin=Turn.ORIGIN_SLACK,
        idempotency_key=uuid.uuid4().hex, turn_mode="manual", claimed_by=box, claimed_at=at,
    )


def _row(unbound):
    return with_driving_turn(Session.objects.filter(pk=unbound["session"].pk)).get()


def test_an_unbound_session_names_the_box_that_claimed_its_newest_turn(unbound):
    old = Runner.objects.create(name="cloud-old", kind=Runner.CLOUD, owner=unbound["jj"])
    live = Runner.objects.create(name="cloud-ec2-live", kind=Runner.CLOUD, owner=unbound["jj"])
    now = timezone.now()
    _claimed(unbound, old, at=now - timezone.timedelta(hours=1))
    _claimed(unbound, live, at=now)
    out = _out(_row(unbound))
    assert out["runner_name"] is None
    assert out["turn_runner_name"] == "cloud-ec2-live"


def test_a_session_nothing_has_claimed_names_no_box(unbound):
    Turn.objects.create(chat_session=unbound["session"],
                        origin=Turn.ORIGIN_SLACK, idempotency_key=uuid.uuid4().hex)
    assert _out(_row(unbound))["turn_runner_name"] is None
