"""Huddles — a team of agents syncs, led by one of them (canopy
docs/superpowers/specs/2026-10-06-huddle-design.md). canopy-web stores NOTHING new
for a huddle: it is derived from tagged turns, close-out reports and board tasks.
These tests pin the tags going in (close-out `origin_ref`, turn filters) and the
derived view coming out (`/api/huddles/`)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=w, role=WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def agents(ws):
    return {s: Agent.objects.create(slug=s, name=s.title(), workspace=ws)
            for s in ("ada", "eva", "echo")}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ── A1: close-out origin_ref + turn filters ──────────────────────────────────


def test_closeout_records_origin_ref(owner, agents):
    r = _client(owner).post("/api/agents/ada/turns/", {
        "cli_session_id": "huddle:work-fleet-20261006", "title": "Huddle work-fleet-20261006",
        "origin_ref": {"kind": "huddle", "huddle": "work-fleet-20261006"}},
        content_type="application/json")
    assert r.status_code == 201, r.content
    t = Turn.objects.get(cli_session_id="huddle:work-fleet-20261006")
    assert t.origin_ref["kind"] == "huddle"


def test_closeout_merges_origin_ref_into_existing_row(owner, agents):
    body = {"cli_session_id": "huddle:h1", "title": "x", "origin_ref": {"kind": "huddle", "huddle": "h1"}}
    _client(owner).post("/api/agents/ada/turns/", body, content_type="application/json")
    body2 = {**body, "summary": "done", "origin_ref": {"finished_at": "2026-10-06T12:00:00Z"}}
    _client(owner).post("/api/agents/ada/turns/", body2, content_type="application/json")
    t = Turn.objects.get(cli_session_id="huddle:h1")
    assert t.origin_ref == {"kind": "huddle", "huddle": "h1", "finished_at": "2026-10-06T12:00:00Z"}
    assert t.report_summary == "done"


def test_closeout_without_origin_ref_keeps_existing(owner, agents):
    body = {"cli_session_id": "huddle:h1", "title": "x", "origin_ref": {"kind": "huddle", "huddle": "h1"}}
    c = _client(owner)
    c.post("/api/agents/ada/turns/", body, content_type="application/json")
    c.post("/api/agents/ada/turns/", {"cli_session_id": "huddle:h1", "title": "y"},
           content_type="application/json")
    assert Turn.objects.get(cli_session_id="huddle:h1").origin_ref == {"kind": "huddle", "huddle": "h1"}


def test_list_turns_filters_by_huddle_and_parent(owner, agents):
    anchor = Turn.objects.create(agent=agents["ada"], idempotency_key="a",
                                 origin_ref={"kind": "huddle", "huddle": "h1"})
    mine = Turn.objects.create(agent=agents["eva"], idempotency_key="b", parent_turn=anchor,
                               origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 1})
    Turn.objects.create(agent=agents["eva"], idempotency_key="c",
                        origin_ref={"kind": "huddle_round", "huddle": "h2", "round": 1})
    c = _client(owner)
    ids = {t["id"] for t in c.get("/api/harness/turns/?huddle=h1").json()}
    assert ids == {str(anchor.id), str(mine.id)}
    ids = {t["id"] for t in c.get(f"/api/harness/turns/?parent_turn={anchor.id}").json()}
    assert ids == {str(mine.id)}


def test_turn_filters_stay_tenant_filtered(agents):
    Turn.objects.create(agent=agents["ada"], idempotency_key="a",
                        origin_ref={"kind": "huddle", "huddle": "h1"})
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    assert _client(stranger).get("/api/harness/turns/?huddle=h1").json() == []
