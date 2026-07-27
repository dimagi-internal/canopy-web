"""claim_next_turn, widened to repos.

The tenant rule is the delicate part and it has already caused one production
outage (#227, dc58b1b): tenancy derives from `runner.paired_by` — the human who
paired the runner — NOT from Runner.workspace, because the fleet deliberately
spans workspaces behind one laptop runner.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, **kw):
    defaults = dict(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web"]},
    )
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def test_a_projects_only_runner_claims_a_project_turn():
    """The early-return `if not slugs` would strand this runner forever: it
    declares no agents at all, so it used to bail before ever looking."""
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj)
    turn = Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1", prompt="fix the header",
    )

    claimed = services.claim_next_turn(runner)

    assert claimed is not None and claimed.pk == turn.pk
    assert claimed.status == Turn.CLAIMED


def test_a_runner_paired_by_a_non_member_cannot_claim_another_tenants_project_turn():
    """The hole the naive widening opens.

    tenant_q ungates pre-tenancy AGENTS via agent__workspace_id__isnull=True. A
    project turn has agent=NULL, so agent__workspace_id is NULL too and every
    project turn matches that leg — claimable by any runner in any tenant. The
    legs must be split by target kind.

    The attacker is TENANTED on purpose: after the section-8 backfill every real
    runner has a workspace, so an untenanted attacker tests a path that no longer
    exists in production.
    """
    jj = _user("jj")
    victim_ws = _ws("canopy", jj)

    mallory = _user("mallory")
    _ws("mallory-space", mallory)  # tenanted, just not in canopy
    attacker = _runner(mallory, name="mallory-box")

    Turn.objects.create(
        project="canopy-web", workspace=victim_ws, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1",
    )

    assert services.claim_next_turn(attacker) is None


def test_a_project_turn_with_no_workspace_is_not_claimable():
    """Fail closed. Project turns are new, so there is no legacy null-workspace
    population to grandfather in — a NULL workspace means no tenant, not 'any'."""
    jj = _user("jj")
    _ws("canopy", jj)
    runner = _runner(jj)
    Turn.objects.create(
        project="canopy-web", workspace=None, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1",
    )

    assert services.claim_next_turn(runner) is None


def test_a_busy_agent_does_not_block_a_project_turn():
    """`.exclude(agent_id__in=busy_agents)` on a nullable column is a NULL-
    semantics trap: NOT (NULL IN (...)) is NULL, not TRUE, which can silently
    drop every project turn from the candidate set."""
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj, capabilities={"agents": ["echo"], "projects": ["canopy-web"]})
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)

    # Echo is mid-turn — that must serialize ECHO, and nothing else.
    Turn.objects.create(
        agent=echo, origin=Turn.ORIGIN_BOARD, idempotency_key="a1", status=Turn.RUNNING
    )
    project_turn = Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL, idempotency_key="p1"
    )

    claimed = services.claim_next_turn(runner)

    assert claimed is not None, "a busy agent swallowed an unrelated repo turn"
    assert claimed.pk == project_turn.pk


def test_pausing_an_agent_does_not_pause_the_repos():
    """exclude_slugs is a per-AGENT pause. Pausing Echo says nothing about
    canopy-web, so repo turns keep flowing."""
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj, capabilities={"agents": ["echo"], "projects": ["canopy-web"]})
    Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    project_turn = Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL, idempotency_key="p1"
    )

    claimed = services.claim_next_turn(runner, exclude_slugs=["echo"])

    assert claimed is not None and claimed.pk == project_turn.pk


def test_a_runner_not_declaring_a_project_does_not_claim_it():
    """capabilities still routes, even though it does not gate."""
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj, capabilities={"projects": ["some-other-repo"]})
    Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL, idempotency_key="p1"
    )

    assert services.claim_next_turn(runner) is None


def test_an_executing_project_turn_does_not_block_every_agent_turn():
    """The NULL-in-NOT-IN trap: an EXECUTING *project* turn has agent_id NULL, so
    `busy_agents` contained NULL and `.exclude(agent_id__in=busy_agents)` became
    `NOT IN (..., NULL)` — never TRUE in SQL, which silently discarded EVERY
    candidate. One running repo turn made the runner claim nothing at all.

    Observed on the cloud runner: a drill sat QUEUED and pinned for 40+ minutes
    while the runner was online and heartbeating, and `POST /claim` returned 204,
    because two canopy-web project turns happened to be executing.

    The identical trap is already documented and guarded for `busy_sessions` a few
    lines below; it was simply never applied to `busy_agents`.
    """
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, workspace=ws)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)

    # A repo turn already executing on this runner (agent_id IS NULL).
    Turn.objects.create(
        project="canopy-web", workspace=ws, origin="api", prompt="repo work",
        idempotency_key="busy-project-turn", status=Turn.RUNNING,
        claimed_by=runner, claimed_at=timezone.now(),
    )
    # An unrelated agent turn, pinned to this very runner (a drill).
    queued = Turn.objects.create(
        agent=agent, workspace=ws, origin="drill", prompt="readiness drill",
        idempotency_key="drill-hal-1", status=Turn.QUEUED, pinned_runner=runner,
    )

    got = services.claim_next_turn(runner)
    assert got is not None, "an executing PROJECT turn must not block agent turns"
    assert got.pk == queued.pk


def test_an_executing_agent_turn_still_blocks_that_same_agent():
    """The real constraint must survive the fix: one executing turn per agent."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, workspace=ws)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)

    Turn.objects.create(
        agent=agent, workspace=ws, origin="api", prompt="already running",
        idempotency_key="hal-running", status=Turn.RUNNING,
        claimed_by=runner, claimed_at=timezone.now(),
    )
    Turn.objects.create(
        agent=agent, workspace=ws, origin="drill", prompt="second",
        idempotency_key="hal-second", status=Turn.QUEUED, pinned_runner=runner,
    )

    assert services.claim_next_turn(runner) is None
