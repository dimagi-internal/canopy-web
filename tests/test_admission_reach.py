"""Turns no runner will take are rejected and their owner told, not left queued.

The incident (2026-10-12): Ada parked haldimagi-mbp-cdp for a weekly usage limit
with no resume time. Work only that box could take would have sat QUEUED with
nobody told, and Supervisor's banner was the only trace of it. The seeded
`no-runner-paused` rule now rejects such a turn the moment it arrives (or the
moment the box is paused under it), naming who paused which box, when and why.
A pause WITH a resume time is a hold: the turn waits, and its status says until
when. A box that is merely down keeps its work queued for the
`no-runner-offline` rule's wait (24h), then the same rejection.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.admission import evaluate
from apps.admission.models import AdmissionNotice, AdmissionRule
from apps.agents.models import Agent
from apps.harness import initiator as _initiator
from apps.harness import services, turn_status
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.harness.routing import ENVELOPE_VERSION, PROFILES_VERSION
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
CAPS = {"sessions": True, "profiles": PROFILES_VERSION, "envelope": ENVELOPE_VERSION}


@pytest.fixture
def fleet():
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com", first_name="Jonathan",
                                  last_name="Jackson")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    M.objects.create(workspace=ws, user=jj, role=M.OWNER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, turn_mode=Agent.AUTO)
    box = enrolled(Runner.objects.create(
        name="haldimagi-mbp-cdp", kind=Runner.EMDASH, owner=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities=CAPS))
    RunnerAssignment.objects.create(agent=hal, runner=box, rank=0)
    client = Client()
    client.force_login(jj)
    return {"jj": jj, "ws": ws, "hal": hal, "box": box, "client": client}


def _ask(agent, key="k-1"):
    turn, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API, prompt="hi",
                                    idempotency_key=key, initiator=_initiator.system(via="test"))
    turn.refresh_from_db()
    return turn


def _pause(fleet, **body):
    r = fleet["client"].post(f"/api/harness/runners/{fleet['box'].pk}/pause", body,
                             content_type="application/json")
    assert r.status_code == 200, r.content
    fleet["box"].refresh_from_db()
    return r.json()


def _rule(ws, key):
    return AdmissionRule.objects.get(workspace=ws, seed_key=key)


def test_the_pause_records_who_paused_and_unpause_clears_it(fleet):
    body = _pause(fleet, note="weekly usage limit")
    assert fleet["box"].paused_by == fleet["jj"] and body["paused_by_email"] == "jj@dimagi.com"
    r = fleet["client"].post(f"/api/harness/runners/{fleet['box'].pk}/unpause")
    assert r.status_code == 200 and r.json()["paused_by_email"] is None


def test_a_turn_only_an_open_ended_paused_box_could_take_is_rejected_on_arrival(
        fleet, django_capture_on_commit_callbacks):
    _pause(fleet, note="weekly usage limit")
    with django_capture_on_commit_callbacks(execute=True):
        turn = _ask(fleet["hal"])

    rule = _rule(fleet["ws"], "no-runner-paused")
    assert turn.status == Turn.CANCELLED
    assert turn.result_note.startswith(f"rejected: admission rule {rule.pk} ")
    # Who, which box, when, why, and that nothing will resume it.
    assert "haldimagi-mbp-cdp was paused by Jonathan Jackson on" in turn.result_note
    assert "(weekly usage limit), with no time set to resume" in turn.result_note
    owner = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.OWNER)
    assert owner.status == AdmissionNotice.SENT
    # The asker reads the same reason on the turn's status, not "Stopped."
    st = turn_status.resolve(turn)
    assert st.state == turn_status.CANCELLED and "paused by Jonathan Jackson" in st.detail


def test_a_pause_with_a_resume_time_holds_the_turn_and_says_until_when(fleet):
    until = timezone.now() + dt.timedelta(hours=3)
    _pause(fleet, note="weekly usage limit", until=until.isoformat())
    turn = _ask(fleet["hal"])

    assert turn.status == Turn.QUEUED
    st = turn_status.resolve(turn)
    assert (st.state, st.parked) == (turn_status.WAITING_RUNNER, True)
    assert abs((st.held_until - until).total_seconds()) < 1


def test_pausing_open_ended_rejects_the_work_already_queued_for_that_box(
        fleet, django_capture_on_commit_callbacks):
    box = fleet["box"]
    box.last_heartbeat_at = timezone.now() - dt.timedelta(hours=1)  # asleep: the turn queues
    box.save(update_fields=["last_heartbeat_at"])
    turn = _ask(fleet["hal"])
    assert turn.status == Turn.QUEUED

    with django_capture_on_commit_callbacks(execute=True):
        _pause(fleet, note="parked for the night")
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED and "parked for the night" in turn.result_note


def test_a_turn_another_live_runner_can_take_is_left_alone(fleet, django_capture_on_commit_callbacks):
    other = enrolled(Runner.objects.create(
        name="cloud-ec2-2", kind=Runner.CLOUD, owner=fleet["jj"], status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities=CAPS))
    RunnerAssignment.objects.create(agent=fleet["hal"], runner=other, rank=1)
    _pause(fleet)
    with django_capture_on_commit_callbacks(execute=True):
        turn = _ask(fleet["hal"])
    assert turn.status == Turn.QUEUED


def test_an_admin_who_disables_the_rule_gets_the_old_behaviour_back(fleet):
    AdmissionRule.objects.filter(workspace=fleet["ws"], reach__in=["paused", "offline"]).update(
        enabled=False)
    _pause(fleet)
    assert _ask(fleet["hal"]).status == Turn.QUEUED


def test_a_box_that_is_merely_down_keeps_its_work_for_the_wait_then_the_sweep_rejects_it(
        fleet, django_capture_on_commit_callbacks):
    box = fleet["box"]
    box.last_heartbeat_at = timezone.now() - dt.timedelta(hours=30)
    box.save(update_fields=["last_heartbeat_at"])
    turn = _ask(fleet["hal"])
    assert turn.status == Turn.QUEUED  # down is not paused: it may come back

    evaluate._last_sweep = None
    assert evaluate.sweep_unrunnable(timezone.now()) == 0  # not waited a day yet
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(hours=25))
    evaluate._last_sweep = None
    with django_capture_on_commit_callbacks(execute=True):
        assert evaluate.sweep_unrunnable(timezone.now()) == 1
    turn.refresh_from_db()
    rule = _rule(fleet["ws"], "no-runner-offline")
    assert turn.status == Turn.CANCELLED and f"admission rule {rule.pk} " in turn.result_note
    assert "haldimagi-mbp-cdp has not been heard from since" in turn.result_note


def test_the_stuck_turn_warning_says_parked_and_names_who(fleet):
    _pause(fleet, until=(timezone.now() + dt.timedelta(hours=2)).isoformat())
    turn = _ask(fleet["hal"])
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(minutes=10))
    [row] = services.unclaimable_queued_turns(fleet["jj"])
    assert row["kind"] == "parked" and "paused by Jonathan Jackson" in row["reason"]


def test_a_reach_rule_cannot_be_an_allow_or_narrowed_by_the_claiming_runner(fleet):
    from django.core.exceptions import ValidationError

    for bad in ({"action": AdmissionRule.ALLOW}, {"runner_kind": "cloud"}):
        with pytest.raises(ValidationError):
            AdmissionRule(workspace=fleet["ws"], reach=AdmissionRule.PAUSED, reason="x",
                          **bad).save()
