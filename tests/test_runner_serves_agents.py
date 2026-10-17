"""A box prepares only the agents it serves (canopy-web#1563, fizzy 2026-10-17).

The box's credential refresh used to walk every agent its owner's token could
list. Once zero-knowledge became the vault's only mode, every agent that does
not route to that box refused it, and each refusal emailed the agent's owner a
device-proof alert: Shayoni got one for fizzy every hour from Jonathan's
cloud-ec2-2, which has never run fizzy. `GET /runners/{id}/agents` answers which
agents the box serves by the SAME rule resolve checks a proof against, so the
two cannot drift apart.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerAssignment, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name):
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=owner, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now()))


def _agents(runner):
    c = Client()
    c.force_login(runner.owner)
    return c.get(f"/api/harness/runners/{runner.pk}/agents")


@pytest.fixture
def fleet():
    """jj owns Dimagi, a cloud box and hal; shayoni owns fizzy, which routes
    only to her own laptop; eva follows the workspace order to the cloud box."""
    jj, shayoni = _user("jj"), _user("shayoni")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=shayoni,
                                       role=WorkspaceMembership.EDITOR)
    cloud, laptop = _runner(jj, "cloud-ec2-2"), _runner(shayoni, "shayoni-win-cdp")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=dimagi, owner=jj)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=shayoni)
    fizzy = Agent.objects.create(slug="fizzy", name="Fizzy", workspace=dimagi, owner=shayoni)
    RunnerAssignment.objects.create(agent=fizzy, runner=laptop, rank=0)
    WorkspaceRunnerOrder.objects.create(workspace=dimagi, runner=cloud, rank=0)
    return cloud, laptop, {"hal": hal, "eva": eva, "fizzy": fizzy}


def test_a_box_lists_the_agents_it_serves_and_not_someone_elses(fleet):
    cloud, _laptop, _agents_by_slug = fleet
    r = _agents(cloud)
    assert r.status_code == 200, r.content
    # hal: its owner owns the box. eva: follows the workspace order to it.
    # fizzy: routes to Shayoni's laptop only, so this box must not ask for it.
    assert r.json() == ["eva", "hal"]


def test_the_list_is_exactly_the_rule_resolve_checks_a_proof_against(fleet):
    for runner in fleet[:2]:
        listed = set(_agents(runner).json())
        for slug, agent in fleet[2].items():
            assert (slug in listed) == device_keys.runner_serves_agent(runner, agent), (
                runner.name, slug)


def test_a_retired_box_serves_nothing(fleet):
    cloud = fleet[0]
    Runner.objects.filter(pk=cloud.pk).update(status=Runner.RETIRED)
    cloud.refresh_from_db()
    assert list(device_keys.agents_served(cloud)) == []


def test_someone_elses_box_is_not_yours_to_ask_about(fleet):
    cloud, laptop, _ = fleet
    c = Client()
    c.force_login(laptop.owner)
    assert c.get(f"/api/harness/runners/{cloud.pk}/agents").status_code == 404
