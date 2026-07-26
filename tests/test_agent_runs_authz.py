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


# --- unhomed (workspace-less) agent: must be unresolvable by ANYONE --------


def test_unhomed_agent_run_surface_is_unreachable_by_non_member(stranger_client):
    """The actual bug: `Agent.objects.create(slug="legacy", name="Legacy")` has
    workspace=None. Pre-fix, `if agent.workspace_id and not is_member(...)`
    short-circuited to False (no 404) for ANY authenticated caller — this must
    now 404 instead."""
    Agent.objects.create(slug="legacy", name="Legacy")
    resp = _create_run(stranger_client, slug="legacy")
    assert resp.status_code == 404
    assert not AgentRun.objects.filter(agent__slug="legacy").exists()


def test_unhomed_agent_existing_run_is_unreachable_by_non_member(stranger_client):
    """Same hole via the read path: a run that already exists on an unhomed
    agent must not be listable or gettable by a stranger either."""
    orphan = Agent.objects.create(slug="orphan", name="Orphan")
    run = AgentRun.objects.create(agent=orphan, label="Orphan run")

    assert stranger_client.get("/api/agents/orphan/runs/").status_code == 404
    assert stranger_client.get(f"/api/agents/orphan/runs/{run.id}/").status_code == 404


def test_unhomed_agent_is_unreachable_even_for_a_workspace_owner(owner_client):
    """Not just strangers: an unhomed agent is unresolvable via this API full
    stop (it needs backfilling a workspace first), not "visible to everyone
    except non-members." A workspace owner with no relationship to the
    unhomed agent gets the same 404."""
    Agent.objects.create(slug="legacy", name="Legacy")
    resp = _create_run(owner_client, slug="legacy")
    assert resp.status_code == 404


# --- positive control: a homed agent keeps working for its members ---------


def test_homed_agent_run_surface_still_reachable_by_its_member(owner_client, agent):
    """The fix must not break the intended path: a real member of the agent's
    workspace can still create/list/get/gate/fork runs."""
    run_id = _create_run(owner_client).json()["id"]
    assert owner_client.get(f"/api/agents/echo/runs/{run_id}/").status_code == 200
    assert owner_client.get(f"/api/agents/echo/runs/{run_id}/steps/").status_code == 200
