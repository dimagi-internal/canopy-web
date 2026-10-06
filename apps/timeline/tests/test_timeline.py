"""Contract + source tests for the /api/timeline aggregator."""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentSync
from apps.projects.models import Project, ProjectContext
from apps.runs.tests.factories import (
    DEFAULT_TEST_WS,
    add_member,
    make_review,
    make_user,
    make_walkthrough,
    make_workspace,
)
from apps.shareouts.models import Shareout
from apps.shareouts.timeline import _period_slug

pytestmark = pytest.mark.django_db

BASE = "/api/timeline/"


@pytest.fixture
def owner(db):
    return make_user()


@pytest.fixture
def ws(owner):
    """The owner's tenant. Workspace-scoped sources (walkthroughs, shareouts,
    agents, projects) only surface rows in a workspace the caller belongs to, so
    tests place their objects here — mirrors production where the API assigns one.
    Matches the workspace make_walkthrough() uses, so all sources share a tenant."""
    w = make_workspace(DEFAULT_TEST_WS)
    add_member(w, owner)
    return w


@pytest.fixture
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _at(model_cls, pk, when, field="created_at"):
    """Force an auto_now_add timestamp for deterministic ordering."""
    model_cls.objects.filter(pk=pk).update(**{field: when})


def _aware(y, mo, d, h=12):
    return timezone.make_aware(dt.datetime(y, mo, d, h, 0, 0))


# --- merge + shape -----------------------------------------------------------


def test_requires_auth():
    assert Client().get(BASE).status_code == 401


def test_merges_across_subsystems(client, owner, ws):
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    note = ProjectContext.objects.create(
        project=project, context_type="note", content="ship it", source="x"
    )
    _at(ProjectContext, note.pk, _aware(2026, 6, 10))
    Shareout.objects.create(
        project=project,
        workspace=ws,
        period_start=_aware(2026, 6, 11, 0),
        period_end=_aware(2026, 6, 11, 23),
        title="Week of June 11",
        summary="shipped things",
        content="body",
        source="canopy:shareout",
    )
    wt = make_walkthrough(owner, kind="video")  # standalone (no run_id)
    _at(type(wt), wt.pk, _aware(2026, 6, 12))

    body = client.get(BASE).json()
    by_sub = {e["subsystem"] for e in body["events"]}
    assert {"projects", "shareouts", "walkthroughs"} <= by_sub
    # newest first
    ats = [e["at"] for e in body["events"]]
    assert ats == sorted(ats, reverse=True)
    # catalog present for the rail
    keys = {s["key"] for s in body["subsystems"]}
    assert {"ddd", "projects", "walkthroughs", "shareouts", "agents", "sessions"} <= keys
    # The Insights feed was retired (2026-10): it is no longer a rail entry.
    assert "insights" not in keys


def test_subsystem_filter(client, owner, ws):
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    ProjectContext.objects.create(
        project=project, context_type="note", content="a note", source="x"
    )
    make_walkthrough(owner, kind="video")  # standalone walkthrough

    body = client.get(BASE, {"subsystem": "projects"}).json()
    assert body["events"]
    assert all(e["subsystem"] == "projects" for e in body["events"])


def test_unknown_subsystem_falls_back_to_all(client, owner, ws):
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    ProjectContext.objects.create(
        project=project, context_type="note", content="a note", source="x"
    )
    body = client.get(BASE, {"subsystem": "bogus"}).json()
    assert body["events"]  # not an empty/error result


def test_retired_insight_rows_do_not_surface(client, owner, ws):
    """The Insights feed was retired but its rows were kept (deleting them is
    held for approval). They must stay unreachable — not resurface as generic
    project context on the timeline."""
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    ProjectContext.objects.create(
        project=project, context_type="insight", content="[stale] old card", source="x"
    )
    body = client.get(BASE).json()
    assert not [e for e in body["events"] if "old card" in (e["title"] or "")]


