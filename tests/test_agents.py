"""Tests for the agent workspace services (idempotency, catalog replace)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from apps.agent_runs.models import AgentRun
from apps.agents import services
from apps.agents.models import Agent, AgentSkill, AgentSync, AgentTask, AgentWorkProduct
from apps.harness.models import Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    # `workspace` is a required keyword: Agent.workspace is NOT NULL
    # (agents/0013), so the tenant is chosen before the row is written rather
    # than patched on afterwards by the view.
    return services.upsert_agent(
        SimpleNamespace(slug=slug, name="Echo", description="", persona="", email="echo@x.com", avatar_url=""),
        workspace=a_workspace(),
    )


def test_upsert_agent_is_idempotent_by_slug():
    a1 = _agent()
    a2 = services.upsert_agent(
        SimpleNamespace(slug="echo", name="Echo v2", description="d", persona="p", email="", avatar_url=""),
        workspace=a_workspace(),
    )
    assert a1.pk == a2.pk
    assert Agent.objects.count() == 1
    assert Agent.objects.get(slug="echo").name == "Echo v2"


def test_sync_is_idempotent_per_period_and_source():
    agent = _agent()
    start = dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 17, tzinfo=dt.timezone.utc)
    payload = SimpleNamespace(
        period_start=start, period_end=end, title="Sync 1", summary="s",
        doc_url="https://docs.google.com/document/d/abc/edit",
        self_grades={"work": "C+", "skills": "B-"}, source="manager-sync",
    )
    services.upsert_sync(agent, payload)
    payload.title = "Sync 1 (revised)"
    services.upsert_sync(agent, payload)  # same window+source → replaces
    assert AgentSync.objects.filter(agent=agent).count() == 1
    assert AgentSync.objects.get(agent=agent).title == "Sync 1 (revised)"
    assert AgentSync.objects.get(agent=agent).self_grades["work"] == "C+"


def test_delete_sync_removes_only_the_targeted_row():
    agent = _agent()
    other = _agent("eva")
    payload = SimpleNamespace(
        period_start=dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 6, 17, tzinfo=dt.timezone.utc),
        title="Sync 1", summary="s", doc_url="https://docs.google.com/document/d/abc/edit",
        self_grades={}, source="manager-sync",
    )
    keep = services.upsert_sync(agent, payload)
    payload.period_start = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    payload.period_end = dt.datetime(2026, 7, 8, tzinfo=dt.timezone.utc)
    payload.title = "Wrong period"
    doomed = services.upsert_sync(agent, payload)
    theirs = services.upsert_sync(other, payload)

    assert services.delete_sync(agent, doomed.pk) is True
    assert set(AgentSync.objects.filter(agent=agent).values_list("pk", flat=True)) == {keep.pk}
    # gone → False (so the API can 404 rather than silently succeed)
    assert services.delete_sync(agent, doomed.pk) is False
    # another agent's sync is not reachable through this agent
    assert services.delete_sync(agent, theirs.pk) is False
    assert AgentSync.objects.filter(pk=theirs.pk).exists()


def _turn(**kw):
    base = dict(cli_session_id="sess-1", title="Turn 1", summary="did stuff",
                task_ext_ids=["t1"], work_product_urls=[], session_slug="", share_token="",
                started_at=None, ended_at=None, source="turn", emdash_task_id="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_turn_is_idempotent_per_cli_session_id():
    agent = _agent()
    services.upsert_turn(agent, _turn(title="Turn 1", task_ext_ids=["t1"]))
    # re-package the same session (e.g. after the transcript uploads) → updates in place
    services.upsert_turn(agent, _turn(title="Turn 1 (transcript added)",
                                      task_ext_ids=["t1", "t2"],
                                      session_slug="abc123", share_token="tok999"))
    assert Turn.objects.filter(agent=agent).count() == 1
    turn = Turn.objects.get(agent=agent)
    assert turn.report_title == "Turn 1 (transcript added)"
    assert turn.task_ext_ids == ["t1", "t2"]
    assert turn.share_token == "tok999"
    # a different session is a separate turn
    services.upsert_turn(agent, _turn(cli_session_id="sess-2", title="Turn 2"))
    assert Turn.objects.filter(agent=agent).count() == 2


def test_turn_transcript_is_optional():
    agent = _agent()
    turn = services.upsert_turn(agent, _turn(session_slug="", share_token=""))
    assert turn.session_slug == "" and turn.share_token == ""
    assert turn.task_ext_ids == ["t1"]  # the unit of work is still packaged


def test_agent_detail_counts_turns():
    agent = _agent()
    assert services.agent_detail(agent)["turn_count"] == 0
    assert services.agent_detail(agent)["latest_turn_at"] is None
    services.upsert_turn(agent, _turn(cli_session_id="s1"))
    services.upsert_turn(agent, _turn(cli_session_id="s2"))
    detail = services.agent_detail(agent)
    assert detail["turn_count"] == 2
    assert detail["latest_turn_at"] is not None


def _dispatch(agent, *, key, task="", started_at=None, status=Turn.DONE):
    """A turn as the HARNESS creates it — dispatched, no close-out report."""
    return Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, status=status, idempotency_key=key,
        emdash_task_id=task, started_at=started_at,
    )


def test_dispatched_turns_count_even_when_the_agent_never_closed_out():
    """The regression this merge exists for.

    ada rendered `turn_count 0, latest_turn_at null` on the fleet page while
    /api/harness/turns/?agent=ada returned 29 records: the detail endpoint read the
    close-out table, and ada — like any agent whose sessions die before packaging —
    had never written a row to it. Dispatch is what "did this agent run" means.
    """
    agent = _agent()
    ran_at = dt.datetime(2026, 8, 10, 18, 5, tzinfo=dt.UTC)
    _dispatch(agent, key="k1", started_at=ran_at)
    detail = services.agent_detail(agent)
    assert detail["turn_count"] == 1
    assert detail["latest_turn_at"] == ran_at


def test_latest_turn_at_ignores_queued_turns_that_never_started():
    """A queued turn has started_at=NULL. Postgres sorts DESC NULLS FIRST, so the
    naive ordering would let one queued turn mask every real run."""
    agent = _agent()
    ran_at = dt.datetime(2026, 8, 10, 18, 5, tzinfo=dt.UTC)
    _dispatch(agent, key="ran", started_at=ran_at)
    _dispatch(agent, key="queued", started_at=None, status=Turn.QUEUED)
    assert services.agent_detail(agent)["latest_turn_at"] == ran_at


def test_close_out_attaches_to_its_dispatch_row_instead_of_adding_one():
    agent = _agent()
    dispatched = _dispatch(agent, key="k1", task="echo-api-1234-0810-1805")
    services.upsert_turn(agent, _turn(cli_session_id="s1", title="Did the thing",
                                      emdash_task_id="echo-api-1234-0810-1805"))
    assert Turn.objects.filter(agent=agent).count() == 1  # not two halves, one turn
    dispatched.refresh_from_db()
    assert dispatched.report_title == "Did the thing"
    assert dispatched.reported_at is not None
    assert dispatched.cli_session_id == "s1"


def test_close_out_without_a_matching_dispatch_row_still_records():
    """A turn a human started by hand has no queue row. The report must not vanish."""
    agent = _agent()
    turn = services.upsert_turn(agent, _turn(cli_session_id="s9", title="Hand-run"))
    assert turn.report_title == "Hand-run"
    assert turn.status == Turn.DONE
    assert Turn.objects.filter(agent=agent).count() == 1


def test_close_out_does_not_reclaim_an_already_reported_turn():
    """Two turns through one reused emdash session: the second close-out must take
    the newer turn, not overwrite the first one's report."""
    agent = _agent()
    task = "echo-api-1234-0810-1805"
    first = _dispatch(agent, key="k1", task=task)
    services.upsert_turn(agent, _turn(cli_session_id="s1", title="First", emdash_task_id=task))
    second = _dispatch(agent, key="k2", task=task)
    services.upsert_turn(agent, _turn(cli_session_id="s2", title="Second", emdash_task_id=task))
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.report_title == "First"
    assert second.report_title == "Second"
    assert Turn.objects.filter(agent=agent).count() == 2


