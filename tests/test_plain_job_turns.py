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
    Agent.objects.filter(pk=w["agent"].pk).update(owner=None)   # no longer its owner either
    sched = AgentSchedule.objects.get(pk=sched.pk)
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


def _on_machine(runner, machine):
    Runner.objects.filter(pk=runner.pk).update(machine_id=machine)
    runner.refresh_from_db()
    return runner


def test_one_job_at_a_time_per_machine_not_per_runner(w):
    """Runners on one box share `canopy-job`, so the rule is the machine's: the second runner
    must not claim a job only for the broker to fail it with a review."""
    one = _on_machine(_box(w, "box-1", caps={"jobs": True}), "role/i-1")
    two = _on_machine(_box(w, "box-2", caps={"jobs": True}), "role/i-1")
    elsewhere = _on_machine(_box(w, "fleet-member", caps={"jobs": True}), "role/i-2")
    first = _job_turn(w, "a")
    second = _job_turn(w, "b")
    assert claim.claim_next_turn(one) == first
    assert claim.claim_next_turn(two) is None
    # A fleet member is its own machine.
    assert claim.claim_next_turn(elsewhere) == second
    harness.finish_turn(Turn.objects.get(pk=first.pk), status=Turn.DONE)
    third = _job_turn(w, "c")
    assert claim.claim_next_turn(two) == third


