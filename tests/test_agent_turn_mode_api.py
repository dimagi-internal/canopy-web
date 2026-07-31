"""PATCH /api/agents/{slug}/turn-mode — the board-side autonomy switch.

Turn mode is operational STATE (the human flips it from the board; the fleet
turn procedure reads it at preflight), so it must be settable only via its own
endpoint — never by the agent-repo self-publish upsert (POST /api/agents/),
which would let an agent change its own autonomy.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


def _patch(client, slug, mode):
    return client.patch(
        f"/api/agents/{slug}/turn-mode",
        data=json.dumps({"turn_mode": mode}),
        content_type="application/json",
    )


def test_new_agent_defaults_to_gated(client, agent):
    res = client.get(f"/api/agents/{agent.slug}/")
    assert res.status_code == 200
    assert res.json()["turn_mode"] == "gated"


def test_patch_flips_mode_and_returns_it(client, agent):
    res = _patch(client, agent.slug, "auto")
    assert res.status_code == 200
    assert res.json()["turn_mode"] == "auto"
    agent.refresh_from_db()
    assert agent.turn_mode == Agent.AUTO

    res = _patch(client, agent.slug, "gated")
    assert res.status_code == 200
    agent.refresh_from_db()
    assert agent.turn_mode == Agent.GATED


def test_patch_rejects_unknown_mode(client, agent):
    res = _patch(client, agent.slug, "yolo")
    assert res.status_code == 422
    agent.refresh_from_db()
    assert agent.turn_mode == Agent.GATED


def test_repo_upsert_cannot_touch_turn_mode(client, agent):
    agent.turn_mode = Agent.AUTO
    agent.save(update_fields=["turn_mode"])

    # The self-publish upsert (what `canopy agent skills` / register runs) —
    # a plain re-upsert must not reset the mode…
    res = client.post(
        "/api/agents/",
        data=json.dumps({"slug": "echo", "name": "Echo v2"}),
        content_type="application/json",
    )
    assert res.status_code == 201
    agent.refresh_from_db()
    assert agent.turn_mode == Agent.AUTO

    # …and naming the field outright is rejected (AgentIn is strict).
    res = client.post(
        "/api/agents/",
        data=json.dumps({"slug": "echo", "name": "Echo", "turn_mode": "gated"}),
        content_type="application/json",
    )
    assert res.status_code == 422
    agent.refresh_from_db()
    assert agent.turn_mode == Agent.AUTO


def test_patch_404s_outside_visible_workspaces(client, agent):
    other_owner = User.objects.create_user("other", "other@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    Agent.objects.create(slug="eva", name="Eva", workspace=other_ws)
    res = _patch(client, "eva", "auto")
    assert res.status_code == 404