def test_work_products_upsert_by_url():
    agent = _agent()
    items = [SimpleNamespace(title="Story", kind="doc", url="https://d/1", description="", tags=["x"], source="echo")]
    assert services.upsert_work_products(agent, items) == {"created": 1, "replaced": 0}
    items[0].title = "Story v2"
    assert services.upsert_work_products(agent, items) == {"created": 0, "replaced": 1}
    assert AgentWorkProduct.objects.get(agent=agent).title == "Story v2"


def _skill_in(**kw):
    """Stand-in for AgentSkillIn. Mirrors its fields (and its defaults) — the
    service is deliberately strict rather than getattr-defensive, because its one
    real caller always hands it a Pydantic AgentSkillIn where the defaults are
    guaranteed. Keep this in step with the schema."""
    return SimpleNamespace(**{
        "description": "", "url": "", "improvement_note": "",
        "launchable": False, "args_hint": "",
        **kw,
    })


def test_replace_skills_mirrors_catalog():
    agent = _agent()
    services.replace_skills(agent, [
        _skill_in(name="email-communicator", description="email", url="u1"),
        _skill_in(name="story-draft", description="write", url="u2", improvement_note="fixed slop"),
    ])
    assert agent.skills.count() == 2
    services.replace_skills(agent, [
        _skill_in(name="email-communicator", description="email v2", url="u1"),
    ])
    assert agent.skills.count() == 1
    assert AgentSkill.objects.get(agent=agent).description == "email v2"