def test_a_runner_with_no_machine_id_answers_only_for_itself(w):
    """"" means "not known", not "the same machine": two of them are not one box."""
    one = _box(w, "unknown-1", caps={"jobs": True})
    two = _box(w, "unknown-2", caps={"jobs": True})
    first = _job_turn(w, "a")
    second = _job_turn(w, "b")
    assert (one.machine_id, two.machine_id) == ("", "")
    assert claim.claim_next_turn(one) == first
    assert claim.claim_next_turn(one) is None
    assert claim.claim_next_turn(two) == second


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
    (and `claim_payload`) like REST, or the gate would have a side entrance."""
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


def test_a_job_lost_with_its_box_is_not_run_again(w):
    from apps.harness import fleets

    new = _box(w, "new-cloud", caps={"jobs": True})
    _job_turn(w)
    claimed = claim.claim_next_turn(new)
    assert fleets.requeue_lost(Turn.objects.get(pk=claimed.pk)) is False
    assert Turn.objects.get(pk=claimed.pk).status == Turn.CLAIMED


# --- review fixes ------------------------------------------------------------------------


@pytest.fixture()
def editor(w):
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    wsvc.ensure_member(w["ws"], ed, WorkspaceMembership.EDITOR)
    return ed


JOB = {"name": "sync", "cron": "0 9 * * *", "job": "nightly-sync", "job_repo": "hal-jobs"}


def test_an_editor_cannot_set_up_a_job_but_an_admin_can(w, editor):
    with pytest.raises(ss.ScheduleForbidden):
        ss.create_schedule(editor, "hal", dict(JOB))
    # An editor's ordinary schedule is unchanged.
    ss.create_schedule(editor, "hal", {"name": "plain", "prompt": "/hal:turn", "cron": "0 9 * * *"})
    assert ss.create_schedule(w["user"], "hal", dict(JOB)).job == "nightly-sync"


def test_an_editor_cannot_change_clear_or_edit_a_job_schedule(w, editor):
    made = ss.create_schedule(w["user"], "hal", dict(JOB))
    for fields in ({"job": "other"}, {"job": "", "job_repo": "", "prompt": "/x"},
                   {"name": "renamed"}, {"cron": "0 10 * * *"}):
        with pytest.raises(ss.ScheduleForbidden):
            ss.update_schedule(editor, "hal", made.pk, fields)
    with pytest.raises(ss.ScheduleForbidden):
        ss.delete_schedule(editor, "hal", made.pk)
    # And an editor cannot turn their own plain schedule into a job.
    plain = ss.create_schedule(editor, "hal", {"name": "p", "prompt": "/x", "cron": "0 9 * * *"})
    with pytest.raises(ss.ScheduleForbidden):
        ss.update_schedule(editor, "hal", plain.pk, {"job": "nightly-sync", "job_repo": "hal-jobs"})
    # An admin may do all of it.
    assert ss.update_schedule(w["user"], "hal", made.pk, {"name": "renamed"}).name == "renamed"


def test_a_demoted_admins_job_stops(w, editor):
    sched = _job_schedule(w, created_by=editor)
    # An editor who made it (e.g. before this rule, or demoted since) no longer fires it.
    assert "admin" in (triggers.refusal(sched) or "")
    with pytest.raises(triggers.TriggerRefused):
        schedule_turns.fire_schedule(sched, SLOT)
    boss = _job_schedule(w, "by-admin", created_by=w["user"])
    assert triggers.refusal(boss) is None
    WorkspaceMembership.objects.filter(user=w["user"], workspace=w["ws"]).update(
        role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=w["agent"].pk).update(owner=None)
    assert triggers.refusal(AgentSchedule.objects.get(pk=boss.pk)) is not None


def test_an_agents_own_login_cannot_hold_a_job(w):
    sched = _job_schedule(w, created_by=w["user"])
    from unittest import mock

    with mock.patch.object(Agent, "is_own_login", return_value=True), \
            mock.patch.object(Agent, "is_admin", return_value=False):
        assert triggers.refusal(sched) is not None


def _job_nag(sched):
    from apps.agents.models import AgentTask

    return list(AgentTask.objects.filter(origin_ref__schedule_id=sched.pk, origin_ref__kind="schedule_nag"))


def test_a_failed_job_raises_a_review_with_no_rerun_and_the_last_lines(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w)
    schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    got = claim.claim_next_turn(new)
    harness.finish_turn(got, status=Turn.FAILED, result_note="line one\nboom: no such file")
    nags = _job_nag(sched)
    assert len(nags) == 1
    assert not nags[0].on_approve
    assert "boom: no such file" in nags[0].ask_body and "Run now" in nags[0].ask_body
    # A later success closes it.
    schedule_turns.fire_schedule(sched, SLOT + dt.timedelta(days=1))
    Turn.objects.filter(status=Turn.QUEUED).update(
        created_at=timezone.now() - dt.timedelta(minutes=5))
    again = claim.claim_next_turn(new)
    harness.finish_turn(again, status=Turn.DONE)
    nags[0].refresh_from_db()
    assert nags[0].ask_closed_at is not None


def test_a_lost_job_and_a_grace_released_job_raise_a_review(w):
    from apps.harness import turn_close

    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w)
    schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    got = claim.claim_next_turn(new)
    turn_close.close_turn(got, Turn.LOST, "box gone", from_states=Turn.NON_TERMINAL)
    assert len(_job_nag(sched)) == 1
    other = _job_schedule(w, "second")
    schedule_turns.fire_schedule(other, SLOT)
    Turn.objects.filter(status=Turn.QUEUED).update(
        created_at=timezone.now() - dt.timedelta(minutes=5))
    got2 = claim.claim_next_turn(new)
    schedule_turns.release_stale_occurrence_turns(
        other, now=got2.claimed_at + dt.timedelta(minutes=other.grace_minutes + 1))
    nags = _job_nag(other)
    assert len(nags) == 1 and not nags[0].on_approve


NO_MACHINE = "No cloud machine that can run jobs took it in time."


def _past_the_window():
    return SLOT + WINDOW + dt.timedelta(minutes=1)


def test_a_late_skipped_job_raises_a_review_with_the_reason_and_the_row_says_it(w):
    from apps.harness.schedule_services import serialize_trigger

    sched = _job_schedule(w)
    turn, _ = schedule_turns.fire_schedule(sched, SLOT)
    assert serialize_trigger(sched)["missed_reason"] == ""
    assert schedule_turns.skip_late_scheduled_turns(now=_past_the_window()) == 1
    turn.refresh_from_db()
    assert turn.status == Turn.MISSED and turn.result_note == NO_MACHINE
    nags = _job_nag(sched)
    assert len(nags) == 1 and not nags[0].on_approve
    assert NO_MACHINE in nags[0].ask_body and "Run now" in nags[0].ask_body
    # Canopy's own sentence is not something the job printed.
    assert "last lines it printed" not in nags[0].ask_body
    row = serialize_trigger(AgentSchedule.objects.get(pk=sched.pk))
    assert row["last_status"] == Turn.MISSED and row["missed_reason"] == NO_MACHINE
    # The next occurrence replaces the reason.
    schedule_turns.fire_schedule(sched, SLOT + dt.timedelta(days=1))
    assert serialize_trigger(AgentSchedule.objects.get(pk=sched.pk))["missed_reason"] == ""


def test_an_outage_keeps_one_open_review_per_schedule(w):
    from apps.agents import services as agent_services

    sched = _job_schedule(w)
    other = _job_schedule(w, "other")
    for day in range(3):
        at = SLOT + dt.timedelta(days=day)
        schedule_turns.fire_schedule(sched, at)
        schedule_turns.fire_schedule(other, at)
        schedule_turns.skip_late_scheduled_turns(now=at + WINDOW + dt.timedelta(minutes=1))
    nags = _job_nag(sched)
    assert len(nags) == 1                  # however many slots went by
    assert len(_job_nag(other)) == 1       # and one schedule's review doesn't hide another's
    # Closed, the next missed slot may ask again.
    agent_services.act(nags[0], action="decline", by="system:test", actor_workspace_ids=set())
    at = SLOT + dt.timedelta(days=3)
    schedule_turns.fire_schedule(sched, at)
    schedule_turns.skip_late_scheduled_turns(now=at + WINDOW + dt.timedelta(minutes=1))
    assert len(_job_nag(sched)) == 2


def test_a_failure_during_an_open_review_opens_no_second_one(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w)
    schedule_turns.fire_schedule(sched, SLOT)
    schedule_turns.skip_late_scheduled_turns(now=_past_the_window())
    schedule_turns.fire_schedule(sched, SLOT + dt.timedelta(days=1))
    Turn.objects.filter(status=Turn.QUEUED).update(created_at=timezone.now() - dt.timedelta(minutes=5))
    harness.finish_turn(claim.claim_next_turn(new), status=Turn.FAILED, result_note="boom")
    assert len(_job_nag(sched)) == 1


def test_a_late_skipped_job_opted_out_of_the_inbox_raises_nothing(w):
    sched = _job_schedule(w, notify=[])
    schedule_turns.fire_schedule(sched, SLOT)
    assert schedule_turns.skip_late_scheduled_turns(now=_past_the_window()) == 1
    assert _job_nag(sched) == []


def test_an_always_run_job_keeps_waiting_past_the_window(w):
    sched = _job_schedule(w, always_run=True)
    turn, _ = schedule_turns.fire_schedule(sched, SLOT)
    assert schedule_turns.skip_late_scheduled_turns(now=_past_the_window()) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED and _job_nag(sched) == []


def test_a_late_skipped_ordinary_slot_still_raises_nothing(w):
    """Skipping stays the designed outcome for a turn; only a job's owner is told."""
    sched = AgentSchedule.objects.create(agent=w["agent"], name="brief", prompt="/hal:turn",
                                         cron="0 9 * * *", created_by=w["user"])
    schedule_turns.fire_schedule(sched, SLOT)
    assert schedule_turns.skip_late_scheduled_turns(now=_past_the_window()) == 1
    assert _job_nag(sched) == []


