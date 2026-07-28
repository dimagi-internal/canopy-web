"""The source vocabulary: six values, legacy aliases normalized at the boundary.

`origin` is now a ROUTING input (spec 2026-07-27), so the set of values a caller
may supply is deliberately narrower than the set the column holds.
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Item, Turn
from apps.harness.schemas import ItemIn, TurnIn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def test_the_vocabulary_is_exactly_six_values():
    assert {v for v, _label in Turn.ORIGIN_CHOICES} == {
        "api", "ace_web", "canopy_web_chat", "canopy_scheduler", "email", "slack",
    }


def test_server_only_origins_are_not_postable():
    assert Turn.POSTABLE_ORIGINS == {"api", "ace_web", "email", "slack"}
    for server_only in ("canopy_web_chat", "canopy_scheduler"):
        with pytest.raises(ValueError):
            TurnIn(agent_slug="echo", origin=server_only, idempotency_key="k")


def test_a_caller_may_post_a_source_value():
    assert TurnIn(agent_slug="echo", origin="ace_web", idempotency_key="k").origin == "ace_web"


@pytest.mark.parametrize(
    "legacy,expected",
    [("board", "api"), ("manual", "api"), ("drill", "api"), ("cron", "canopy_scheduler")],
)
def test_legacy_origins_normalize_rather_than_422(legacy, expected):
    """The live fleet posts these today. Rejecting them would 422 Echo/Ada mid-flight,
    so they normalize to their migration target for one release."""
    assert TurnIn(agent_slug="echo", origin=legacy, idempotency_key="k").origin == expected
    assert ItemIn(title="t", origin=legacy, idempotency_key="k").origin == expected


def test_an_unknown_origin_is_still_rejected():
    with pytest.raises(ValueError):
        TurnIn(agent_slug="echo", origin="wat", idempotency_key="k")


def test_routable_sources_exclude_nothing_produced_and_include_the_real_cases():
    assert set(Turn.ROUTABLE_ORIGINS) == {
        "ace_web", "email", "canopy_scheduler", "canopy_web_chat", "slack", "api",
    }


def test_both_origin_columns_hold_the_longest_value():
    """`canopy_scheduler` is 16 chars; the column was max_length=10."""
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key="k1"
    )
    item = Item.objects.create(
        agent=agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, title="t", idempotency_key="i1"
    )
    turn.refresh_from_db()
    item.refresh_from_db()
    assert turn.origin == item.origin == "canopy_scheduler"


def test_a_stored_dispatch_spec_with_a_retired_origin_still_enqueues_a_valid_one():
    """Items raised before this deploy carry origin="manual" in their dispatch JSON,
    and TurnSpec.from_dict hands it straight to enqueue_turn without a schema in the
    path. A turn born with a retired origin matches no rule and no log filter."""
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    turn, _ = services.enqueue_turn(agent=agent, origin="manual", idempotency_key="legacy-1")
    assert turn.origin == Turn.ORIGIN_API
