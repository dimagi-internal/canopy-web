"""Every terminal transition of a turn goes through `harness.closing.close_turn`,
so every one of them reaches analytics as exactly one `turn.finished` fact with
the status it ended in — a stop, a rider's end, an admission refusal and an
expired lease included, not only a runner's finish."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.events import activity
from apps.events.models import Activity
from apps.harness import services
from apps.harness.claim import sweep_expired_leases
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=jj)
    box = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now())
    return {"agent": agent, "box": box}


def _turn(ctx, key, **fields):
    return Turn.objects.create(agent=ctx["agent"], origin=Turn.ORIGIN_API, idempotency_key=key,
                               **fields)


def _finished(turn) -> list[str]:
    return list(Activity.objects.filter(kind=activity.TURN_FINISHED, ref=str(turn.pk))
                .values_list("meta__status", flat=True))


def test_cancelling_a_queued_turn_is_one_cancelled_fact(ctx):
    turn = _turn(ctx, "q1")
    assert services.cancel_turn(turn, by="jj") is not None
    assert turn.status == Turn.CANCELLED
    assert _finished(turn) == [Turn.CANCELLED]
    assert services.stopped_by(turn) == "jj"
    # A second press changes nothing and records nothing.
    services.cancel_turn(turn, by="jj")
    assert _finished(turn) == [Turn.CANCELLED]


def test_a_rider_ends_with_its_turn_as_its_own_fact(ctx):
    holder = _turn(ctx, "h1", status=Turn.RUNNING, claimed_by=ctx["box"])
    # A project turn: one agent holds one executing turn at a time.
    rider = Turn.objects.create(project="canopy-web", workspace=ctx["agent"].workspace,
                                origin=Turn.ORIGIN_API, idempotency_key="r1", status=Turn.CLAIMED,
                                claimed_by=ctx["box"], rides_turn=holder)

    services.finish_turn(holder, status=Turn.DONE, result_note="ok")

    rider.refresh_from_db()
    assert rider.status == Turn.DONE
    assert _finished(holder) == [Turn.DONE]
    assert _finished(rider) == [Turn.DONE]


def test_an_expired_lease_is_one_lost_fact(ctx):
    turn = _turn(ctx, "l1", status=Turn.RUNNING, claimed_by=ctx["box"],
                 lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert sweep_expired_leases() == 1
    turn.refresh_from_db()
    assert turn.status == Turn.LOST
    assert _finished(turn) == [Turn.LOST]
    assert sweep_expired_leases() == 0
    assert _finished(turn) == [Turn.LOST]


def test_an_admission_refusal_is_one_cancelled_fact(ctx, django_capture_on_commit_callbacks):
    from apps.admission import evaluate
    from apps.admission.models import AdmissionRule

    rule = AdmissionRule.objects.create(workspace=ctx["agent"].workspace, origin="api",
                                        reason="not here", notify_sender=False)
    turn = _turn(ctx, "a1")
    with django_capture_on_commit_callbacks(execute=True):
        assert evaluate.reject(turn, rule) is True
        assert evaluate.reject(turn, rule) is False
    assert turn.status == Turn.CANCELLED
    assert _finished(turn) == [Turn.CANCELLED]
    # Its ledger still carries the one status line naming the rule.
    statuses = [e.payload for e in turn.events.filter(kind="status")]
    assert [p.get("admission_rule") for p in statuses if p.get("status") == Turn.CANCELLED] == [rule.pk]


def test_a_lost_turn_says_which_runner_and_when_it_last_heartbeat(ctx):
    """An expired lease never leaves LOST with an empty note: the note names the
    runner, its last heartbeat and whether it was still beating (it lost track of
    the turn) or went away."""
    now = timezone.now()
    beating = _turn(ctx, "l2", status=Turn.RUNNING, claimed_by=ctx["box"],
                    lease_expires_at=now - dt.timedelta(minutes=2))
    assert sweep_expired_leases() == 1
    beating.refresh_from_db()
    assert beating.status == Turn.LOST
    assert "runner box" in beating.result_note
    assert "last heartbeat" in beating.result_note
    assert "still heartbeating but stopped renewing" in beating.result_note

    ctx["box"].last_heartbeat_at = now - dt.timedelta(minutes=30)
    ctx["box"].save(update_fields=["last_heartbeat_at"])
    gone = _turn(ctx, "l3", status=Turn.RUNNING, claimed_by=ctx["box"],
                 lease_expires_at=now - dt.timedelta(minutes=1))
    assert sweep_expired_leases() == 1
    gone.refresh_from_db()
    assert "stopped heartbeating" in gone.result_note
    assert "30 min ago" in gone.result_note


def test_a_lost_turn_keeps_a_note_the_runner_already_left(ctx):
    turn = _turn(ctx, "l4", status=Turn.RUNNING, claimed_by=ctx["box"], result_note="runner said",
                 lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert sweep_expired_leases() == 1
    turn.refresh_from_db()
    assert turn.result_note == "runner said"
