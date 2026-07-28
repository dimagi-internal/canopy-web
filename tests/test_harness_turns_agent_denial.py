"""Filtering `?agent=` to zero rows must not be how a permission denial is expressed.

`GET /api/harness/turns/?agent=<slug>` tenant-filters its queryset but never checks
that the REQUESTED agent is one the caller may see. A non-member therefore got
`200 []` — indistinguishable from "that agent has never run" — where the sibling
route `/api/agents/<slug>/tasks/` correctly returns 404.

Measured on production 2026-07-28, same base URL, two tokens:

    hal PAT   /api/harness/turns/?agent=eva  -> 200, 0 rows
    jjackson  /api/harness/turns/?agent=eva  -> 200, 71 rows
    hal PAT   /api/agents/eva/tasks/         -> 404

This matters more, not less, under the intended model — agents legitimately hold
DIFFERENT permission sets, so any fleet survey routinely queries agents it cannot
see, and each one reads back as healthy-and-idle. Ada's `conduct` skill reads this
endpoint directly per agent to spot stuck turns. (`agent_health` is incidentally
safe: it resolves `/api/agents/<slug>/` first, which already 404s.)

404, not 403 — same no-existence-leak rule as `_agent_or_404` and
`agents.api._get_agent_or_404`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def other_ws(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def home_ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def eva(other_ws):
    """An agent homed in a tenant the outsider is not a member of."""
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=other_ws)
    Turn.objects.create(agent=agent, prompt="cadence work", idempotency_key="eva-1")
    return agent


@pytest.fixture()
def outsider(home_ws):
    """A member of `connect` only — the shape of every non-eva agent identity."""
    user = User.objects.create_user("hal", "hal@example.org", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=home_ws,
                                       role=WorkspaceMembership.EDITOR)
    return user


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_non_member_gets_404_not_an_empty_list(eva, outsider):
    resp = _client(outsider).get("/api/harness/turns/?agent=eva")
    assert resp.status_code == 404, (
        "a denial returned as 200 [] is indistinguishable from an idle agent"
    )


def test_unknown_agent_gets_the_same_404_no_existence_leak(eva, outsider):
    """A non-member and a typo must be indistinguishable, or the endpoint enumerates
    which tenants' agents exist."""
    denied = _client(outsider).get("/api/harness/turns/?agent=eva")
    missing = _client(outsider).get("/api/harness/turns/?agent=nope-not-real")
    assert denied.status_code == missing.status_code == 404


def test_a_member_still_sees_the_turns(eva, owner):
    resp = _client(owner).get("/api/harness/turns/?agent=eva")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_the_unfiltered_list_is_unchanged(eva, outsider, home_ws):
    """Without `?agent=`, the endpoint still returns the caller's own tenants' turns
    — the tenant filter keeps doing its job; only the targeted lookup is gated."""
    mine = Agent.objects.create(slug="hal", name="Hal", workspace=home_ws)
    Turn.objects.create(agent=mine, prompt="my own work", idempotency_key="hal-1")

    resp = _client(outsider).get("/api/harness/turns/")
    assert resp.status_code == 200
    slugs = {t["agent_slug"] for t in resp.json()}
    assert slugs == {"hal"}, "unfiltered list must not leak eva, nor drop hal"
