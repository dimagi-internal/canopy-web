"""A runner is owned by a person, never by an agent's login.

`owner` is a box's identity for life: claims run with the owner's
memberships, and only the owner may grant administration. On 2026-10-02 a
teammate's laptop was paired with ACE's token, which produced a box its own
operator could not manage, and on which their work would be attributed to ACE.
An agent may still ADMINISTER a box, through an explicit grant.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def fleet():
    human = User.objects.create_user("st", "stewari@dimagi.com", "pw")
    ace_login = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=human)
    WorkspaceMembership.objects.create(user=human, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=ace_login, workspace=ws, role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="ace", name="ACE", workspace=ws, user=ace_login)
    return {"human": human, "ace_login": ace_login}


def _pair(user):
    c = Client()
    c.force_login(user)
    return c.post("/api/harness/runners/",
                  data=json.dumps({"name": "st-mbp", "kind": "emdash", "workspace": "connect"}),
                  content_type="application/json")


def test_an_agent_login_cannot_pair_a_runner(fleet):
    res = _pair(fleet["ace_login"])
    assert res.status_code == 403
    assert "owned by a person" in res.json()["detail"]
    assert not Runner.objects.exists()


def test_a_person_can_pair_and_then_grant_the_agent_admin(fleet):
    res = _pair(fleet["human"])
    assert res.status_code == 201, res.content
    runner_id = res.json()["id"]

    c = Client()
    c.force_login(fleet["human"])
    grant = c.post(f"/api/harness/runners/{runner_id}/admins",
                   data=json.dumps({"email": "ace@dimagi-ai.com"}),
                   content_type="application/json")
    assert grant.status_code == 200, grant.content
    assert Runner.objects.get(pk=runner_id).owner == fleet["human"]