def test_a_grace_released_jobs_note_is_not_called_its_output(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w)
    schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    got = claim.claim_next_turn(new)
    schedule_turns.release_stale_occurrence_turns(
        sched, now=got.claimed_at + dt.timedelta(minutes=sched.grace_minutes + 1))
    body = _job_nag(sched)[0].ask_body
    assert "released after 125m unattended" in body and "last lines it printed" not in body


def test_a_job_that_opts_out_of_the_inbox_raises_no_review(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w, notify=[])
    schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    harness.finish_turn(claim.claim_next_turn(new), status=Turn.FAILED, result_note="x")
    assert _job_nag(sched) == []


def test_a_failing_job_starts_no_debugger_turn(w, settings):
    from unittest import mock

    from apps.harness import auto_debug

    settings.CANOPY_AUTO_DEBUG = True
    new = _box(w, "new-cloud", caps={"jobs": True})
    _job_turn(w)
    got = claim.claim_next_turn(new)
    with mock.patch.object(auto_debug, "_note_for", return_value="exit 3") as looked:
        harness.finish_turn(got, status=Turn.FAILED, result_note="exit 3: tests failed")
    # The debugger gets no further than the exemption: it never even reads the note.
    looked.assert_not_called()
    # Control: the same failure of an ordinary turn does reach it.
    ordinary = _ordinary_turn(w)
    again = claim.claim_next_turn(new)
    Turn.objects.filter(pk=again.pk).update(attempts=3)        # past the sessionless retries
    again.refresh_from_db()
    with mock.patch.object(auto_debug, "_note_for", return_value="boom") as looked:
        harness.finish_turn(again, status=Turn.FAILED, result_note="boom")
    assert again.pk == ordinary.pk
    assert looked.called


