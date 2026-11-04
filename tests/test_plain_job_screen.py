"""What the schedule screen needs from the server for a plain job (Part 3, Task 4).

* a non-admin who tries to set or change a job is answered in a sentence, from here;
* a turn's listing says which job it was, so its page shows a job and not an empty prompt;
* the review task a failed job raises has NO approve action: approving a task with no
  `on_approve` starts a model turn on the agent, which for a job is the empty-prompt
  mistake the claim gate exists to prevent. The task says which schedule it belongs to so
  its screen can offer "Run now" instead.
"""
from __future__ import annotations

import datetime as dt

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents import services as agent_services
from apps.agents.models import Agent, AgentTask
from apps.agents.schemas.tasks import AgentTaskOut
from apps.harness import claim, schedule_turns
from apps.harness import services as harness
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)
M = WorkspaceMembership
SLOT = dt.datetime(2026, 11, 4, 9, tzinfo=dt.UTC)


@pytest.fixture()
def w():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    for u, role in ((boss, M.OWNER), (ed, M.EDITOR)):
        M.objects.create(user=u, workspace=ws, role=role)
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=boss)
    return {"ws": ws, "agent": agent, "boss": boss, "ed": ed}


def _c(user):
    c = Client()
    c.force_login(user)
    return c


JOB = {"name": "Nightly", "job": "nightly-sync", "job_repo": "hal-jobs", "cron": "0 9 * * *"}


def test_a_non_admin_setting_a_job_is_told_so_in_a_sentence(w):
    r = _c(w["ed"]).post("/api/agents/hal/triggers/", JOB, content_type="application/json")
    assert r.status_code == 403
    assert r.json()["detail"] == "Only an admin of hal can change a job."


def test_a_non_admin_changing_a_job_schedule_is_told_so_in_a_sentence(w):
    sched = AgentSchedule.objects.create(agent=w["agent"], name="n", prompt="", cron="0 9 * * *",
                                         job="nightly-sync", job_repo="hal-jobs",
                                         routing=Turn.CLOUD_ONLY, grace_minutes=125,
                                         created_by=w["boss"])
    for verb, url, body in (("patch", f"/api/agents/hal/triggers/{sched.pk}", {"enabled": False}),
                            ("delete", f"/api/agents/hal/triggers/{sched.pk}", None)):
        c = _c(w["ed"])
        r = getattr(c, verb)(url, body, content_type="application/json") if body else getattr(c, verb)(url)
        assert r.status_code == 403, verb
        assert r.json()["detail"] == "Only an admin of hal can change a job."


def test_an_admin_sets_a_job_and_the_listing_carries_it(w):
    r = _c(w["boss"]).post("/api/agents/hal/triggers/", JOB, content_type="application/json")
    assert r.status_code == 201, r.content
    row = _c(w["boss"]).get("/api/agents/hal/triggers/").json()["items"][0]
    assert (row["job"], row["job_repo"]) == ("nightly-sync", "hal-jobs")


def test_an_ordinary_schedule_forbidden_sentence_is_unchanged(w):
    viewer = User.objects.create_user("vw", "vw@dimagi.com", "pw")
    M.objects.create(user=viewer, workspace=w["ws"], role=M.VIEWER)
    EmailAddress.objects.create(user=viewer, email=viewer.email, verified=True, primary=True)
    r = _c(viewer).post("/api/agents/hal/triggers/",
                        {"name": "x", "prompt": "/hal:turn", "cron": "0 9 * * 5"},
                        content_type="application/json")
    assert r.status_code == 403 and "job" not in r.json()["detail"]


def _failed_job_nag(w):
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, host="box", owner=w["boss"], workspace_id=w["ws"].pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "jobs": True})
    RunnerAssignment.objects.create(agent=w["agent"], runner=runner, rank=0)
    sched = AgentSchedule.objects.create(agent=w["agent"], name="nightly", prompt="", cron="0 9 * * *",
                                         job="nightly-sync", job_repo="hal-jobs",
                                         routing=Turn.CLOUD_ONLY, grace_minutes=125,
                                         created_by=w["boss"])
    schedule_turns.fire_schedule(sched, SLOT)
    Turn.objects.update(created_at=timezone.now() - dt.timedelta(minutes=5))
    got = claim.claim_next_turn(runner)
    harness.finish_turn(got, status=Turn.FAILED, result_note="boom")
    return sched, AgentTask.objects.get(origin_ref__schedule_id=sched.pk, origin_ref__kind="schedule_nag")


def test_the_turn_listing_names_the_job(w):
    sched, _ = _failed_job_nag(w)
    rows = _c(w["boss"]).get("/api/harness/turns/?agent=hal").json()
    assert (rows[0]["job"], rows[0]["job_repo"]) == ("nightly-sync", "hal-jobs")
    mine = _c(w["boss"]).get("/api/agents/hal/turns/").json()
    items = mine["items"] if isinstance(mine, dict) else mine
    assert (items[0]["job"], items[0]["job_repo"]) == ("nightly-sync", "hal-jobs")


def test_the_failed_jobs_review_says_which_schedule_runs_it_again(w):
    sched, nag = _failed_job_nag(w)
    assert AgentTaskOut.model_validate(nag).job_schedule_id == sched.pk


def test_an_ordinary_schedule_nag_has_no_job_schedule(w):
    task = AgentTask.objects.create(
        workspace=w["ws"], agent=w["agent"], title="Scheduled turn unattended: x", ask_kind="review",
        origin_ref={"schedule_id": 1, "kind": "schedule_nag"})
    assert AgentTaskOut.model_validate(task).job_schedule_id is None


def test_approving_a_failed_jobs_review_starts_no_turn_and_says_what_to_do(w):
    _, nag = _failed_job_nag(w)
    before = Turn.objects.count()
    with pytest.raises(ValueError, match="Run now"):
        agent_services.act(nag, action="approve", by="boss@dimagi.com", by_user=w["boss"],
                           actor_workspace_ids={"connect"}, may_start_turns=True)
    assert Turn.objects.count() == before
    nag.refresh_from_db()
    assert nag.ask_closed_at is None          # still open, still decidable another way


def test_the_other_decisions_on_a_failed_jobs_review_still_work(w):
    _, nag = _failed_job_nag(w)
    agent_services.act(nag, action="decline", by="boss@dimagi.com", by_user=w["boss"],
                       actor_workspace_ids={"connect"}, may_start_turns=True)
    nag.refresh_from_db()
    assert nag.ask_closed_at is not None
