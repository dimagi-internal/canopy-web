"""`GET /api/harness/turns/` pages past its 200-row clamp, and filters by runner.

The list is newest first and clamped to 200, so a window longer than 200 turns could
not be read at all: a fleet survey of one busy agent saw about a day. `?before=` takes
the last row's `created_at` back as a cursor, `?since=` bounds the window, and
`?runner=` keeps the turns one runner claimed — what a box's occupancy is measured from.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

T0 = timezone.now().replace(microsecond=0) - dt.timedelta(days=10)


@pytest.fixture()
def owner():
    user = User.objects.create_user("owner", "owner@example.org", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    box = Runner.objects.create(name="cloud-box", kind=Runner.CLOUD, capabilities={})
    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, capabilities={})
    for i in range(5):  # one turn a day; even days on the cloud box, odd on the laptop
        t = Turn.objects.create(agent=agent, prompt=f"t{i}", idempotency_key=f"eva-{i}",
                                claimed_by=box if i % 2 == 0 else laptop)
        Turn.objects.filter(pk=t.pk).update(created_at=T0 + dt.timedelta(days=i))
    return user


def _get(user, query):
    c = Client()
    c.force_login(user)
    resp = c.get(f"/api/harness/turns/?agent=eva&{query}")
    assert resp.status_code == 200, resp.content
    return [row["prompt"] for row in resp.json()]


def _iso(d):
    return d.isoformat().replace("+00:00", "Z")


def test_before_pages_back_from_a_cursor(owner):
    assert _get(owner, "limit=2") == ["t4", "t3"]
    assert _get(owner, f"limit=2&before={_iso(T0 + dt.timedelta(days=3))}") == ["t2", "t1"]


def test_since_bounds_the_window(owner):
    assert _get(owner, f"since={_iso(T0 + dt.timedelta(days=3))}") == ["t4", "t3"]


def test_runner_keeps_what_that_runner_claimed(owner):
    assert _get(owner, "runner=cloud-box") == ["t4", "t2", "t0"]
    assert _get(owner, "runner=nope") == []


def test_no_new_params_means_the_list_is_unchanged(owner):
    assert _get(owner, "") == ["t4", "t3", "t2", "t1", "t0"]
