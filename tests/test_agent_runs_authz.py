"""Authorization tests for /api/agents/{slug}/runs/... (apps/agent_runs/api.py) —
a non-member must get 404, never 403, and never a leak that the resource
exists. Mirrors apps/agents' and apps/harness's posture (see #421).

apps/agent_runs/api.py::_get_agent_or_404 carried the same fail-open pattern
those two sites had before #421 fixed them: `if agent.workspace_id and not
wsvc.is_member(...)` short-circuits to "allow" whenever `agent.workspace_id`
is falsy, so an unhomed (workspace=None) agent's entire run surface —
list/create runs, get a run (label, session_link, steps, artifacts, verdicts,
decisions, gates), list steps, record a gate/verdict, fork a run — was
reachable by ANY authenticated user, not just a workspace member. This file
pins the fix: a non-member of a homed agent still 404s, and a workspace-less
agent is unresolvable by anyone (not universally visible), while a genuine
member keeps working."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db.utils import IntegrityError
from django.test import Client

from apps.agents.models import Agent
from apps.agent_runs.models import AgentRun
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
def stranger():
    """Authenticated, but a member of nothing. auto_join_workspaces keys off the
    email domain, so use one outside the auto-join set."""
    return User.objects.create_user("stranger", "stranger@example.org", "pw")


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


@pytest.fixture()
def owner_client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def stranger_client(stranger):
    c = Client()
    c.force_login(stranger)
    return c


def _create_run(client, slug="echo", label="Run one"):
    return client.post(
        f"/api/agents/{slug}/runs/",
        {"label": label, "mode": "review"},
        content_type="application/json",
    )


# --- homed agent: ordinary tenant gate (member vs. non-member) --------------


def test_member_can_create_and_list_runs(owner_client, agent):
    resp = _create_run(owner_client)
    assert resp.status_code == 201
    assert owner_client.get("/api/agents/echo/runs/").status_code == 200


def test_stranger_cannot_create_run_for_someone_elses_agent(stranger_client, agent):
    """404, not 403: a non-member must not learn the agent exists."""
    resp = _create_run(stranger_client)
    assert resp.status_code == 404
    assert not AgentRun.objects.filter(agent=agent).exists()


def test_stranger_cannot_list_someone_elses_runs(owner_client, stranger_client, agent):
    _create_run(owner_client)
    resp = stranger_client.get("/api/agents/echo/runs/")
    assert resp.status_code == 404


def test_stranger_cannot_get_someone_elses_run(owner_client, stranger_client, agent):
    run_id = _create_run(owner_client).json()["id"]
    resp = stranger_client.get(f"/api/agents/echo/runs/{run_id}/")
    assert resp.status_code == 404


def test_stranger_cannot_list_someone_elses_run_steps(owner_client, stranger_client, agent):
    run_id = _create_run(owner_client).json()["id"]
    resp = stranger_client.get(f"/api/agents/echo/runs/{run_id}/steps/")
    assert resp.status_code == 404


def test_stranger_cannot_record_gate_on_someone_elses_run(owner_client, stranger_client, agent):
    run_id = _create_run(owner_client).json()["id"]
    resp = stranger_client.post(
        f"/api/agents/echo/runs/{run_id}/steps/design/gate",
        {"decision": "approve"},
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_stranger_cannot_fork_someone_elses_run(owner_client, stranger_client, agent):
    run_id = _create_run(owner_client).json()["id"]
    resp = stranger_client.post(
        f"/api/agents/echo/runs/{run_id}/fork",
        {"at_step": "design"},
        content_type="application/json",
    )
    assert resp.status_code == 404


# --- unhomed (workspace-less) agent: now unrepresentable, not merely gated --


def test_an_unhomed_agent_cannot_be_created_at_all():
    """This file originally carried three tests proving an unhomed agent 404s
    for a stranger, for an existing run's read path, and for an unrelated
    workspace owner. All three constructed their subject with
    `Agent.objects.create(slug=..., name=...)` — workspace=None — and that is
    now an IntegrityError, so none of them can express their own precondition.

    They are replaced by this one rather than deleted quietly, because the
    security property did not go away: it moved from a runtime gate to a
    schema constraint (agents migration 0013, `Agent.workspace` NOT NULL).
    That constraint is what closed the whole bug class — a nullable FK read as
    "allow" in a tenancy predicate, found eight times across this codebase.

    The runtime gate in `_get_agent_or_404` is deliberately KEPT as
    defense-in-depth and is still exercised, against a homed agent in another
    tenant, by the stranger_* tests above. Model-level coverage of the
    constraint itself lives in tests/test_agent_workspace_not_null.py."""
    with pytest.raises(IntegrityError):
        Agent.objects.create(slug="legacy", name="Legacy")


# --- positive control: a homed agent keeps working for its members ---------


def test_homed_agent_run_surface_still_reachable_by_its_member(owner_client, agent):
    """The fix must not break the intended path: a real member of the agent's
    workspace can still create/list/get/gate/fork runs."""
    run_id = _create_run(owner_client).json()["id"]
    assert owner_client.get(f"/api/agents/echo/runs/{run_id}/").status_code == 200
    assert owner_client.get(f"/api/agents/echo/runs/{run_id}/steps/").status_code == 200
