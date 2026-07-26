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

import contextlib
import importlib
import logging

import pytest
from django.apps import apps as global_apps
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
User = get_user_model()

_mod = importlib.import_module("apps.agents.migrations.0013_agent_workspace_not_null")


class _ListHandler(logging.Handler):
    """A plain handler on the migration's OWN logger, not `caplog`: the `apps`
    logger this module's logger feeds into is configured with
    `propagate=False` (`config/settings/base.py`), which is exactly what makes
    it survive into deploy logs — but it also means records never reach the
    root logger `caplog` listens on. Attaching here observes the same records
    production would emit, without depending on caplog's propagation."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_migration_logs():
    handler = _ListHandler()
    logger = logging.getLogger(_mod.__name__)
    orig_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(orig_level)


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


def test_raises_on_a_modal_tie_naming_every_tied_workspace():
    """A tie between the top workspaces is a plurality, not a certainty — there
    is no data-derived answer, so this must fail loudly (naming both tied
    workspaces and their count) rather than silently favor whichever slug
    sorts first."""
    owner = _user()
    a, b = _ws("alpha", owner), _ws("beta", owner)
    Agent.objects.create(slug="one", name="One", workspace=a)
    Agent.objects.create(slug="two", name="Two", workspace=b)

    with pytest.raises(RuntimeError, match="alpha, beta") as exc_info:
        _mod._resolve_target(global_apps)
    assert "2 workspaces are tied" in str(exc_info.value)


def test_three_way_tie_names_all_three():
    owner = _user()
    a, b, c = _ws("alpha", owner), _ws("beta", owner), _ws("gamma", owner)
    Agent.objects.create(slug="one", name="One", workspace=a)
    Agent.objects.create(slug="two", name="Two", workspace=b)
    Agent.objects.create(slug="three", name="Three", workspace=c)

    with pytest.raises(RuntimeError, match="alpha, beta, gamma"):
        _mod._resolve_target(global_apps)


def test_a_clear_plurality_is_not_treated_as_a_tie():
    """Sanity check on the other side of the same boundary: a workspace with
    strictly more homed agents than every rival is not ambiguous, even when
    two other workspaces are themselves tied with each other for second."""
    owner = _user()
    winner = _ws("connect", owner)
    b, c = _ws("beta", owner), _ws("gamma", owner)
    Agent.objects.create(slug="a1", name="A1", workspace=winner)
    Agent.objects.create(slug="a2", name="A2", workspace=winner)
    Agent.objects.create(slug="b1", name="B1", workspace=b)
    Agent.objects.create(slug="c1", name="C1", workspace=c)

    assert _mod._resolve_target(global_apps) == "connect"


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


def test_home_unhomed_agents_logs_when_it_finds_nothing_to_do():
    """"Nothing happened" must be an observed fact in the log, not an
    assumption about a migration that printed nothing."""
    with _capture_migration_logs() as handler:
        _mod.home_unhomed_agents(global_apps, None)
    assert any("no unhomed agents" in m for m in handler.messages)


def test_home_unhomed_agents_logs_every_row_it_homes(monkeypatch):
    """The backfill must never move an agent silently: every slug it homes,
    and the workspace it lands in, has to survive into the log.

    `Agent.workspace` is NOT NULL as of this same migration (test_workspace_
    backfill.py hits the identical wall), so an actual unhomed row cannot be
    constructed against the live schema to drive `home_unhomed_agents`
    end-to-end. This pins the function's own logic — which rows it logs, and
    what it hands to `.update()` — against a minimal fake standing in for the
    historical (nullable) model the real migration runs against."""

    class _FakeUpdateQuerySet:
        def __init__(self, sink, slugs):
            self._sink = sink
            self._slugs = slugs

        def update(self, **kwargs):
            self._sink["slug__in"] = self._slugs
            self._sink["workspace_id"] = kwargs["workspace_id"]

    class _FakeManager:
        def __init__(self, unhomed_slugs, sink):
            self._unhomed_slugs = unhomed_slugs
            self._sink = sink

        def filter(self, **kwargs):
            if kwargs == {"workspace__isnull": True}:
                return self
            if "slug__in" in kwargs:
                return _FakeUpdateQuerySet(self._sink, kwargs["slug__in"])
            raise AssertionError(f"unexpected filter kwargs: {kwargs}")

        def values_list(self, field, flat=False):
            assert field == "slug" and flat is True
            return list(self._unhomed_slugs)

    class _FakeAgentModel:
        def __init__(self, unhomed_slugs, sink):
            self.objects = _FakeManager(unhomed_slugs, sink)

    class _FakeApps:
        def __init__(self, agent_model):
            self._agent_model = agent_model

        def get_model(self, app_label, model_name):
            assert (app_label, model_name) == ("agents", "Agent")
            return self._agent_model

    sink: dict = {}
    fake_apps = _FakeApps(_FakeAgentModel(["stray-one", "stray-two"], sink))
    monkeypatch.setattr(_mod, "_resolve_target", lambda apps: "connect")

    with _capture_migration_logs() as handler:
        _mod.home_unhomed_agents(fake_apps, None)

    text = "\n".join(handler.messages)
    assert "stray-one" in text
    assert "stray-two" in text
    assert text.count("connect") >= 2
    assert sink == {"slug__in": ["stray-one", "stray-two"], "workspace_id": "connect"}
