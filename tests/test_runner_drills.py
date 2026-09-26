"""Readiness drills — pinned doctor turns, report callback, per-pair outcomes.

See docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md (Task 7).
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, RunnerDrill, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def test_start_drill_endpoint_empty_agents_list_is_422(django_user_model):
    # payload.agents=[] must narrow to "drill nothing" (422), not be treated
    # the same as omitted/None ("drill everyone assigned").
    u = django_user_model.objects.create_user(username="o3", password="x")
    client = Client()
    client.force_login(u)
    r = Runner.objects.create(name="s3", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo3", name="echo3", workspace=a_workspace())
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)

    resp = client.post(
        f"/api/harness/runners/{r.id}/drill", data={"agents": []}, content_type="application/json",
    )
    assert resp.status_code == 422, resp.content


def test_start_drill_endpoint_omitted_agents_drills_all_assigned(django_user_model):
    u = django_user_model.objects.create_user(username="o4", password="x")
    client = Client()
    client.force_login(u)
    r = Runner.objects.create(name="s4", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s, workspace=a_workspace()) for s in ("echo4", "ada4"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)

    resp = client.post(
        f"/api/harness/runners/{r.id}/drill", data={}, content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert len(resp.json()) == 2


def test_start_drill_fans_out_pinned_pending_turns(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="standby", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s, workspace=a_workspace()) for s in ("echo", "ada"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)
    drills = services.start_drill(r, [a1, a2])
    assert {d.outcome for d in drills} == {RunnerDrill.OUTCOME_PENDING}
    turns = Turn.objects.filter(origin=Turn.ORIGIN_API)
    assert turns.count() == 2
    assert all(t.pinned_runner_id == r.id for t in turns)
    assert "read-only" in turns.first().prompt.lower()


def test_report_drill_resolves_outcome(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u)
    a = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    d = RunnerDrill.objects.create(runner=r, agent=a)
    services.report_drill(d, outcome="pass", summary="all checks green")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PASS and d.finished_at is not None


def test_failed_drill_turn_marks_drill_fail(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    turn = Turn.objects.get(origin=Turn.ORIGIN_API)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=r)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.FAILED, result_note="claude auth expired")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_FAIL
    assert "claude auth expired" in d.summary


def test_lost_drill_turn_marks_drill_fail(django_user_model):
    # A LOST turn is marked via sweep_expired_leases's queryset update, which
    # bypasses finish_turn entirely — so the FAILED-drill hook there never
    # fires on its own. Assert sweep_expired_leases mirrors that hook so a
    # drill whose runner disappears mid-turn doesn't strand as pending forever.
    u = django_user_model.objects.create_user(username="o5", password="x")
    r = Runner.objects.create(name="s5", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo5", name="Echo5", workspace=a_workspace())
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    turn = Turn.objects.get(origin=Turn.ORIGIN_API)
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.CLAIMED, claimed_by=r,
        lease_expires_at=timezone.now() - timezone.timedelta(minutes=5),
    )
    swept = services.sweep_expired_leases()
    assert swept == 1
    turn.refresh_from_db()
    assert turn.status == Turn.LOST
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_FAIL
    assert "lost" in d.summary.lower()
    assert d.finished_at is not None


def test_redrill_resets_to_pending(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    services.report_drill(d, outcome="pass", summary="ok")
    [d2] = services.start_drill(r, [a])
    assert d2.pk == d.pk and d2.outcome == RunnerDrill.OUTCOME_PENDING


def test_list_runners_includes_drill_rollup(django_user_model):
    """After a drill fan-out + one report, GET /api/harness/runners/ carries the
    rollup RunnerOut.resolve_drill_rollup computes — right counts, right
    last_finished_at — not just the bare RunnerDrill rows list_runner_drills
    already exposed."""
    u = django_user_model.objects.create_user(username="o", password="x")
    client = Client()
    client.force_login(u)
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s, workspace=a_workspace()) for s in ("echo", "ada"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)
    d1, d2 = services.start_drill(r, [a1, a2])
    services.report_drill(d1, outcome="pass", summary="all checks green")

    resp = client.get("/api/harness/runners/")
    assert resp.status_code == 200
    [row] = resp.json()
    rollup = row["drill_rollup"]
    assert rollup is not None
    assert rollup["passed"] == 1
    assert rollup["failed"] == 0
    assert rollup["pending"] == 1
    d1.refresh_from_db()
    assert rollup["last_finished_at"] is not None
    assert rollup["last_finished_at"].startswith(d1.finished_at.isoformat()[:19])


def test_list_runners_drill_rollup_none_when_never_drilled(django_user_model):
    u = django_user_model.objects.create_user(username="o2", password="x")
    client = Client()
    client.force_login(u)
    Runner.objects.create(name="fresh", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                          last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    resp = client.get("/api/harness/runners/")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["drill_rollup"] is None


# --- The drilled agent reports as ITSELF (2026-09-22) -------------------------
# Agents carry their own canopy login (per-agent PATs, `Agent.user`), and a
# well-behaved agent refuses to borrow the operator's token. The report gate was
# runner-OWNER only, so hal's correct report 404'd ("runner not found") and the
# drill stranded pending while its turn finished DONE — observed on cloud-ec2-1.


def _drilled(django_user_model, *, agent_user=None):
    owner = django_user_model.objects.create_user(username="owner", password="x")
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={}, paired_by=owner,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="hal", name="Hal", workspace=a_workspace(), user=agent_user)
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    return owner, r, a, d


def _report(user, drill, outcome="pass"):
    client = Client()
    client.force_login(user)
    return client.post(f"/api/harness/drills/{drill.id}/report",
                       data={"outcome": outcome, "summary": "checks ran"},
                       content_type="application/json")


def test_drilled_agent_may_report_under_its_own_identity(django_user_model):
    hal_user = django_user_model.objects.create_user(username="hal-bot", password="x")
    _, _, _, d = _drilled(django_user_model, agent_user=hal_user)
    resp = _report(hal_user, d)
    assert resp.status_code == 200, resp.content
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PASS


def test_runner_owner_may_still_report(django_user_model):
    owner, _, _, d = _drilled(django_user_model)
    assert _report(owner, d).status_code == 200


def test_a_stranger_may_not_report(django_user_model):
    hal_user = django_user_model.objects.create_user(username="hal-bot", password="x")
    _, _, _, d = _drilled(django_user_model, agent_user=hal_user)
    stranger = django_user_model.objects.create_user(username="stranger", password="x")
    assert _report(stranger, d).status_code == 404
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PENDING


def test_another_agents_identity_may_not_report(django_user_model):
    # Identity is per AGENT: echo's login is not hal's.
    hal_user = django_user_model.objects.create_user(username="hal-bot", password="x")
    _, _, _, d = _drilled(django_user_model, agent_user=hal_user)
    echo_user = django_user_model.objects.create_user(username="echo-bot", password="x")
    Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace(), user=echo_user)
    assert _report(echo_user, d).status_code == 404


def _finish(r, status):
    turn = Turn.objects.get(origin=Turn.ORIGIN_API)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=r)
    turn.refresh_from_db()
    services.finish_turn(turn, status=status, result_note="Readiness drill complete.")


def test_done_drill_turn_without_a_report_fails_the_drill(django_user_model):
    _, r, _, d = _drilled(django_user_model)
    _finish(r, Turn.DONE)
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_FAIL
    assert "without reporting" in d.summary
    assert d.finished_at is not None


def test_done_drill_turn_keeps_a_report_it_already_got(django_user_model):
    owner, r, _, d = _drilled(django_user_model)
    services.report_drill(d, outcome="pass", summary="all green")
    _finish(r, Turn.DONE)
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PASS and d.summary == "all green"


def test_drill_prompt_prefers_the_agents_own_token(django_user_model):
    _drilled(django_user_model)
    prompt = Turn.objects.get(origin=Turn.ORIGIN_API).prompt
    own = prompt.index("CANOPY_WEB_PAT")
    borrowed = prompt.index("workbench-token")
    assert own < borrowed
    assert "~/.hal/.env" in prompt


def test_the_signed_report_link_lets_any_login_report_that_run_and_only_that_run(django_user_model):
    """An agent reports through its run's signed link, whichever canopy login it
    happens to hold. Echo reported as echo@dimagi-ai.com — a login not linked to
    its agent row — and was 404'd, so a passing check read as a dead box
    (cloud-ec2-1, 2026-09-26)."""
    import re

    owner = django_user_model.objects.create_user(username="own", email="own@dimagi.com", password="x")
    stranger = django_user_model.objects.create_user(username="echo-login", email="echo@x.org", password="x")
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={}, paired_by=owner,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo7", name="Echo", workspace=a_workspace())
    [drill] = services.start_drill(r, [a])
    link = re.search(r'-X POST "([^"]+)"', drill.turn.prompt).group(1)
    path, query = link.split("/api/harness/", 1)[1].split("?", 1)

    c = Client()
    c.force_login(stranger)
    body = {"outcome": "pass", "summary": "ok"}
    # Without the link, a login that is neither the agent's nor the pairer's: 404.
    assert c.post(f"/api/harness/{path}", body, content_type="application/json").status_code == 404
    # A tampered link: still 404.
    assert c.post(f"/api/harness/{path}?t=x{query[2:]}", body,
                  content_type="application/json").status_code == 404
    # The run's own link: accepted.
    ok = c.post(f"/api/harness/{path}?{query}", body, content_type="application/json")
    assert ok.status_code == 200, ok.content
    assert ok.json()["outcome"] == "pass"

    # A NEW run of the same (runner, agent) reuses the row; the old link must not answer it.
    services.start_drill(r, [a])
    stale = c.post(f"/api/harness/{path}?{query}", body, content_type="application/json")
    assert stale.status_code == 404
