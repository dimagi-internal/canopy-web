"""A queued turn says why it is queued.

The incident (2026-10-30): an ACE dispatch pinned to an online, ready laptop sat queued five
hours while every surface said a live runner was about to take it. They were reading the
heartbeat; nothing recorded whether the runner was asking for work at all.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import claim, waiting
from apps.harness.models import FleetHold, Runner, RunnerAssignment, Turn, TurnEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=now, capabilities={})
    cloud = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                  status=Runner.ONLINE, last_heartbeat_at=now, capabilities={})
    RunnerAssignment.objects.create(agent=ace, runner=cloud, rank=0)
    return {"ws": ws, "agent": ace, "laptop": laptop, "cloud": cloud}


def _queued(agent, *, key="k1", age_s=600, pinned=None):
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key,
                               routing=Turn.PREFER_LOCAL, pinned_runner=pinned)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age_s))
    turn.refresh_from_db()
    return turn


def _waiting(turn):
    return [e.payload for e in TurnEvent.objects.filter(turn=turn, kind="waiting").order_by("seq")]


def test_asking_for_work_is_recorded_apart_from_the_heartbeat(fleet):
    laptop = fleet["laptop"]
    assert laptop.last_claim_poll_at is None
    claim.claim_next_turn(laptop)
    laptop.refresh_from_db()
    assert laptop.last_claim_poll_at is not None
    first = laptop.last_claim_poll_at
    claim.claim_next_turn(laptop)  # inside the throttle: no second write
    laptop.refresh_from_db()
    assert laptop.last_claim_poll_at == first


def test_a_pinned_turn_on_a_runner_that_heartbeats_but_never_asks_says_so(fleet):
    laptop = fleet["laptop"]
    Runner.objects.filter(pk=laptop.pk).update(last_claim_poll_at=timezone.now() - dt.timedelta(hours=5))
    turn = _queued(fleet["agent"], pinned=laptop)

    assert waiting.note_waiting_turns() == 1

    (note,) = _waiting(turn)
    assert "jj-mbp is online" in note["why"]
    assert "has not asked for work for 5h" in note["why"]
    assert "heartbeat alone does not take turns" in note["why"]


def test_a_runner_with_no_poll_on_record_says_that(fleet):
    turn = _queued(fleet["agent"], pinned=fleet["laptop"])
    waiting.note_waiting_turns()
    assert "no record of it ever asking for work" in _waiting(turn)[0]["why"]


def test_a_box_that_passes_a_turn_over_says_why_on_the_turn(fleet):
    RunnerAssignment.objects.create(agent=fleet["agent"], runner=fleet["laptop"], rank=1)
    turn = _queued(fleet["agent"], age_s=5)
    # The cloud box is ranked first and is up, so the laptop must leave it alone — and say so.
    assert claim.claim_next_turn(fleet["laptop"]) is None
    (skip,) = [e for e in _waiting(turn) if "skipped" in e]
    assert skip["runner"] == "jj-mbp"
    assert "not in ace's runner order" in skip["skipped"]
    # ...once, not on every poll.
    Runner.objects.filter(pk=fleet["laptop"].pk).update(last_claim_poll_at=None)
    claim.claim_next_turn(Runner.objects.get(pk=fleet["laptop"].pk))
    assert len([e for e in _waiting(turn) if "skipped" in e]) == 1


def test_a_runner_that_is_asking_and_skipping_is_named_with_the_reason(fleet):
    turn = _queued(fleet["agent"], pinned=fleet["laptop"], age_s=600)
    TurnEvent.objects.create(turn=turn, seq=1, kind="waiting",
                             payload={"runner": "jj-mbp", "skipped": "no agent CLI on this box can run it"})
    Runner.objects.filter(pk=fleet["laptop"].pk).update(last_claim_poll_at=timezone.now())
    waiting.note_waiting_turns()
    why = [e for e in _waiting(turn) if "why" in e][0]["why"]
    assert "is asking for work and is passing this turn over: no agent CLI" in why


def test_an_unchanged_explanation_is_not_repeated_until_it_is_old(fleet):
    turn = _queued(fleet["agent"], pinned=fleet["laptop"])
    assert waiting.note_waiting_turns() == 1
    assert waiting.note_waiting_turns() == 0
    later = timezone.now() + waiting.REPEAT_EVERY + dt.timedelta(seconds=1)
    assert waiting.note_waiting_turns(now=later) == 1
    assert len(_waiting(turn)) == 2


def test_a_young_turn_is_left_alone(fleet):
    _queued(fleet["agent"], age_s=10, pinned=fleet["laptop"])
    assert waiting.note_waiting_turns() == 0


def test_a_fleet_hold_is_named(fleet):
    turn = _queued(fleet["agent"])
    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK, defaults={"held": True})
    waiting.note_waiting_turns()
    assert "whole fleet is on hold" in _waiting(turn)[0]["why"]


def test_an_offline_runner_is_named(fleet):
    Runner.objects.filter(pk=fleet["cloud"].pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=2))
    turn = _queued(fleet["agent"])
    waiting.note_waiting_turns()
    assert "No runner that could take it is online" in _waiting(turn)[0]["why"]
