"""Migration 0072: today's per-agent review grants become the program-level shape without changing who
can read whom, and the agents the product names as coaches get a role in their own workspace."""
from __future__ import annotations

import importlib

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole, SessionReviewGrant
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
copy = importlib.import_module("apps.canopy_sessions.migrations.0072_copy_review_grants_to_coaching").copy


def test_live_grants_become_one_opt_in_per_person_and_a_role_per_agent():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=owner)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    gone = User.objects.create_user("gone", "gone@dimagi.com", "pw")
    SessionReviewGrant.objects.create(user=lili, agent=trellis)
    SessionReviewGrant.objects.create(user=lili, agent=hal)                      # two agents, one person
    SessionReviewGrant.objects.create(user=gone, agent=trellis, revoked_at=timezone.now())   # opted out already
    with override_settings(COACHING_AGENT_SLUGS="trellis, ada"):
        copy(apps, None)
        copy(apps, None)                                                           # idempotent
    assert list(CoachingOptIn.objects.filter(revoked_at__isnull=True).values_list("user__username", flat=True)) == ["lili"]
    assert not CoachingOptIn.objects.filter(user=gone).exists()
    assert {r.agent.slug for r in CoachingRole.objects.filter(revoked_at__isnull=True)} == {"trellis", "hal", "ada"}
    assert CoachingRole.objects.count() == 3
    assert ada.workspace_id == "dimagi"
