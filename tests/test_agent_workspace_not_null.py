"""`Agent.workspace` is NOT NULL — the constraint that ends a bug class.

Six tenancy predicates independently grew a `workspace_id IS NULL` leg that
reads as ALLOW, because a nullable tenant FK invites
`if agent.workspace_id and <membership check>` — which short-circuits to
"ungated" on precisely the row that declares no tenant. Four were fixed one
site at a time (PRs #378, #421, #423) before it was clear the recurrence was
the defect. `agents/0013` removes the state instead of the symptom.

This file is where the regression now lives. It SUPERSEDES the per-surface
"an unhomed agent is invisible here" tests that used to sit in
tests/test_agent_out_workspace.py, tests/test_agents_turns_api.py,
tests/test_agent_runners_api.py, tests/test_harness_authz.py,
tests/test_harness_transcript_api.py and tests/test_schedule_services_crud.py:
each of them constructed an unhomed agent and asserted one surface refused it,
which cannot be written any more — and which was always the weaker claim.
Those files keep their CROSS-TENANT tests, which are the ones with something
left to prove.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def test_an_agent_cannot_be_created_without_a_workspace():
    """The whole point: the fail-open state is unrepresentable, not merely
    unpopulated. Every predicate downstream is allowed to assume this."""
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Agent.objects.create(slug="orphan", name="Orphan")


def test_an_agent_cannot_be_un_homed_after_the_fact():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    agent.workspace = None
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            agent.save(update_fields=["workspace"])


def test_the_upsert_service_requires_a_tenant_up_front():
    """`services.upsert_agent` takes `workspace` as a required keyword rather
    than creating the row and letting the view home it a few lines later. That
    gap — an agent that exists unhomed for the length of a request — is where
    an unhomed row could still appear if a caller 4xx'd in between."""
    from types import SimpleNamespace

    from apps.agents import services

    payload = SimpleNamespace(
        slug="echo", name="Echo", description="", persona="", email="", avatar_url=""
    )
    with pytest.raises(TypeError):
        services.upsert_agent(payload)


def test_upsert_homes_on_create_and_never_moves_an_existing_agent():
    """`workspace` is a CREATE default, not an update one: re-registering an
    agent (the plugin does this on every sync) must not silently drag it into
    whichever tenant the caller happens to be pinned to."""
    from types import SimpleNamespace

    from apps.agents import services

    home = a_workspace("acme")
    elsewhere = a_workspace("other")
    payload = SimpleNamespace(
        slug="echo", name="Echo", description="", persona="", email="", avatar_url=""
    )

    created = services.upsert_agent(payload, workspace=home)
    assert created.workspace_id == "acme"

    payload.name = "Echo v2"
    again = services.upsert_agent(payload, workspace=elsewhere)
    assert again.pk == created.pk
    assert again.name == "Echo v2"
    assert again.workspace_id == "acme"  # not moved


def test_a_schedule_always_has_a_tenant_through_its_agent():
    """`week_schedules` and `_runner_schedule_qs` both gate on
    `agent__workspace_id`, and both dropped their isnull leg. That is only safe
    because the traversal can never land on NULL."""
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=a_workspace())
    schedule = AgentSchedule.objects.create(
        agent=agent, name="weekly", prompt="/eva:report",
        cron="0 9 * * 5", timezone="America/New_York",
    )
    assert AgentSchedule.objects.filter(agent__workspace_id__isnull=True).count() == 0
    assert schedule.agent.workspace_id is not None
