"""Workspace scoping for the /api/timeline feed.

The DDD timeline source (apps.runs.timeline) scopes its run + narrative-review
events to the caller's workspaces via the ``workspace_slugs`` seam the aggregator
offers each source. A member of workspace A must not see workspace B's DDD
activity.
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.runs.tests.factories import (
    add_member,
    make_review,
    make_user,
    make_walkthrough,
    make_workspace,
)

pytestmark = pytest.mark.django_db

BASE = "/api/timeline/"

NARR = {
    "gate": "concept_change",
    "narrative": "Story",
    "narration": [{"scene": 1, "id": "n1", "text": "x"}],
}

A_RUN = "alpha-2026-06-01-001"
B_RUN = "bravo-2026-06-01-001"


@pytest.fixture
def two_tenants(db):
    ua = make_user("a@dimagi.com")
    ub = make_user("b@dimagi.com")
    ws_a = make_workspace("ws-a")
    ws_b = make_workspace("ws-b")
    add_member(ws_a, ua)
    add_member(ws_b, ub)
    make_walkthrough(
        ua, kind="video", run_id=A_RUN, narrative_slug="alpha",
        role="hero_video", workspace=ws_a,
    )
    make_review(
        ua, run_id=A_RUN, version=1,
        request_json={**NARR, "run_id": A_RUN}, workspace=ws_a,
    )
    make_walkthrough(
        ub, kind="video", run_id=B_RUN, narrative_slug="bravo",
        role="hero_video", workspace=ws_b,
    )
    make_review(
        ub, run_id=B_RUN, version=1,
        request_json={**NARR, "run_id": B_RUN}, workspace=ws_b,
    )
    return ua, ub


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _ddd_ids(user):
    body = _client(user).get(BASE, {"subsystem": "ddd"}).json()
    return {e["id"] for e in body["events"]}


def test_ddd_feed_scoped_to_members_workspace(two_tenants):
    ua, ub = two_tenants

    a_ids = _ddd_ids(ua)
    assert any(i.startswith("run:" + A_RUN) for i in a_ids)  # own run present
    assert not any("bravo" in i for i in a_ids)  # B's run + review hidden

    b_ids = _ddd_ids(ub)
    assert any(i.startswith("run:" + B_RUN) for i in b_ids)
    assert not any("alpha" in i for i in b_ids)


def _sub_ids(user, subsystem):
    body = _client(user).get(BASE, {"subsystem": subsystem}).json()
    return {e["id"] for e in body["events"]}


def test_all_tenant_sources_scoped_to_members_workspace(db):
    """walkthroughs, shareouts, agents each only show the caller's
    workspace's rows — the DDD source proves reviews/runs; this proves the rest."""

    from django.utils import timezone

    from apps.agents.models import Agent, AgentSync
    from apps.shareouts.models import Shareout

    ua, ub = make_user("wa@dimagi.com"), make_user("wb@dimagi.com")
    ws_a, ws_b = make_workspace("wsa"), make_workspace("wsb")
    add_member(ws_a, ua)
    add_member(ws_b, ub)

    def seed(user, ws, tag):
        make_walkthrough(user, kind="video", workspace=ws)  # standalone
        Shareout.objects.create(
            project_slug=tag, workspace=ws,
            period_start=timezone.now(), period_end=timezone.now(),
            title=f"{tag}-shareout", content="b", source="canopy:shareout",
        )
        agent = Agent.objects.create(slug=f"{tag}-agent", name=tag, owner=user, workspace=ws)
        AgentSync.objects.create(
            agent=agent, period_start=timezone.now(), period_end=timezone.now(),
            title=f"{tag}-sync", summary="s", doc_url="https://d/x", source="x",
        )

    seed(ua, ws_a, "aaa")
    seed(ub, ws_b, "bbb")

    for sub in ("walkthroughs", "shareouts", "agents"):
        a_ids = _sub_ids(ua, sub)
        b_ids = _sub_ids(ub, sub)
        assert a_ids, f"{sub}: A should see its own rows"
        assert b_ids, f"{sub}: B should see its own rows"
        assert a_ids.isdisjoint(b_ids), f"{sub}: tenants must not share events"
