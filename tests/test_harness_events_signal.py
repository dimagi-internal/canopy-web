"""SP1 Task 3 — append_events emits turn_events_appended after commit.

The signal exists because append_events uses bulk_create, which does NOT emit
post_save; a post_save receiver on TurnEvent would silently never fire.
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.harness.signals import turn_events_appended
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db(transaction=True)


def _turn():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="k1")


def test_append_events_fires_signal_after_commit():
    turn = _turn()
    received: list[dict] = []
    turn_events_appended.connect(
        lambda **kw: received.append(kw), weak=False, dispatch_uid="test-recv"
    )
    try:
        services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi"}}])
    finally:
        turn_events_appended.disconnect(dispatch_uid="test-recv")

    assert len(received) == 1
    assert received[0]["turn"].pk == turn.pk
    assert [r.seq for r in received[0]["rows"]] == [1]
    assert received[0]["rows"][0].kind == "assistant"


def test_append_events_batch_fires_once_with_all_rows():
    turn = _turn()
    received: list[dict] = []
    turn_events_appended.connect(
        lambda **kw: received.append(kw), weak=False, dispatch_uid="test-recv"
    )
    try:
        services.append_events(
            turn,
            [
                {"kind": "tool_start", "payload": {}},
                {"kind": "tool_end", "payload": {}},
            ],
        )
    finally:
        turn_events_appended.disconnect(dispatch_uid="test-recv")

    assert len(received) == 1
    assert [r.seq for r in received[0]["rows"]] == [1, 2]
