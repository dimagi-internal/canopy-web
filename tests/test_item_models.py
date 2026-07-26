"""Item model — the supervisor's queue. Dual of harness.Turn."""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(default_workspace):
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def test_item_defaults_to_open_review_with_no_decision(agent):
    item = Item.objects.create(
        agent=agent, kind=Item.REVIEW, title="discard 81 junk emails",
        idempotency_key="k1",
    )
    assert item.state == Item.OPEN
    assert item.decision == ""
    assert item.dispatch == []
    assert item.raised_by is None


def test_idempotency_key_is_unique(agent):
    Item.objects.create(agent=agent, kind=Item.REVIEW, title="a", idempotency_key="dupe")
    with pytest.raises(IntegrityError):
        Item.objects.create(agent=agent, kind=Item.REVIEW, title="b", idempotency_key="dupe")


def test_turn_records_the_item_it_came_from(agent):
    item = Item.objects.create(agent=agent, kind=Item.REVIEW, title="a", idempotency_key="k2")
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1", raised_from=item,
    )
    assert list(item.dispatched_turns.all()) == [turn]


def test_item_records_the_turn_that_raised_it(agent):
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t2")
    item = Item.objects.create(
        agent=agent, kind=Item.REVIEW, title="a", idempotency_key="k3", raised_by=turn,
    )
    assert list(turn.raised_items.all()) == [item]
