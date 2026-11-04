"""A schedule can name a plain job, and its turn goes only to a cloud box able to run it.

The point of the claim gate is OLD BOXES: a cloud box that predates jobs reports no
`jobs` capability, would read the turn's empty prompt as a request, and hand it to a
model. So a job turn (and any `cloud_only` turn) reaches a box only when it is a cloud
box that said `capabilities.jobs is True` — whichever way the claim arrives (REST and
WebSocket both end in `claim_next_turn`), pinned or not, in a directed box or an
ordinary one — and never two at once on one box, since jobs share a Unix user.
canopy-web names the job and the repo and nothing else: no command rides any payload.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import claim, schedule_turns, triggers
from apps.harness import initiator as _initiator
from apps.harness import schedule_services as ss
from apps.harness import services as harness
from apps.harness.claim_payload import claim_payload
from apps.harness.routing_orders import CASCADE_GRACE_SECONDS
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

_BY_CANOPY = _initiator.system(via="test")
SLOT = dt.datetime(2026, 11, 4, 9, tzinfo=dt.UTC)
WINDOW = dt.timedelta(minutes=schedule_turns.LATE_SLOT_WINDOW_MINUTES)


@pytest.fixture()
def w():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user,
                                  access_request_domains=[])
    wsvc.ensure_member(ws, user, WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=user)
    return {"user": user, "ws": ws, "agent": agent}


def _box(w, name, *, kind=Runner.CLOUD, caps=None, directed=False):
    runner = Runner.objects.create(
        name=name, kind=kind, host=name, owner=w["user"], workspace_id=w["ws"].pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), directed=directed,
        capabilities={"sessions": True, **(caps or {})})
    RunnerAssignment.objects.create(agent=w["agent"], runner=runner,
                                    rank=RunnerAssignment.objects.filter(agent=w["agent"]).count())
    return runner


def _job_schedule(w, name="nightly", **kw):
    fields = dict(agent=w["agent"], name=name, prompt="", cron="0 9 * * *", job="nightly-sync",
                  job_repo="hal-jobs", routing=Turn.CLOUD_ONLY, grace_minutes=125,
                  created_by=w["user"])
    fields.update(kw)
    return AgentSchedule.objects.create(**fields)


def _aged(turn):
    """Past the cascade grace, so rank order between the test's boxes is not what is tested."""
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - dt.timedelta(seconds=CASCADE_GRACE_SECONDS + 5))
    return Turn.objects.get(pk=turn.pk)


def _job_turn(w, name="nightly", slot=SLOT):
    turn, _ = schedule_turns.fire_schedule(_job_schedule(w, name), slot)
    return _aged(turn)


