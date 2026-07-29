"""`capabilities["projects"]` is REPORTED by the runner, never typed by a human.

Declared at pairing, that list drifted silently and only ever toward "cannot run":
a turn dispatched at repo `canopy` sat QUEUED forever because nobody had typed
`canopy` into it, while the repo sat in emdash's own projects table the whole time
(labs, 2026-07-28). Measured that day: emdash held 21 projects, the runner declared
10, and every declared one was also in emdash — the declaration was an arbitrary
subset of an observable fact.

So the runner reports it on every heartbeat. Routing is untouched —
`claim_next_turn` still matches `Q(project__in=runner.project_names())`; only the
WRITER changed.

The absent-vs-empty distinction carries the whole safety property, and it is the
`replace_reported_sessions` lesson with a bigger blast radius: a runner that cannot
read emdash must OMIT the field, because reporting [] would blank the stored list
and make every repo turn on that runner unclaimable. See
`docs/superpowers/specs/2026-07-28-observed-runner-projects-design.md`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def runner(owner, workspace):
    return Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, paired_by=owner, workspace=workspace,
        capabilities={"projects": ["canopy-web"], "agents": ["echo"], "sessions": True},
    )


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _beat(client, runner, **extra):
    body = {"active_turn_ids": [], "degraded": False, "note": "", **extra}
    return client.post(
        f"/api/harness/runners/{runner.id}/heartbeat", body, content_type="application/json",
    )


def _projects(runner) -> list[str]:
    runner.refresh_from_db()
    return runner.capabilities.get("projects")


def test_a_reported_list_replaces_the_stored_one(client, runner):
    """The fix, stated: `canopy` becomes routable because the box HAS it, with
    nobody editing anything."""
    assert _beat(client, runner, projects=["canopy", "canopy-web"]).status_code == 200
    assert _projects(runner) == ["canopy", "canopy-web"]


def test_a_report_is_a_replacement_not_a_union(client, runner):
    """A repo REMOVED from emdash must stop routing. A merge would make the list
    grow-only — the same staleness, just accumulating instead of missing."""
    _beat(client, runner, projects=["canopy"])
    assert _projects(runner) == ["canopy"]  # canopy-web is gone, not merged


def test_omitting_the_field_leaves_the_stored_list_alone(client, runner):
    """THE safety property. A runner that could not read emdash omits the field;
    if that were treated as an empty report it would blank the list and make every
    repo turn on this runner unclaimable. It also makes the rollout free: a runner
    on old code sends nothing and keeps working."""
    assert _beat(client, runner).status_code == 200
    assert _projects(runner) == ["canopy-web"]


def test_an_explicit_empty_report_really_does_empty_it(client, runner):
    """A fresh box with no emdash projects is a real state, and it must be
    distinguishable from 'could not tell' — which is why absence, not emptiness,
    is the no-op."""
    assert _beat(client, runner, projects=[]).status_code == 200
    assert _projects(runner) == []


def test_reporting_projects_leaves_the_other_capabilities_intact(client, runner):
    """The runner reports ONE key. `sessions` gates chat routing and `agents` is
    still read by older paths; clobbering the whole dict would silently unwire
    both."""
    _beat(client, runner, projects=["canopy"])
    runner.refresh_from_db()
    assert runner.capabilities["sessions"] is True
    assert runner.capabilities["agents"] == ["echo"]


def test_blank_names_are_dropped(client, runner):
    """A session turn has project="", so a stray "" here would make this runner
    match every session turn via `project__in`. The runner strips them too; this
    is the server not trusting a report it does not have to."""
    _beat(client, runner, projects=["canopy", "", "  "])
    assert _projects(runner) == ["canopy"]


def test_patching_projects_by_hand_is_refused(client, runner):
    """The route stays for `agents`/`sessions`, but a hand-written `projects` is
    now a ghost edit — the next heartbeat overwrites it seconds later. Better a
    loud 422 naming the real fix than a write that silently evaporates."""
    resp = client.patch(
        f"/api/harness/runners/{runner.id}",
        {"capabilities": {"projects": ["canopy"], "sessions": True}},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert "emdash" in resp.json()["detail"].lower()
    assert _projects(runner) == ["canopy-web"]  # untouched


def test_patching_other_capabilities_still_works(client, runner):
    """Only `projects` became reported. Don't break the route."""
    resp = client.patch(
        f"/api/harness/runners/{runner.id}",
        {"capabilities": {"agents": ["echo", "ada"], "sessions": False}},
        content_type="application/json",
    )
    assert resp.status_code == 200
    runner.refresh_from_db()
    assert runner.capabilities["agents"] == ["echo", "ada"]
    # The reported key survives a PATCH that doesn't mention it — it belongs to the
    # runner now, so a capabilities write must not drop it as a side effect.
    assert runner.capabilities.get("projects") == ["canopy-web"]
