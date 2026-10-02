"""Repo turns route by the workspace's runner order.

A repo turn (a project dispatch: no agent, no chat session) never had a ranking —
`_assignment_allows` only ever ran for agent turns — so every online runner that
declared the repo raced for it and the first poll won. Observed 2026-10-02: a
canopy-web dispatch was claimed by a second macOS account's runner 126 ms after
enqueue while the owner's runner, rank 0 in every agent's order, was online.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.harness import services
from apps.harness.models import Runner, Turn, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, name, projects=("canopy-web",), **kw):
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, paired_by=pairer,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": list(projects)}, **kw,
    )


def _turn(ws, key="p1", project="canopy-web", age_seconds=0):
    t = Turn.objects.create(project=project, workspace=ws, origin=Turn.ORIGIN_API,
                            idempotency_key=key, prompt="fix it")
    if age_seconds:
        Turn.objects.filter(pk=t.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=age_seconds))
    return t


def _order(ws, *runners, disabled=()):
    for i, r in enumerate(runners):
        WorkspaceRunnerOrder.objects.create(workspace=ws, runner=r, rank=i,
                                            enabled=r not in disabled)


@pytest.fixture
def fleet():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    return jj, ws, _runner(jj, "jj-mbp"), _runner(jj, "acedimagi-mbp"), _runner(jj, "haldimagi-mbp")


def test_without_an_order_any_declaring_runner_claims(fleet):
    """Opt-in: a workspace with no order routes exactly as before."""
    _jj, ws, _first, _second, hal = fleet
    turn = _turn(ws)
    assert services.claim_next_turn(hal).pk == turn.pk


def test_an_unlisted_runner_never_claims(fleet):
    """The 2026-10-02 case: haldimagi polled first and won. Not any more."""
    _jj, ws, first, second, hal = fleet
    _order(ws, first, second)
    _turn(ws, age_seconds=3600)  # past the grace too — unlisted is unlisted
    assert services.claim_next_turn(hal) is None


def test_rank_one_waits_while_rank_zero_is_available(fleet):
    _jj, ws, first, second, _hal = fleet
    _order(ws, first, second)
    turn = _turn(ws)
    assert services.claim_next_turn(second) is None
    assert services.claim_next_turn(first).pk == turn.pk


def test_rank_one_claims_when_rank_zero_is_offline(fleet):
    _jj, ws, first, second, _hal = fleet
    _order(ws, first, second)
    Runner.objects.filter(pk=first.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    turn = _turn(ws)
    assert services.claim_next_turn(second).pk == turn.pk


def test_rank_one_claims_after_the_grace(fleet):
    """An online-but-wedged rank 0 must not park the queue — the agent cascade's grace."""
    _jj, ws, first, second, _hal = fleet
    _order(ws, first, second)
    turn = _turn(ws, age_seconds=services.CASCADE_GRACE_SECONDS + 5)
    assert services.claim_next_turn(second).pk == turn.pk


def test_a_better_rank_that_does_not_declare_the_repo_does_not_block(fleet):
    jj, ws, _first, second, _hal = fleet
    elsewhere = _runner(jj, "cloud", projects=("connect-labs",))
    _order(ws, elsewhere, second)
    turn = _turn(ws)
    assert services.claim_next_turn(second).pk == turn.pk


def test_a_disabled_row_neither_claims_nor_blocks(fleet):
    _jj, ws, first, second, _hal = fleet
    _order(ws, first, second, disabled=(first,))
    assert services.claim_next_turn(first) is None
    turn = _turn(ws)
    assert services.claim_next_turn(second).pk == turn.pk


def test_all_rows_disabled_means_no_order(fleet):
    _jj, ws, first, _second, hal = fleet
    _order(ws, first, disabled=(first,))
    turn = _turn(ws)
    assert services.claim_next_turn(hal).pk == turn.pk


def test_an_order_does_not_touch_agent_turns(fleet):
    """Agent turns keep their own ranked list; the workspace order is repo-only."""
    from apps.agents.models import Agent
    from apps.harness.models import RunnerAssignment

    _jj, ws, first, _second, hal = fleet
    _order(ws, first)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    RunnerAssignment.objects.create(agent=agent, runner=hal, rank=0)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="a1")
    assert services.claim_next_turn(hal).pk == turn.pk


def test_coverage_agrees_an_unlisted_runner_is_not_coverage(fleet):
    """The stuck-turn warning must say what claiming does: with only an unlisted
    runner online, the turn is waiting on its ordered runners (`offline`), and
    with none of them able to serve it at all it is `config`."""
    _jj, ws, first, _second, _hal = fleet
    _order(ws, first)
    Runner.objects.filter(pk=first.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    turn = _turn(ws)
    reach = services.turn_reach(turn)
    assert reach.kind == services.OFFLINE
    assert [r.name for r in reach.runners] == ["jj-mbp"]


# ---- API -------------------------------------------------------------------------

def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_put_then_get_round_trips_the_order(fleet):
    jj, _ws_, first, second, _hal = fleet
    c = _client(jj)
    resp = c.put("/api/workspaces/dimagi/runner-order", data=json.dumps({"runners": [
        {"runner_id": str(first.id)}, {"runner_id": str(second.id), "enabled": False},
    ]}), content_type="application/json")
    assert resp.status_code == 200, resp.content
    got = c.get("/api/workspaces/dimagi/runner-order").json()
    assert [(r["runner_name"], r["rank"], r["enabled"]) for r in got] == [
        ("jj-mbp", 0, True), ("acedimagi-mbp", 1, False)]
    assert got[0]["projects"] == ["canopy-web"]


def test_put_refuses_a_runner_that_cannot_serve_the_workspace(fleet):
    jj, _ws_, first, _second, _hal = fleet
    outsider = _runner(_user("mallory"), "mallory-box")
    resp = _client(jj).put("/api/workspaces/dimagi/runner-order", data=json.dumps({"runners": [
        {"runner_id": str(first.id)}, {"runner_id": str(outsider.id)},
    ]}), content_type="application/json")
    assert resp.status_code == 422
    assert "mallory-box" in resp.content.decode()
    assert not WorkspaceRunnerOrder.objects.exists()


def test_put_needs_the_editor_tier(fleet):
    _jj, ws, first, _second, _hal = fleet
    viewer = _user("viewer")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    resp = _client(viewer).put("/api/workspaces/dimagi/runner-order", data=json.dumps(
        {"runners": [{"runner_id": str(first.id)}]}), content_type="application/json")
    assert resp.status_code == 403


def test_an_empty_put_removes_the_order(fleet):
    jj, ws, first, _second, _hal = fleet
    _order(ws, first)
    resp = _client(jj).put("/api/workspaces/dimagi/runner-order",
                           data=json.dumps({"runners": []}), content_type="application/json")
    assert resp.status_code == 200 and resp.json() == []