def test_before_cursor_paginates(client, owner, ws):
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    for i, day in enumerate((10, 11, 12)):
        Shareout.objects.create(
            project=project,
            workspace=ws,
            period_start=_aware(2026, 6, day, 0),
            period_end=_aware(2026, 6, day, 23),
            title=f"so-{i}",
            content="body",
            source="canopy:shareout",
        )
    page1 = client.get(BASE, {"subsystem": "shareouts", "limit": 2}).json()
    assert len(page1["events"]) == 2
    assert page1["next_before"] is not None
    page2 = client.get(
        BASE, {"subsystem": "shareouts", "limit": 2, "before": page1["next_before"]}
    ).json()
    titles1 = {e["title"] for e in page1["events"]}
    titles2 = {e["title"] for e in page2["events"]}
    assert titles1 == {"so-2", "so-1"}
    assert titles2 == {"so-0"}
    assert titles1.isdisjoint(titles2)


def test_cursor_no_loss_on_tied_timestamps(client, owner, ws):
    # Two shareouts stamped at the exact same instant — a strict `< at` cursor
    # would drop one when paging. The compound (at, id) cursor must surface both.
    project = Project.objects.create(name="Reef", slug="reef", workspace=ws)
    same = _aware(2026, 6, 11, 12)
    for i in range(2):
        Shareout.objects.create(
            project=project,
            workspace=ws,
            period_start=same,
            period_end=same,
            title=f"tie-{i}",
            content="body",
            source="canopy:shareout",
        )
    seen: list[str] = []
    before = None
    for _ in range(4):  # page one-at-a-time; loop-guard well above 2 events
        params = {"subsystem": "shareouts", "limit": 1}
        if before:
            params["before"] = before
        body = client.get(BASE, params).json()
        seen.extend(e["title"] for e in body["events"])
        before = body["next_before"]
        if not before:
            break
    assert sorted(seen) == ["tie-0", "tie-1"]


# --- DDD source --------------------------------------------------------------


def test_ddd_run_and_narrative_review(client, owner):
    rid = "reef-2026-06-02-001"
    make_walkthrough(owner, kind="video", run_id=rid, narrative_slug="reef", role="hero_video")
    rev = make_review(
        owner,
        run_id=rid,
        gate="concept_change",
        narrative_slug="reef",
        version=1,
        request_json={"run_id": rid, "narrative": "Reef demo\nmore"},
    )
    body = client.get(BASE, {"subsystem": "ddd"}).json()
    kinds = {e["kind"]: e for e in body["events"]}
    assert "run" in kinds and "narrative_review" in kinds
    assert kinds["run"]["href"] == f"/ddd/reef/{rid}"
    assert kinds["narrative_review"]["href"] == f"/review/{rev.id}"


def test_ddd_excludes_standalone_walkthrough_from_walkthroughs(client, owner):
    # A walkthrough tied to a run must NOT appear under the walkthroughs subsystem.
    make_walkthrough(owner, kind="video", run_id="reef-2026-06-02-001", narrative_slug="reef")
    body = client.get(BASE, {"subsystem": "walkthroughs"}).json()
    assert body["events"] == []


# --- agents ------------------------------------------------------------------


def test_agent_sync_event(client, owner, ws):
    agent = Agent.objects.create(slug="echo", name="Echo", owner=owner, workspace=ws)
    AgentSync.objects.create(
        agent=agent,
        period_start=_aware(2026, 6, 10, 0),
        period_end=_aware(2026, 6, 10, 23),
        title="Weekly sync",
        summary="did stuff",
        doc_url="https://docs.example/echo",
        source="echo",
    )
    body = client.get(BASE, {"subsystem": "agents"}).json()
    ev = body["events"][0]
    assert ev["kind"] == "sync"
    assert ev["href"] == "/agents/echo/syncs"


# --- period slug -------------------------------------------------------------


def _utc(y, mo, d, h, mi, s):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.UTC)


def test_period_slug_day_aligned():
    assert _period_slug(_utc(2026, 6, 11, 0, 0, 0), _utc(2026, 6, 11, 23, 59, 59)) == "2026-06-11"


def test_period_slug_midday_run():
    assert _period_slug(_utc(2026, 6, 11, 14, 30, 0), _utc(2026, 6, 11, 16, 0, 0)) == "2026-06-11-1430"