def _task(**kw):
    base = dict(ext_id="t", title="T", next_action="", status="suggested", owner="",
                assigned="", confidence="", score="", review="", rationale="", source_url="",
                plan="", due=None, links=[], notes="", position=0, source="sheet")
    base.update(kw)
    return SimpleNamespace(**base)


def test_sync_tasks_upserts_and_normalizes_status():
    agent = _agent()
    link = SimpleNamespace(model_dump=lambda: {"label": "doc", "url": "https://d/1"})
    tasks = [
        _task(ext_id="t1", title="PRIDE story", next_action="Run the interview", status="in_progress",
              owner="Sarvesh", assigned="Sarvesh", due=dt.date(2026, 6, 20), links=[link], position=0),
        _task(ext_id="t2", title="Weird status", status="banana", position=1),  # invalid -> suggested
    ]
    res = services.sync_tasks(agent, tasks)
    assert res["count"] == 2 and res["created"] == 2
    assert AgentTask.objects.get(agent=agent, ext_id="t2").status == "suggested"
    assert AgentTask.objects.get(agent=agent, ext_id="t1").assigned == "Sarvesh"
    # re-sync is NON-destructive (DB is the source of truth): updates, never deletes
    tasks[0].title = "PRIDE story v2"
    res2 = services.sync_tasks(agent, tasks[:1])
    assert res2["created"] == 0 and AgentTask.objects.filter(agent=agent).count() == 2
    assert AgentTask.objects.get(agent=agent, ext_id="t1").title == "PRIDE story v2"


def test_agenttask_run_link_round_trips():
    """A task can mean 'execute this run' — the nullable FK round-trips and
    SET_NULL leaves the task when its run is deleted."""
    agent = _agent()
    run = AgentRun.objects.create(agent=agent, label="Linked run")
    task = services.create_task(agent, _task(ext_id="t1", title="Execute the run"))
    assert task.run_id is None                                   # nullable by default
    task.run = run
    task.save(update_fields=["run"])
    task.refresh_from_db()
    assert task.run_id == run.pk and task.run == run
    assert list(run.tasks.all()) == [task]                       # reverse accessor
    run.delete()                                                 # on_delete=SET_NULL
    task.refresh_from_db()
    assert task.run_id is None and AgentTask.objects.filter(pk=task.pk).exists()


def test_agenttask_score_review_captured_at_completion():
    """A task carries a self-grade + one-line review scored WHEN it's marked
    done, so a manager sync reads completion-time scores instead of re-grading.
    Blank on suggestion; set via patch at done; surfaced on the Out schema."""
    from apps.agents.schemas import AgentTaskOut

    agent = _agent()
    t = services.create_task(agent, _task(ext_id="t1", title="Solina guide", status="in_progress"))
    assert t.score == "" and t.review == ""                       # unscored while in flight
    # completion: patch status=done with the grade captured at that moment
    services.patch_task(t, {"status": "done", "score": "A-",
                            "review": "provenance-checked; transcript-grounded"})
    t.refresh_from_db()
    assert t.status == "done" and t.score == "A-"
    assert t.review == "provenance-checked; transcript-grounded"
    # the score/review round-trip out through the API schema
    out = AgentTaskOut.model_validate(t, from_attributes=True)
    assert out.score == "A-" and out.review.startswith("provenance-checked")
    # create-time scoring works too (e.g. importing already-done history)
    t2 = services.create_task(agent, _task(ext_id="t2", title="MLC submission",
                                           status="done", score="B", review="you rewrote it to ship"))
    assert t2.score == "B" and t2.review == "you rewrote it to ship"


def test_command_flow_accept_then_apply_and_decline():
    agent = _agent()
    t = services.create_task(agent, _task(ext_id="t1", title="ZEGCAWIS story", next_action="Get consent",
                                          status="suggested", owner="Matt", confidence="high",
                                          rationale="strong near-miss", plan="email the FLW"))
    # accept: applies to the task AND leaves a pending command for the agent
    cmd = services.create_command(agent, t, "accept", {}, "jonathan@dimagi.com")
    t.refresh_from_db()
    assert t.status == "in_progress" and t.assigned == "Echo"
    assert cmd.status == "pending"
    assert [c.id for c in services.list_commands(agent, "pending")] == [cmd.id]
    services.apply_command(cmd, "drafted")
    cmd.refresh_from_db()
    assert cmd.status == "applied" and cmd.result_note == "drafted"
    # decline applies immediately (terminal) and records the reason
    cmd2 = services.create_command(agent, t, "decline", {"reason": "not now"}, "x")
    t.refresh_from_db()
    assert t.status == "declined" and "not now" in t.notes and cmd2.status == "applied"
