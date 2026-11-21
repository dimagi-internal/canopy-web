"""A stored agent (its definition kept in canopy-web, no GitHub repo) is servable.

The runner side of stored agents (`apps/harness/stored_agents.py`): a claim says the
turn's agent is stored and which revision is live (`ClaimedTurnOut.definition`), the
turn gets no GitHub identity, and routing never refuses a laptop for lacking an emdash
project the agent cannot have.

Stored-ness is read through `stored_agents.source_of`, patched here, so these tests hold
whether or not the agents app's `Agent.source` field has landed.
"""
from __future__ import annotations

import datetime as dt
import types
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.harness import claim, routing, stored_agents
from apps.harness.claim_payload import claim_payload
from apps.harness.models import Runner, RunnerAssignment, Turn, WorkspaceRunnerOrder
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def stored(monkeypatch):
    """Which slugs read as stored agents."""
    slugs: set[str] = set()
    monkeypatch.setattr(stored_agents, "source_of",
                        lambda a: stored_agents.STORED if a is not None and getattr(a, "slug", "") in slugs
                        else stored_agents.REPO)
    return slugs


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def fleet():
    jj = _user("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    laptop = Runner.objects.create(name="jj-laptop", kind=Runner.EMDASH, host="jj-laptop", owner=jj,
                                   workspace=ws, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   capabilities={"projects": ["ace"]})
    cloud = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="cloud-ec2-1", owner=jj,
                                  workspace=ws, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                  capabilities={})
    helper = Agent.objects.create(slug="connect-helper", name="Connect helper", workspace=ws, owner=jj,
                                  repo_url="")
    AgentAdmin.objects.get_or_create(agent=helper, user=jj)
    return types.SimpleNamespace(jj=jj, ws=ws, laptop=laptop, cloud=cloud, helper=helper)


def _turn(agent, *, key=None, age=3600, **kw):
    t = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key or uuid.uuid4().hex,
                            routing=Turn.PREFER_LOCAL, prompt="hi", **kw)
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age))
    return t


# ---- routing ---------------------------------------------------------------------

def test_a_laptop_without_an_emdash_project_still_serves_a_stored_agent(fleet, stored):
    RunnerAssignment.objects.create(agent=fleet.helper, runner=fleet.laptop, rank=0)
    t = _turn(fleet.helper)
    # A repo agent with no emdash project here: refused, as before.
    assert routing.cannot_serve(fleet.laptop, fleet.helper)
    assert claim.claim_next_turn(fleet.laptop) is None
    stored.add("connect-helper")
    assert routing.cannot_serve(fleet.laptop, fleet.helper) == ""
    assert claim.claim_next_turn(fleet.laptop).pk == t.pk


def test_a_bare_slug_is_still_read_as_a_repo_agent(fleet, stored):
    stored.add("connect-helper")
    assert "no emdash project" in routing.cannot_serve(fleet.laptop, "connect-helper")


def test_a_box_that_could_not_prepare_a_stored_agent_still_refuses_it(fleet, stored):
    stored.add("connect-helper")
    Runner.objects.filter(pk=fleet.cloud.pk).update(capabilities={"unprepared": ["connect-helper"]})
    fleet.cloud.refresh_from_db()
    assert "could not set 'connect-helper' up" in routing.cannot_serve(fleet.cloud, fleet.helper)


def test_a_followed_order_does_not_count_a_laptop_as_missing_a_stored_agents_repo(fleet, stored):
    WorkspaceRunnerOrder.objects.create(workspace=fleet.ws, runner=fleet.laptop, rank=0)
    inh = routing.inherited_orders([fleet.helper.pk])[fleet.helper.pk]
    assert [r.name for r in inh.missing_repo] == ["jj-laptop"]
    stored.add("connect-helper")
    inh = routing.inherited_orders([fleet.helper.pk])[fleet.helper.pk]
    assert inh.missing_repo == [] and [r.name for r in inh.runners] == ["jj-laptop"]


# ---- the claim -------------------------------------------------------------------

def test_the_claim_says_a_stored_agent_is_stored_and_a_repo_agent_nothing(fleet, stored):
    t = _turn(fleet.helper)
    assert claim_payload(t)["definition"] is None
    stored.add("connect-helper")
    out = claim_payload(Turn.objects.get(pk=t.pk))["definition"]
    assert out["source"] == "stored"
    assert set(out) == {"source", "revision", "root_sha256"}


def test_the_definition_names_the_live_revision():
    agent = types.SimpleNamespace(slug="x", source="stored",
                                  live_revision=types.SimpleNamespace(number=7, root_sha256="ab" * 32))
    assert stored_agents.definition_of(agent) == {"source": "stored", "revision": 7,
                                                   "root_sha256": "ab" * 32}
    unpublished = types.SimpleNamespace(slug="x", source="stored", live_revision=None)
    assert stored_agents.definition_of(unpublished)["revision"] is None
    # An agents app without the field reads as a repo agent.
    assert stored_agents.definition_of(types.SimpleNamespace(slug="x")) is None
    assert stored_agents.definition_of(None) is None


# ---- no GitHub identity --------------------------------------------------------------

def _runner_client(runner):
    c = Client()
    c.force_login(runner.owner)
    grant_step_up(c)
    return c


def test_a_stored_agents_turn_gets_no_github_identity(fleet, stored):
    stored.add("connect-helper")
    t = _turn(fleet.helper, status=Turn.CLAIMED, claimed_by=fleet.cloud)
    r = _runner_client(fleet.cloud).post(f"/api/harness/runners/{fleet.cloud.pk}/turns/{t.pk}/github-token")
    assert r.status_code == 409, r.content
    assert "stored agent" in r.json()["detail"]


def test_github_readiness_reports_a_stored_agent_as_having_nothing_to_check(fleet, stored):
    stored.add("connect-helper")
    RunnerAssignment.objects.create(agent=fleet.helper, runner=fleet.cloud, rank=0)
    r = _runner_client(fleet.cloud).get(f"/api/harness/runners/{fleet.cloud.pk}/github-readiness")
    assert r.status_code == 200, r.content
    rows = {row["agent_slug"]: row for row in r.json()}
    assert rows["connect-helper"]["status"] == "ok"
    assert "stored agent" in rows["connect-helper"]["detail"]


def test_a_laptop_is_never_asked_to_clone_a_stored_agent(fleet, stored):
    stored.add("connect-helper")
    r = _runner_client(fleet.laptop).post(
        f"/api/harness/runners/{fleet.laptop.pk}/projects", {"agent": "connect-helper"},
        content_type="application/json")
    assert r.status_code == 422, r.content
    assert "stored agent" in r.content.decode()
