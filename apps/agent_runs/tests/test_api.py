"""Contract tests for the unified run-lifecycle REST surface (apps/agent_runs/api.py).

Exercises every endpoint via the Django test client against the live
`DbRunStore` (the resolver's default): create a run -> add steps -> gate ->
get the full read model -> fork. Plus auth + 404 paths.

The endpoints are store-agnostic — these tests prove the wiring (router →
resolver → DbRunStore → ORM → read model) end to end through HTTP.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agent_runs.models import AgentRun, AgentRunStep
from apps.agents.models import Agent
from apps.workspaces.models import Workspace

User = get_user_model()

pytestmark = pytest.mark.django_db


# ---- helpers ----
def _make_user(username="alice", email="alice@dimagi.com"):
    return User.objects.create_user(username=username, email=email, password="pw")


def _auth_client(user=None):
    c = Client()
    c.force_login(user or _make_user())
    return c


def _post(client, url, data):
    return client.post(url, data=json.dumps(data), content_type="application/json")


@pytest.fixture
def workspace():
    # apps.agent_runs.api._get_agent_or_404 now fails CLOSED on an unhomed
    # agent (an agent.workspace_id IS NULL used to short-circuit the tenant
    # check to "allow" — see the fix's commit for the full story), so the
    # `agent` fixture below must be homed like a real agent. Every test user
    # here is @dimagi.com, so homing to the "dimagi" auto-join workspace means
    # each fresh test user is auto-joined as a member with no extra fixture
    # wiring per test.
    bootstrap = User.objects.create_user(
        username="workspace-bootstrap", email="bootstrap@dimagi.com", password="pw"
    )
    return Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", created_by=bootstrap,
        auto_join_domains=["dimagi.com"],
    )


@pytest.fixture
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


def _base(slug="echo"):
    return f"/api/agents/{slug}/runs/"


# ---- auth ----
def test_list_runs_requires_auth(agent):
    resp = Client().get(_base())
    assert resp.status_code == 401


# ---- create + list ----
def test_create_run_with_steps(agent):
    c = _auth_client()
    resp = _post(c, _base(), {
        "label": "demo",
        "mode": "review",
        "current_step": "spec",
        "session_link": "https://example.com/s",
        "steps": [
            {"key": "spec", "ordinal": 0, "title": "Spec"},
            {"key": "render", "ordinal": 1, "title": "Render"},
        ],
    })
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["agent_slug"] == "echo"
    assert body["label"] == "demo"
    # two pending steps → derived in_progress
    assert body["status"] == "in_progress"
    # actually persisted via DbRunStore
    run = AgentRun.objects.get(pk=body["id"])
    assert run.steps.count() == 2


def test_list_runs_returns_paginated_summaries(agent):
    c = _auth_client()
    _post(c, _base(), {"label": "one", "steps": []})
    _post(c, _base(), {"label": "two", "steps": []})
    resp = c.get(_base())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {r["label"] for r in body["items"]} == {"one", "two"}


# ---- full read model ----
def test_get_run_returns_full_read_model(agent):
    c = _auth_client()
    rid = _post(c, _base(), {
        "label": "demo",
        "steps": [{"key": "spec", "ordinal": 0}, {"key": "render", "ordinal": 1}],
    }).json()["id"]

    resp = c.get(f"{_base()}{rid}/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == rid
    assert [s["key"] for s in body["steps"]] == ["spec", "render"]
    # the full read model carries all the attached collections
    for key in ("steps", "artifacts", "verdicts", "decisions", "gates"):
        assert key in body


def test_get_run_404(agent):
    c = _auth_client()
    resp = c.get(f"{_base()}999999/")
    assert resp.status_code == 404


# ---- steps ----
def test_list_steps(agent):
    c = _auth_client()
    rid = _post(c, _base(), {
        "steps": [{"key": "spec", "ordinal": 0}, {"key": "render", "ordinal": 1}],
    }).json()["id"]
    resp = c.get(f"{_base()}{rid}/steps/")
    assert resp.status_code == 200
    assert [s["key"] for s in resp.json()] == ["spec", "render"]


# ---- gate ----
def test_record_gate(agent):
    c = _auth_client(_make_user(email="jj@dimagi.com"))
    rid = _post(c, _base(), {
        "steps": [{"key": "render", "ordinal": 0, "status": "running"}],
    }).json()["id"]
    # seed an open gate on the step
    step = AgentRunStep.objects.get(run_id=rid, key="render")
    from apps.agent_runs.models import AgentRunGate
    AgentRunGate.objects.create(step=step)

    resp = _post(c, f"{_base()}{rid}/steps/render/gate", {
        "decision": "approve", "note": "lgtm",
    })
    assert resp.status_code == 201, resp.content
    gate = resp.json()
    assert gate["decision"] == "approve"
    assert gate["decided_at"] is not None
    # decided_by defaults to the authed user's email
    assert gate["decided_by"] == "jj@dimagi.com"

    # reflected in the full read model
    body = c.get(f"{_base()}{rid}/").json()
    assert body["gates"][0]["decision"] == "approve"


def test_gate_missing_step_404(agent):
    c = _auth_client()
    rid = _post(c, _base(), {"steps": [{"key": "spec", "ordinal": 0}]}).json()["id"]
    resp = _post(c, f"{_base()}{rid}/steps/nope/gate", {"decision": "approve"})
    assert resp.status_code == 404


def test_record_verdict_and_aggregate(agent):
    c = _auth_client()
    rid = _post(c, _base(), {
        "steps": [{"key": "render", "ordinal": 0, "status": "running"}],
    }).json()["id"]
    resp = _post(c, f"{_base()}{rid}/steps/render/verdict", {
        "kind": "judge", "score": 82.0, "rationale": "solid",
    })
    assert resp.status_code == 201, resp.content
    v = resp.json()
    assert v["kind"] == "judge" and v["score"] == 82.0 and v["step_key"] == "render"
    # reflected in the run read model + the run-level aggregate
    body = c.get(f"{_base()}{rid}/").json()
    assert body["verdicts"][0]["score"] == 82.0
    assert body["overall_score"] == 82.0
    assert body["qa_gate_ok"] is True


def test_verdict_missing_step_404(agent):
    c = _auth_client()
    rid = _post(c, _base(), {"steps": [{"key": "spec", "ordinal": 0}]}).json()["id"]
    resp = _post(c, f"{_base()}{rid}/steps/nope/verdict", {"kind": "qa", "passed": True})
    assert resp.status_code == 404


# ---- fork ----
def test_fork_run(agent):
    c = _auth_client()
    rid = _post(c, _base(), {
        "label": "parent",
        "steps": [
            {"key": "spec", "ordinal": 0, "status": "complete"},
            {"key": "render", "ordinal": 1, "status": "running"},
        ],
    }).json()["id"]

    resp = _post(c, f"{_base()}{rid}/fork", {"at_step": "render", "mode": "keep-all"})
    assert resp.status_code == 201, resp.content
    forked = resp.json()
    assert forked["forked_from"] == rid
    assert forked["id"] != rid

    # the forked run kept `spec` (pre-fork, complete) and reset `render` to pending
    body = c.get(f"{_base()}{forked['id']}/").json()
    by_key = {s["key"]: s["status"] for s in body["steps"]}
    assert by_key["spec"] == "complete"
    assert by_key["render"] == "pending"


def test_fork_unknown_step_400(agent):
    c = _auth_client()
    rid = _post(c, _base(), {"steps": [{"key": "spec", "ordinal": 0}]}).json()["id"]
    resp = _post(c, f"{_base()}{rid}/fork", {"at_step": "nope"})
    assert resp.status_code == 400


def test_unknown_agent_404():
    c = _auth_client()
    resp = c.get(_base("ghost"))
    assert resp.status_code == 404