@pytest.mark.parametrize("ending", [Turn.LOST, Turn.MISSED])
def test_a_lost_or_missed_job_starts_no_debugger_turn_with_lost_on(w, settings, ending):
    """A job's ending is told by its review; `CANOPY_AUTO_DEBUG_LOST` is for turns, which a
    debugger can read. (A job has no transcript and runs no model.)"""
    from unittest import mock

    from apps.harness import auto_debug, turn_close

    settings.CANOPY_AUTO_DEBUG = True
    settings.CANOPY_AUTO_DEBUG_LOST = True
    new = _box(w, "new-cloud", caps={"jobs": True})
    _job_turn(w)
    got = claim.claim_next_turn(new)
    with mock.patch.object(auto_debug, "_note_for", return_value="box gone") as looked:
        turn_close.close_turn(got, ending, "box gone", from_states=Turn.NON_TERMINAL)
        auto_debug._on_turn_failed(Turn.objects.get(pk=got.pk))
    looked.assert_not_called()
    # Control: the same ending of an ordinary turn does reach it, when it is LOST.
    if ending == Turn.LOST:
        ordinary = _ordinary_turn(w)
        again = claim.claim_next_turn(new)
        assert again.pk == ordinary.pk
        with mock.patch.object(auto_debug, "_note_for", return_value="box gone") as looked:
            turn_close.close_turn(again, Turn.LOST, "box gone", from_states=Turn.NON_TERMINAL)
        assert looked.called


def test_a_new_slot_never_supersedes_a_running_job(w):
    new = _box(w, "new-cloud", caps={"jobs": True})
    sched = _job_schedule(w)
    first, _ = schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    running = claim.claim_next_turn(new)
    second, _ = schedule_turns.fire_schedule(sched, SLOT + dt.timedelta(days=1))
    running.refresh_from_db()
    assert running.status == Turn.CLAIMED                    # the job carries on
    assert second.status == Turn.MISSED and "still going" in second.result_note
    # Run now is refused outright: pressing it must not leave a MISSED turn behind.
    before = Turn.objects.count()
    with pytest.raises(schedule_turns.JobStillRunning, match="This job is still running."):
        schedule_turns.run_schedule_now(sched)
    assert Turn.objects.count() == before
    running.refresh_from_db()
    assert running.status == Turn.CLAIMED
    # The box still counts as busy, so it takes no other job.
    _job_turn(w, "other")
    assert claim.claim_next_turn(new) is None
    # A queued occurrence is still superseded: nothing runs, so a newer slot replaces it.
    other_sched = _job_schedule(w, "q")
    queued, _ = schedule_turns.fire_schedule(other_sched, SLOT)
    newer, _ = schedule_turns.fire_schedule(other_sched, SLOT + dt.timedelta(days=1))
    queued.refresh_from_db()
    assert queued.status == Turn.MISSED and newer.status == Turn.QUEUED


