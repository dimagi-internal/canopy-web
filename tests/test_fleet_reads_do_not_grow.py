"""The fleet reads cost a fixed number of queries, however many runners.

The runner topology and the runner list once asked the tenant rule
(`routing.runner_tenant_slugs`), runner administration and the RunnerOut row's
related rows one runner at a time — about six queries per box. They now read
those once (`runner_tenant_slugs_of`, `administered_runner_ids`, joined rows;
docs/architecture/roles.md § A view that judges many pairs reads once). This
pins it: adding runners must not add queries.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership as M
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db
User = get_user_model()

PATHS = ["/api/workspaces/dimagi/runner-topology", "/api/w/dimagi/harness/runners/"]


def _user(email):
    return User.objects.create(username=email, email=email)


@pytest.fixture
def fleet():
    jj = _user("jj@dimagi.com")
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    div = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=root)
    M.objects.create(workspace=root, user=jj, role=M.OWNER)
    agents = [Agent.objects.create(slug=f"a{i}", name=f"A{i}", workspace=[root, div][i % 2], owner=jj)
              for i in range(4)]
    return {"jj": jj, "root": root, "div": div, "agents": agents}


def _add_runners(fleet, n, start):
    for i in range(start, start + n):
        owner = _user(f"owner{i}@dimagi.com")
        M.objects.create(workspace=fleet["root"], user=owner, role=M.EDITOR)
        r = enrolled(Runner.objects.create(name=f"r{i}", kind=Runner.EMDASH, workspace=fleet["root"],
                                           owner=owner), fleet["root"])
        RunnerAdmin.objects.create(runner=r, user=fleet["jj"])
        RunnerAssignment.objects.create(agent=fleet["agents"][i % 4], runner=r, rank=i)


def _count(user, path):
    c = Client()
    c.force_login(user)
    with CaptureQueriesContext(connection) as ctx:
        assert c.get(path).status_code == 200, path
    return len(ctx.captured_queries)


@pytest.mark.parametrize("path", PATHS)
def test_queries_do_not_grow_with_the_runners(fleet, path):
    # Four to start, so every agent already has a route of its own: an agent with
    # none reads its workspace's inherited order, which is not what this measures.
    _add_runners(fleet, 4, 0)
    before = _count(fleet["jj"], path)
    _add_runners(fleet, 8, 4)
    assert _count(fleet["jj"], path) == before