def _ordinary_turn(w, key="o1", routing=Turn.ANY):
    turn, _ = harness.enqueue_turn(initiator=_BY_CANOPY, agent=w["agent"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                   idempotency_key=key, prompt="/hal:turn", routing=routing)
    return _aged(turn)


# --- the schedule ----------------------------------------------------------------------


@pytest.mark.parametrize("job, repo, why", [
    ("Nightly", "hal-jobs", "name"),               # upper case
    ("-lead", "hal-jobs", "name"),                 # must start alphanumeric
    ("a" * 65, "hal-jobs", "name"),                # too long
    ("has space", "hal-jobs", "name"),
    ("nightly", "owner/repo", "repo"),             # a bare name, never a path or URL
    ("nightly", "..", "repo"),
    ("nightly", ".", "repo"),
    ("nightly", "", "repo"),                       # one without the other
    ("", "hal-jobs", "name"),
    ("nightly", "x" * 101, "repo"),
])
def test_job_schedule_validation(job, repo, why):
    with pytest.raises(ss.InvalidSchedule):
        ss.validate_job(job, repo)


def test_job_schedule_validation_accepts_and_blank_pair_is_no_job():
    assert ss.validate_job("nightly-sync_2", "Hal.jobs_x-1") == ("nightly-sync_2", "Hal.jobs_x-1")
    assert ss.validate_job("", "") == ("", "")


def test_a_job_schedule_needs_no_prompt_and_is_forced_cloud_only(w):
    made = ss.create_schedule(w["user"], "hal", {
        "name": "sync", "prompt": "", "cron": "0 9 * * *", "job": "nightly-sync",
        "job_repo": "hal-jobs", "routing": "prefer_local", "grace_minutes": 10})
    assert (made.job, made.job_repo, made.prompt) == ("nightly-sync", "hal-jobs", "")
    assert made.routing == Turn.CLOUD_ONLY
    assert made.grace_minutes >= 125
    # A longer grace the person chose is kept.
    kept = ss.create_schedule(w["user"], "hal", {
        "name": "sync2", "cron": "0 9 * * *", "job": "nightly-sync", "job_repo": "hal-jobs",
        "grace_minutes": 300})
    assert kept.grace_minutes == 300
    out = ss.serialize_schedule(made)
    assert out["job"] == "nightly-sync" and out["job_repo"] == "hal-jobs"


def test_an_ordinary_schedule_still_needs_a_prompt(w):
    with pytest.raises(ss.InvalidSchedule):
        ss.create_schedule(w["user"], "hal", {"name": "x", "prompt": "", "cron": "0 9 * * *"})


def test_updating_a_schedule_cannot_leave_half_a_job(w):
    made = ss.create_schedule(w["user"], "hal", {
        "name": "sync", "cron": "0 9 * * *", "job": "nightly-sync", "job_repo": "hal-jobs"})
    with pytest.raises(ss.InvalidSchedule):
        ss.update_schedule(w["user"], "hal", made.pk, {"job_repo": ""})
    # Clearing both turns it back into an ordinary schedule, which then needs a prompt.
    with pytest.raises(ss.InvalidSchedule):
        ss.update_schedule(w["user"], "hal", made.pk, {"job": "", "job_repo": ""})
    back = ss.update_schedule(w["user"], "hal", made.pk,
                              {"job": "", "job_repo": "", "prompt": "/hal:turn", "routing": "any"})
    assert (back.job, back.job_repo, back.routing) == ("", "", "any")


def test_the_database_refuses_half_a_job(w):
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentSchedule.objects.create(agent=w["agent"], name="half", prompt="p", cron="0 9 * * *",
                                     job="nightly-sync")
    with pytest.raises(IntegrityError), transaction.atomic():
        Turn.objects.create(agent=w["agent"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                            idempotency_key="half", idempotency_scope="x", job="nightly-sync")


def test_a_chat_session_turn_cannot_be_a_job(w):
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=w["ws"], created_by=w["user"], agent=w["agent"],
                                     title="t")
    with pytest.raises(IntegrityError), transaction.atomic():
        Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="s",
                            idempotency_scope="x", job="nightly-sync", job_repo="hal-jobs")


# --- the turn --------------------------------------------------------------------------


def test_fire_copies_job_and_forces_cloud_only(w):
    sched = _job_schedule(w, routing=Turn.ANY)       # whatever it said, the turn is cloud-only
    turn, created = schedule_turns.fire_schedule(sched, SLOT)
    assert created
    assert (turn.job, turn.job_repo) == ("nightly-sync", "hal-jobs")
    assert turn.routing == Turn.CLOUD_ONLY
    assert turn.prompt == ""


def test_run_now_copies_job(w):
    turn = schedule_turns.run_schedule_now(_job_schedule(w, routing=Turn.PREFER_LOCAL))
    assert (turn.job, turn.job_repo) == ("nightly-sync", "hal-jobs")
    assert turn.routing == Turn.CLOUD_ONLY and turn.prompt == ""


def test_supersede_and_late_slot_apply(w):
    sched = _job_schedule(w)
    first, _ = schedule_turns.fire_schedule(sched, SLOT)
    second, _ = schedule_turns.fire_schedule(sched, SLOT + dt.timedelta(days=1))
    first.refresh_from_db()
    assert first.status == Turn.MISSED and second.status == Turn.QUEUED
    # A slot nobody could start (no box able) is skipped when late, like any other's.
    assert schedule_turns.skip_late_scheduled_turns(
        now=SLOT + dt.timedelta(days=1) + WINDOW + dt.timedelta(minutes=1)) == 1
    second.refresh_from_db()
    assert second.status == Turn.MISSED