def test_a_signed_out_box_still_takes_a_job_but_no_prompt_turn(w):
    box = _box(w, "no-login", caps={"jobs": True, "clis": {
        "claude-code": {"signed_in": False, "profiles": 0, "envelope": 0}}})
    ordinary = _ordinary_turn(w)
    assert claim.claim_next_turn(box) is None
    assert Turn.objects.get(pk=ordinary.pk).status == Turn.QUEUED
    job = _job_turn(w)
    got = claim.claim_next_turn(box)
    assert got == job and got.cli == ""
    # An old box with no login takes neither.
    old = _box(w, "old-no-login", caps={"clis": {
        "claude-code": {"signed_in": False, "profiles": 0, "envelope": 0}}})
    _job_turn(w, "again")
    assert claim.claim_next_turn(old) is None


def test_the_unclaimable_reason_does_not_blame_pairing(w):
    capable = _box(w, "elsewhere", caps={"jobs": True})
    RunnerAssignment.objects.filter(runner=capable).delete()      # off the agent's list
    turn = _job_turn(w)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - claim.UNCLAIMABLE_GRACE - dt.timedelta(seconds=5))
    rows = [r for r in claim.unclaimable_queued_turns(w["user"]) if r["turn_id"] == str(turn.pk)]
    assert rows and "No cloud machine that can run jobs serves this agent" in rows[0]["reason"]
    assert "paired" not in rows[0]["reason"]


def test_cloud_only_routing_needs_a_job(w):
    with pytest.raises(ss.InvalidSchedule):
        ss.create_schedule(w["user"], "hal", {"name": "x", "prompt": "/x", "cron": "0 9 * * *",
                                              "routing": "cloud_only"})
    plain = ss.create_schedule(w["user"], "hal", {"name": "p", "prompt": "/x", "cron": "0 9 * * *"})
    with pytest.raises(ss.InvalidSchedule):
        ss.update_schedule(w["user"], "hal", plain.pk, {"routing": "cloud_only"})


def _patch(client, runner, caps):
    return client.patch(f"/api/harness/runners/{runner.id}", data={"capabilities": caps},
                        content_type="application/json")


def test_a_person_cannot_patch_jobs_onto_a_box(w, client):
    old = _box(w, "old-cloud")
    client.force_login(w["user"])
    res = _patch(client, old, {"sessions": True, "jobs": True})
    assert res.status_code == 200, res.content
    old.refresh_from_db()
    assert "jobs" not in old.capabilities
    # Nor can a person take it away from a box that said it.
    capable = _box(w, "new-cloud", caps={"jobs": True})
    assert _patch(client, capable, {"sessions": True}).status_code == 200
    capable.refresh_from_db()
    assert capable.capabilities["jobs"] is True
    assert _patch(client, capable, {"sessions": True, "jobs": False}).status_code == 200
    capable.refresh_from_db()
    assert capable.capabilities["jobs"] is True


def test_the_box_itself_may_say_it_runs_jobs_and_pairing_cannot(w):
    from django.test import Client

    from apps.harness.api import declared_capabilities
    from apps.harness.models import RunnerAccessToken, RunnerDeviceKey

    assert "jobs" not in declared_capabilities({"jobs": True, "sessions": True})
    box = _box(w, "own")
    key = RunnerDeviceKey.objects.create(runner=box, kind=RunnerDeviceKey.SOFTWARE,
                                         status=RunnerDeviceKey.ACTIVE, public_jwk={})
    raw, _ = RunnerAccessToken.mint(key)
    res = _patch(Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), box, {"sessions": True, "jobs": True})
    assert res.status_code == 200, res.content
    box.refresh_from_db()
    assert box.capabilities["jobs"] is True
