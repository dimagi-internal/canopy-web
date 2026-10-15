"""Fixtures shared by the capability suites (AWS replaced by fakes.FakeAws)."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.agents.models import Agent
from apps.capabilities import aws, services
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_user, a_workspace

from .fakes import FakeAws
from .helpers import _acquire, _as, _grant, _json, _publish


@pytest.fixture()
def world(monkeypatch):
    w = FakeAws()
    monkeypatch.setattr(aws, "get", lambda region: w.broker())
    cache.delete(services.SWEEP_CACHE_KEY)
    cache.delete(services.SWEEP_RAN_KEY)
    return w


@pytest.fixture()
def tenancy(world):
    ws = a_workspace("connect")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    viewer = a_member(ws, email="vi@dimagi.com", role=M.VIEWER)
    outsider_ws = a_workspace("elsewhere")
    outsider = a_member(outsider_ws, email="out@example.com", role=M.OWNER)
    bot = a_user("ace@dimagi-ai.com")  # the agent's own login; not a member at all
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, user=bot)
    return {"ws": ws, "owner": owner, "editor": editor, "viewer": viewer, "outsider": outsider,
            "bot": bot, "agent": agent}


@pytest.fixture()
def ready_to_lease(tenancy):
    c = _as(tenancy["owner"])
    assert _publish(c).status_code == 200
    assert _grant(c).status_code == 200
    return c


@pytest.fixture()
def ready_lease(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    world.ready(lease["instance_id"])
    _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}")
    return lease