def test_refused_trigger_stops_a_job(w):
    sched = _job_schedule(w)
    WorkspaceMembership.objects.filter(user=w["user"], workspace=w["ws"]).update(
        role=WorkspaceMembership.VIEWER)
    assert triggers.refusal(sched) is not None
    with pytest.raises(triggers.TriggerRefused):
        schedule_turns.fire_schedule(sched, SLOT)
    with pytest.raises(triggers.TriggerRefused):
        schedule_turns.run_schedule_now(sched)
    assert not Turn.objects.exists()


# --- the claim: every path, every kind of box --------------------------------------------


def test_laptop_never_claims_a_job(w):
    laptop = _box(w, "laptop", kind=Runner.EMDASH, caps={"jobs": True, "agents": ["hal"]})
    turn = _job_turn(w)
    assert claim.claim_next_turn(laptop) is None
    # Not even pinned to it: a pin is a placement, never a way past a safety rule.
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=laptop)
    assert claim.claim_next_turn(laptop) is None
    assert Turn.objects.get(pk=turn.pk).status == Turn.QUEUED


@pytest.mark.parametrize("caps", [{}, {"jobs": False}, {"jobs": "yes"}, {"jobs": 1}, {"jobs": None}])
def test_old_cloud_box_never_claims_a_job(w, caps):
    old = _box(w, "old-cloud", caps=caps)
    turn = _job_turn(w)
    assert claim.claim_next_turn(old) is None
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=old)
    assert claim.claim_next_turn(old) is None
    assert Turn.objects.get(pk=turn.pk).status == Turn.QUEUED


def test_directed_old_box_with_a_pin_never_claims_a_job(w):
    old = _box(w, "directed-old", directed=True)
    turn = _job_turn(w)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=old)
    assert claim.claim_next_turn(old) is None


def test_a_cloud_only_turn_without_a_job_also_needs_the_capability(w):
    old = _box(w, "old-cloud")
    new = _box(w, "new-cloud", caps={"jobs": True})
    laptop = _box(w, "laptop", kind=Runner.EMDASH)
    turn = _ordinary_turn(w, routing=Turn.CLOUD_ONLY)
    assert claim.claim_next_turn(old) is None
    assert claim.claim_next_turn(laptop) is None
    assert claim.claim_next_turn(new) == turn


def test_a_sensitive_data_box_never_claims_a_job(w):
    from apps.harness import runner_requirements as rr

    box = _box(w, "sensitive", caps={"jobs": True})
    turn = _job_turn(w)
    # Whatever it reports, a box in force as a sensitive-data runner takes no job.
    import unittest.mock as mock

    with mock.patch.object(rr, "in_force", return_value=frozenset({rr.SENSITIVE_DATA})):
        assert claim.claim_next_turn(box) is None
    assert Turn.objects.get(pk=turn.pk).status == Turn.QUEUED


