"""Agent tasks over the REST routes: create, read, submit, cancel, the long-poll,
the "responded too soon" backstop, the deadline, and the caller envelope."""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.agent_tasks import services
from apps.agent_tasks.models import AgentTask
from apps.agents.models import AgentSkill
from apps.harness import caller_context
from apps.harness import services as harness
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken

from tests.access_helpers import editors_run

from .helpers import GOOD, INPUT, SCHEMA, _create, _pat, _post

pytestmark = pytest.mark.django_db

@pytest.fixture()
def task(world):
    r = _create(_pat(world["owner"]))
    assert r.status_code == 201, r.content
    return AgentTask.objects.get(pk=r.json()["id"])


def _end_turn(turn, django_capture_on_commit_callbacks, status=Turn.DONE):
    with django_capture_on_commit_callbacks(execute=True):
        if status == Turn.CANCELLED:
            harness.cancel_queued_turn(turn)
        else:
            harness.finish_turn(turn, status=status, allow_queued=True)


# ---- create --------------------------------------------------------------------

def test_create_queues_one_turn_that_names_the_task(world):
    r = _create(_pat(world["owner"]))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "queued" and body["input"] is None and body["output_schema"] == SCHEMA
    turn = Turn.objects.get(pk=body["turn_id"])
    assert turn.agent == world["agent"] and turn.origin == Turn.ORIGIN_API
    assert turn.origin_ref == {"agent_task": body["id"]}
    assert turn.idempotency_key == f"agent-task:{body['id']}"
    assert "get_agent_task" in turn.prompt and "submit_agent_task_result" in turn.prompt
    assert "no outbound action" in turn.prompt
    assert turn.initiator_user == world["owner"]


def test_a_task_named_after_a_skill_tells_the_turn_to_use_it(world):
    AgentSkill.objects.create(agent=world["agent"], name="gmail-triage")
    body = _create(_pat(world["owner"])).json()
    assert "using your `gmail-triage` skill" in Turn.objects.get(pk=body["turn_id"]).prompt


def test_create_has_the_enqueue_turn_gate(world):
    editors_run(world["agent"])
    assert _create(_pat(world["editor"])).status_code == 201
    assert _create(_pat(world["viewer"])).status_code == 403
    assert _create(_pat(world["outsider"])).status_code == 404
    assert not AgentTask.objects.filter(created_by__in=[world["viewer"], world["outsider"]]).exists()


def test_an_invalid_output_schema_is_refused(world):
    r = _create(_pat(world["owner"]), output_schema={"type": "not-a-type"})
    assert r.status_code == 422 and "not a valid JSON Schema" in r.json()["detail"]
    assert not AgentTask.objects.exists() and not Turn.objects.exists()


def test_an_input_over_the_cap_is_413(world):
    big = {"blob": "x" * (services.MAX_INPUT_BYTES + 1)}
    r = _create(_pat(world["owner"]), input=big)
    assert r.status_code == 413 and "input" in r.json()["detail"]
    assert not Turn.objects.exists()


def test_timeout_is_capped(world):
    assert _create(_pat(world["owner"]), timeout_s=7200).status_code == 422


def test_a_client_cannot_stamp_agent_task_on_an_ordinary_turn(world, task):
    r = _post(_pat(world["owner"]), "/api/harness/turns/", {
        "agent_slug": "bailey", "origin": "api", "idempotency_key": "plain-1",
        "origin_ref": {"agent_task": str(task.pk), "note": "kept"}})
    assert r.status_code == 201, r.content
    assert Turn.objects.get(idempotency_key="plain-1").origin_ref == {"note": "kept"}


# ---- read ----------------------------------------------------------------------

def test_the_agent_reads_its_task_and_it_becomes_running(world, task):
    r = _pat(world["bot"]).get(f"/api/agent-tasks/{task.pk}")
    assert r.status_code == 200
    body = r.json()
    assert body["input"] == INPUT and body["output_schema"] == SCHEMA and body["status"] == "running"


