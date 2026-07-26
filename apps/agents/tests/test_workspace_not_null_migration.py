"""Where `agents/0013` sends an unhomed agent.

Production has zero unhomed agents, so the migration is a no-op there and the
interesting half is `_resolve_target` — the rule a dev box, a staging DB or a
restored snapshot WOULD be backfilled by. Tested directly against the live app
registry (the function only reads), because the NOT NULL column it exists to
enable makes the row it operates on unconstructible once the migration has run.

Each case pins one step of the "most-evidenced first" ladder documented in the
migration, so a later edit cannot quietly swap the order.
"""
from __future__ import annotations

import importlib

import pytest
from django.apps import apps as global_apps
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()

_mod = importlib.import_module("apps.agents.migrations.0013_agent_workspace_not_null")


def _user(name="su"):
    return User.objects.create(username=name, email=f"{name}@dimagi.com", is_superuser=True)


def _ws(slug, owner):
    return Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)


def test_prefers_the_workspace_the_deployments_agents_already_live_in():
    """Step 1: the modal home of already-homed agents. A stray unhomed row is
    one that escaped 0007 or was hand-created after it; its siblings' tenant is
    the answer the data gives."""
    owner = _user()
    busy, quiet = _ws("connect", owner), _ws("dimagi", owner)
    Agent.objects.create(slug="ace", name="Ace", workspace=busy)
    Agent.objects.create(slug="ada", name="Ada", workspace=busy)
    Agent.objects.create(slug="echo", name="Echo", workspace=quiet)

    assert _mod._resolve_target(global_apps) == "connect"


def test_modal_tie_breaks_deterministically_on_slug():
    """Replicas and repeated runs must agree; an arbitrary tie-break would let
    two environments diverge on the same data."""
    owner = _user()
    a, b = _ws("alpha", owner), _ws("beta", owner)
    Agent.objects.create(slug="one", name="One", workspace=a)
    Agent.objects.create(slug="two", name="Two", workspace=b)

    assert _mod._resolve_target(global_apps) == "alpha"


def test_falls_back_to_the_sole_workspace_when_no_agent_is_homed():
    """Step 2: a dev DB whose single tenant is not called `dimagi`. There is no
    other candidate, so there is nothing to get wrong — and inventing a second
    `dimagi` workspace beside it would be the wrong answer."""
    owner = _user()
    _ws("acme", owner)

    assert _mod._resolve_target(global_apps) == "acme"
    assert not Workspace.objects.filter(slug="dimagi").exists()


def test_falls_back_to_the_default_workspace_when_there_is_no_evidence(settings):
    """Step 3: several workspaces, not one homed agent. Falls to the same
    target 0007 picked for every agent in this deployment's history — creating
    it exactly as 0007 does, including auto_join_domains."""
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"
    owner = _user()
    _ws("alpha", owner)
    _ws("beta", owner)

    assert _mod._resolve_target(global_apps) == "dimagi"
    created = Workspace.objects.get(slug="dimagi")
    assert created.created_by_id == owner.pk
    assert created.auto_join_domains == ["dimagi.com"]


def test_prefers_an_existing_default_workspace_over_creating_one():
    owner = _user()
    _ws("alpha", owner)
    _ws("beta", owner)
    _ws("dimagi", owner)

    assert _mod._resolve_target(global_apps) == "dimagi"
    assert Workspace.objects.filter(slug="dimagi").count() == 1


def test_raises_a_readable_error_when_there_is_nothing_to_home_to():
    """Step 4: no workspace and no user to own one. A Workspace requires a
    created_by, so this is genuinely unresolvable — fail with instructions
    rather than let the AlterField throw an opaque NOT NULL violation."""
    with pytest.raises(RuntimeError, match="no user"):
        _mod._resolve_target(global_apps)


def test_home_unhomed_agents_is_a_no_op_with_nothing_to_do():
    """The production path. It must not create a default workspace as a side
    effect of finding nothing wrong."""
    _mod.home_unhomed_agents(global_apps, None)
    assert Workspace.objects.count() == 0
