"""harness 0058: fold old report-only close-out rows into their turns — and only
when the turn is certain. Shapes taken from labs (echo, 2026-10-01/02)."""
from __future__ import annotations

import datetime as dt
import importlib

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, Turn, TurnEvent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

merge = importlib.import_module(
    "apps.harness.migrations.0058_merge_orphan_closeouts").merge_orphan_closeouts

T0 = timezone.now() - dt.timedelta(days=1)


@pytest.fixture()
def world(django_user_model):
    user = django_user_model.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=user)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    cloud = Runner.objects.create(name="cloud-ec2-1", kind="cloud", capabilities={}, owner=user)
    laptop = Runner.objects.create(name="jj-mbp", kind="emdash", capabilities={}, owner=user)
    return echo, cloud, laptop


def _dispatch(agent, runner, key, start, end, session_key=""):
    return Turn.objects.create(
        agent=agent, origin="canopy_scheduler", idempotency_key=key, status=Turn.DONE, claimed_by=runner,
        started_at=start, finished_at=end, session_key=session_key, prompt="/echo:turn")


def _report(agent, cli, at, title="Scheduled turn — nothing in queue"):
    t = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, status=Turn.DONE,
        idempotency_key=f"closeout:{agent.slug}:{cli}", cli_session_id=cli,
        report_title=title, report_source="turn", reported_at=at)
    Turn.objects.filter(pk=t.pk).update(created_at=at)
    return t


def test_an_old_cloud_pair_is_merged_by_its_running_window(world):
    echo, cloud, _ = world
    turn = _dispatch(echo, cloud, "d1", T0, T0 + dt.timedelta(seconds=84))
    report = _report(echo, "772b76a6", T0 + dt.timedelta(seconds=79))

    assert merge(Turn, TurnEvent) == 1
    assert not Turn.objects.filter(pk=report.pk).exists()
    turn.refresh_from_db()
    assert turn.report_title == "Scheduled turn — nothing in queue"
    assert turn.cli_session_id == turn.session_key == "772b76a6"
    assert turn.reported_at is not None


def test_a_keyed_pair_is_merged_exactly(world):
    echo, cloud, _ = world
    turn = _dispatch(echo, cloud, "d2", T0, T0 + dt.timedelta(minutes=2), session_key="27629448")
    _report(echo, "27629448", T0 + dt.timedelta(minutes=1))
    assert merge(Turn, TurnEvent) == 1
    turn.refresh_from_db()
    assert turn.reported_at is not None


def test_two_overlapping_cloud_turns_are_left_alone(world):
    echo, cloud, _ = world
    _dispatch(echo, cloud, "d3", T0, T0 + dt.timedelta(minutes=5))
    _dispatch(echo, cloud, "d4", T0 + dt.timedelta(minutes=1), T0 + dt.timedelta(minutes=6))
    _report(echo, "ambiguous", T0 + dt.timedelta(minutes=2))
    assert merge(Turn, TurnEvent) == 0
    assert Turn.objects.filter(idempotency_key="closeout:echo:ambiguous").exists()


def test_a_laptop_turn_is_never_guessed(world):
    """A laptop dispatch finishes seconds in; its report lands long after."""
    echo, _, laptop = world
    _dispatch(echo, laptop, "d5", T0, T0 + dt.timedelta(seconds=6))
    _report(echo, "laptop-claude", T0 + dt.timedelta(minutes=3))
    assert merge(Turn, TurnEvent) == 0


def test_a_report_outside_any_turn_stays(world):
    echo, cloud, _ = world
    _dispatch(echo, cloud, "d6", T0, T0 + dt.timedelta(minutes=1))
    _report(echo, "by-hand", T0 + dt.timedelta(hours=3))
    assert merge(Turn, TurnEvent) == 0


def test_an_already_reported_turn_is_not_taken(world):
    echo, cloud, _ = world
    taken = _dispatch(echo, cloud, "d7", T0, T0 + dt.timedelta(minutes=2))
    Turn.objects.filter(pk=taken.pk).update(reported_at=T0, report_title="its own")
    _report(echo, "late", T0 + dt.timedelta(minutes=1))
    assert merge(Turn, TurnEvent) == 0
    taken.refresh_from_db()
    assert taken.report_title == "its own"


def test_a_report_row_with_its_own_events_is_never_deleted(world):
    echo, cloud, _ = world
    _dispatch(echo, cloud, "d8", T0, T0 + dt.timedelta(minutes=2))
    report = _report(echo, "evented", T0 + dt.timedelta(minutes=1))
    TurnEvent.objects.create(turn=report, seq=1, kind="status", payload={})
    assert merge(Turn, TurnEvent) == 0


def test_running_it_twice_changes_nothing_more(world):
    echo, cloud, _ = world
    _dispatch(echo, cloud, "d9", T0, T0 + dt.timedelta(minutes=2))
    _report(echo, "once", T0 + dt.timedelta(minutes=1))
    assert merge(Turn, TurnEvent) == 1
    assert merge(Turn, TurnEvent) == 0