def test_who_may_read(world, task):
    assert _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}").json()["input"] is None
    assert _pat(world["editor"]).get(f"/api/agent-tasks/{task.pk}").status_code == 404
    assert _pat(world["outsider"]).get(f"/api/agent-tasks/{task.pk}").status_code == 404


def test_the_creator_reads_their_own_task(world):
    editors_run(world["agent"])
    body = _create(_pat(world["editor"])).json()
    assert _pat(world["editor"]).get(f"/api/agent-tasks/{body['id']}").json()["status"] == "queued"


# ---- submit --------------------------------------------------------------------

def test_only_the_agent_submits(world, task):
    r = _post(_pat(world["owner"]), f"/api/agent-tasks/{task.pk}/result", {"result": GOOD})
    assert r.status_code == 403
    assert _post(_pat(world["outsider"]), f"/api/agent-tasks/{task.pk}/result", {"result": GOOD}).status_code == 404
    task.refresh_from_db()
    assert task.status == AgentTask.QUEUED


def test_an_invalid_result_is_422_with_paths_and_the_task_stays_open(world, task):
    bad = {"decisions": [{"id": "m1", "action": "burn"}]}
    r = _post(_pat(world["bot"]), f"/api/agent-tasks/{task.pk}/result", {"result": bad})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert {"path": "/decisions/0", "message": "'reason' is a required property"} in errors
    assert any(e["path"] == "/decisions/0/action" for e in errors)
    task.refresh_from_db()
    assert task.status in AgentTask.OPEN and task.result is None


def test_a_valid_result_completes_the_task_once(world, task):
    bot = _pat(world["bot"])
    r = _post(bot, f"/api/agent-tasks/{task.pk}/result", {"result": GOOD})
    assert r.status_code == 200 and r.json()["status"] == "done"
    got = _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}").json()
    assert got["status"] == "done" and got["result"] == GOOD
    assert _post(bot, f"/api/agent-tasks/{task.pk}/result", {"result": GOOD}).status_code == 409


def test_a_turn_scoped_agent_login_may_submit(world, task):
    """The login canopy issues a runner for ONE turn is a PersonalToken of the agent."""
    raw, _ = PersonalToken.create_for_user(user=world["bot"], ttl=dt.timedelta(hours=1), label="turn-scoped")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert _post(c, f"/api/agent-tasks/{task.pk}/result", {"result": GOOD}).status_code == 200


# ---- long-poll -----------------------------------------------------------------

def test_the_long_poll_returns_when_the_status_changes(world, task, monkeypatch):
    def finish_meanwhile(_s):
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.DONE, result=GOOD)

    monkeypatch.setattr(services, "_sleep", finish_meanwhile)
    body = _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}?wait_s=20").json()
    assert body["status"] == "done" and body["result"] == GOOD


def test_the_long_poll_gives_up_after_wait_s(world, task, monkeypatch):
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(services, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(services, "_sleep", lambda _s: None)
    assert _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}?wait_s=10").json()["status"] == "queued"
    assert _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}?wait_s=21").status_code == 422


# ---- the backstop: a turn ended without a result ---------------------------------