def test_capable_box_claims_it(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    turn = _job_turn(w)
    got = claim.claim_next_turn(new)
    assert got == turn and got.status == Turn.CLAIMED and got.claimed_by == new


def test_capable_box_claims_a_pinned_job(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    turn = _job_turn(w)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=new)
    assert claim.claim_next_turn(new) == turn


def test_one_job_at_a_time_per_box(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    other = _box(w, "other-cloud", caps={"jobs": True})
    first = _job_turn(w, "a")
    second = _job_turn(w, "b")
    assert claim.claim_next_turn(new) == first
    # The box holding one takes no second job, but another capable box does…
    assert claim.claim_next_turn(new) is None
    assert claim.claim_next_turn(other) == second
    # …and a box that finished its job takes the next.
    harness.finish_turn(Turn.objects.get(pk=first.pk), status=Turn.DONE)
    third = _job_turn(w, "c")
    assert claim.claim_next_turn(new) == third


def test_a_box_running_a_job_still_takes_ordinary_turns(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    job = _job_turn(w)
    assert claim.claim_next_turn(new) == job
    ordinary = _ordinary_turn(w)
    assert claim.claim_next_turn(new) == ordinary


def test_ordinary_turns_unchanged(w):
    old = _box(w, "old-cloud")
    new = _box(w, "new-cloud", caps={"jobs": True})
    laptop = _box(w, "laptop", kind=Runner.EMDASH)
    t1 = _ordinary_turn(w, "o1", routing=Turn.ANY)
    assert claim.claim_next_turn(old) == t1
    t2 = _ordinary_turn(w, "o2", routing=Turn.PREFER_LOCAL)
    assert claim.claim_next_turn(new) == t2
    t3 = _ordinary_turn(w, "o3", routing=Turn.LOCAL_ONLY)
    assert claim.claim_next_turn(old) is None
    assert claim.claim_next_turn(laptop) == t3
    assert (t1.job, t1.job_repo) == ("", "")


def test_the_websocket_channel_asks_the_same_claim(w):
    """The cloud runner's normal path is the socket; it must end in `claim_next_turn`
    (and `claim_payload`) like REST, or the gate would have a side door."""
    import inspect

    from apps.harness import api_runners_protocol
    from apps.realtime import consumers

    assert "claim.claim_next_turn(" in inspect.getsource(consumers)
    assert "claim.claim_next_turn(" in inspect.getsource(api_runners_protocol)


def test_failed_job_not_requeued(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    job = _job_turn(w)
    got = claim.claim_next_turn(new)
    done = harness.finish_turn(got, status=Turn.FAILED, result_note="exit 3")
    assert done.status == Turn.FAILED and done.attempts == 0
    # Control: an ordinary sessionless failure is still retried.
    ordinary = _ordinary_turn(w)
    again = claim.claim_next_turn(new)
    assert again == ordinary
    requeued = harness.finish_turn(again, status=Turn.FAILED, result_note="boom")
    assert requeued.status == Turn.QUEUED and requeued.attempts == 1
    assert job.pk != ordinary.pk


# --- what the box is told ---------------------------------------------------------------


def test_claim_payload_has_name_and_repo_only(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    _job_turn(w)
    payload = claim_payload(claim.claim_next_turn(new))
    assert payload["job"] == "nightly-sync" and payload["job_repo"] == "hal-jobs"
    assert payload["prompt"] == "" and payload["routing"] == Turn.CLOUD_ONLY

    banned = {"run", "command", "cmd", "argv", "args", "script", "exec", "shell", "timeout_minutes"}

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    assert not banned & set(keys(payload))
    # Only the job's name and repo are free text of a job anywhere in it.
    assert "jobs.yaml" not in str(payload)


def test_an_ordinary_claim_payload_carries_blank_job_fields(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    _ordinary_turn(w)
    payload = claim_payload(claim.claim_next_turn(new))
    assert payload["job"] == "" and payload["job_repo"] == ""


# --- waiting is visible -----------------------------------------------------------------


def test_unclaimable_job_listed(w):
    _box(w, "old-cloud")                          # online, but cannot run a job
    _box(w, "laptop", kind=Runner.EMDASH, caps={"agents": ["hal"]})
    turn = _job_turn(w)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - claim.UNCLAIMABLE_GRACE - dt.timedelta(seconds=5))
    rows = claim.unclaimable_queued_turns(w["user"])
    mine = [r for r in rows if r["turn_id"] == str(turn.pk)]
    assert len(mine) == 1
    assert mine[0]["kind"] == "config"
    assert "job" in mine[0]["reason"].lower()


def test_a_capable_box_makes_the_job_not_unclaimable(w):
    _box(w, "new-cloud", caps={"jobs": True})
    turn = _job_turn(w)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - claim.UNCLAIMABLE_GRACE - dt.timedelta(seconds=5))
    assert [r for r in claim.unclaimable_queued_turns(w["user"])
            if r["turn_id"] == str(turn.pk)] == []


def test_a_missed_job_raises_no_rerun_nag(w):
    """The nag's approval re-runs the schedule as an ordinary turn with its prompt;
    for a job that would be an empty prompt, so a job raises none."""
    from apps.agents.models import AgentTask

    sched = _job_schedule(w)
    turn, _ = schedule_turns.fire_schedule(sched, SLOT)
    schedule_turns._raise_schedule_nag(sched, turn)
    assert not AgentTask.objects.filter(origin_ref__schedule_id=sched.pk).exists()


def test_a_job_lost_with_its_box_is_not_run_again(w):
    from apps.harness import fleets

    new = _box(w, "new-cloud", caps={"jobs": True})
    _job_turn(w)
    claimed = claim.claim_next_turn(new)
    assert fleets.requeue_lost(Turn.objects.get(pk=claimed.pk)) is False
    assert Turn.objects.get(pk=claimed.pk).status == Turn.CLAIMED