def test_a_turn_ending_without_a_result_is_nudged_then_the_task_fails(world, task,
                                                                      django_capture_on_commit_callbacks):
    first = task.turn
    _end_turn(first, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    assert task.status in AgentTask.OPEN and task.nudges == 1 and task.turn_id != first.pk
    nudge = task.turn
    assert nudge.origin_ref["agent_task"] == str(task.pk) and nudge.parent_turn_id == first.pk
    assert nudge.prompt.startswith(f"You ended without submitting the result for agent task {task.pk}.")
    assert "Call submit_agent_task_result now." in nudge.prompt

    _end_turn(nudge, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    assert task.nudges == 2 and task.status in AgentTask.OPEN

    _end_turn(task.turn, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    assert task.status == AgentTask.FAILED and task.error == services.ENDED_WITHOUT_RESULT
    assert Turn.objects.filter(origin_ref__agent_task=str(task.pk)).count() == 3


def test_a_done_task_is_not_nudged(world, task, django_capture_on_commit_callbacks):
    assert _post(_pat(world["bot"]), f"/api/agent-tasks/{task.pk}/result", {"result": GOOD}).status_code == 200
    _end_turn(task.turn, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    assert task.status == AgentTask.DONE and task.nudges == 0
    assert Turn.objects.filter(origin_ref__agent_task=str(task.pk)).count() == 1


def test_only_the_tasks_latest_turn_moves_it(world, task, django_capture_on_commit_callbacks):
    stale = task.turn
    _end_turn(stale, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    before = task.nudges
    services.on_turn_ended(stale)  # the old turn's end again
    task.refresh_from_db()
    assert task.nudges == before


def test_a_cancelled_turn_fails_the_task(world, task, django_capture_on_commit_callbacks):
    _end_turn(task.turn, django_capture_on_commit_callbacks, status=Turn.CANCELLED)
    task.refresh_from_db()
    assert task.status == AgentTask.FAILED and task.error.startswith("its turn was cancelled")


# ---- the deadline ----------------------------------------------------------------

def test_a_task_past_its_deadline_fails_on_read(world, task):
    AgentTask.objects.filter(pk=task.pk).update(deadline_at=timezone.now() - dt.timedelta(seconds=1))
    body = _pat(world["owner"]).get(f"/api/agent-tasks/{task.pk}").json()
    assert body["status"] == "failed" and body["error"].startswith("timed out after 900s")
    assert Turn.objects.get(pk=task.turn_id).status == Turn.CANCELLED
    r = _post(_pat(world["bot"]), f"/api/agent-tasks/{task.pk}/result", {"result": GOOD})
    assert r.status_code == 409


def test_the_tick_fails_overdue_tasks(world, task):
    AgentTask.objects.filter(pk=task.pk).update(deadline_at=timezone.now() - dt.timedelta(seconds=1))
    assert services.expire_overdue() == 1
    task.refresh_from_db()
    assert task.status == AgentTask.FAILED


# ---- cancel ----------------------------------------------------------------------

def test_the_creator_cancels_and_the_queued_turn_goes_with_it(world, task):
    r = _post(_pat(world["owner"]), f"/api/agent-tasks/{task.pk}/cancel", {})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert Turn.objects.get(pk=task.turn_id).status == Turn.CANCELLED
    task.refresh_from_db()
    assert task.nudges == 0  # the cancelled turn's end nudged nothing


def test_the_agent_and_strangers_do_not_cancel(world, task):
    assert _post(_pat(world["bot"]), f"/api/agent-tasks/{task.pk}/cancel", {}).status_code == 403
    assert _post(_pat(world["editor"]), f"/api/agent-tasks/{task.pk}/cancel", {}).status_code == 404


# ---- the caller envelope ---------------------------------------------------------

def test_the_envelope_names_the_task_the_turn_holds(world, task):
    env = caller_context.build(task.turn)
    assert env["agent_task"] == {"id": str(task.pk), "name": "gmail-triage.review", "status": "queued"}


def test_an_ordinary_turn_holds_no_task(world):
    turn, _ = harness.enqueue_turn(agent=world["agent"], origin=Turn.ORIGIN_API, idempotency_key="k")
    assert caller_context.build(turn)["agent_task"] is None


def test_a_laptop_session_still_working_is_not_nudged(world, task, monkeypatch,
                                                      django_capture_on_commit_callbacks):
    """A laptop turn finishes as soon as its session exists; the agent works on in it."""
    monkeypatch.setattr(services, "_session_lives_on", lambda turn: True)
    _end_turn(task.turn, django_capture_on_commit_callbacks)
    task.refresh_from_db()
    assert task.status in AgentTask.OPEN and task.nudges == 0
