"""Reading the fleet is workspace-scoped; acting on a runner stays owner-scoped.

`_runner_visibility_q` answered both questions with one predicate whose last leg
was `paired_by == caller`. That is right for acting on a runner and wrong for
seeing one: a workspace's fleet is usually paired by ONE human, so every other
member — an agent identity, a teammate — listed zero runners and could not tell
"nobody serves this repo" from "I cannot see anything at all".

That is not hypothetical. `canopy project dispatch` preflights by listing the
fleet, concluded BLOCKED from an empty list, and was routed around with
`--no-preflight`; the next dispatch went at a repo genuinely nothing declared and
sat QUEUED until the stuck-turn banner caught it (labs, 2026-07-28, turn
cec64f60). The banner could catch it precisely because `unclaimable_queued_turns`
had ALREADY made this fix at its own call site — it scopes candidate runners by
`runner_tenant_slugs`, with a comment explaining that `paired_by=user` made every
stuck turn read as `config` for anyone who had not paired a runner. This file
pins the same rule for the read the CLI actually performs.

The agreement `_runner_visibility_q`'s docstring protects — never list a runner
that every action then 404s on — is preserved as an EXPLICIT signal rather than
by making the two predicates identical: `RunnerOut.can_manage` tells the client
which listed runners it may mutate, so "you can see it, its owner must declare
on it" is sayable instead of arriving as a bare 404.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def teammate():
    """A second member of the same workspace who paired NOTHING — the agent
    identity (hal@dimagi-ai.com) whose preflight went blind."""
    return User.objects.create_user("teammate", "teammate@example.org", "pw")


@pytest.fixture()
def stranger():
    return User.objects.create_user("stranger", "stranger@example.org", "pw")


@pytest.fixture()
def workspace(owner, teammate):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=teammate, workspace=ws, role=WorkspaceMembership.EDITOR)
    return ws


@pytest.fixture()
def runner(owner, workspace):
    """Paired by `owner`, homed to the shared workspace — the laptop."""
    return Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, capabilities={"projects": ["canopy-web"]},
        paired_by=owner, workspace=workspace,
    )


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _listed(user, ws_slug: str = "") -> list[dict]:
    path = f"/api/w/{ws_slug}/harness/runners/" if ws_slug else "/api/harness/runners/"
    resp = _client(user).get(path)
    assert resp.status_code == 200
    return resp.json()


def test_workspace_member_sees_a_runner_someone_else_paired(teammate, runner):
    """The bug, stated positively: sharing the tenant is what makes a runner
    visible, not having personally paired it."""
    assert [r["name"] for r in _listed(teammate)] == ["jj-mbp-cdp"]


def test_workspace_member_sees_it_under_the_tenant_path_too(teammate, runner, workspace):
    assert [r["name"] for r in _listed(teammate, workspace.slug)] == ["jj-mbp-cdp"]


def test_a_listed_runner_says_whether_the_caller_may_mutate_it(owner, teammate, runner):
    """The replacement for list/gate identity: the list no longer implies you can
    act, so it has to SAY so. Without this the teammate's only way to learn is a
    404 from an action it was told to try."""
    assert _listed(owner)[0]["can_manage"] is True
    assert _listed(teammate)[0]["can_manage"] is False


def test_seeing_a_runner_does_not_confer_declaring_on_it(teammate, runner):
    """Act-on is UNCHANGED — the widened read must not widen the write. PATCH is
    the one the CLI reaches for next (`--declare`), so it is the one pinned."""
    resp = _client(teammate).patch(
        f"/api/harness/runners/{runner.id}",
        {"capabilities": {"projects": ["canopy-web", "canopy"]}},
        content_type="application/json",
    )
    assert resp.status_code == 404
    runner.refresh_from_db()
    assert runner.capabilities == {"projects": ["canopy-web"]}  # untouched


def test_seeing_a_runner_does_not_confer_heartbeating_as_it(teammate, runner):
    """Claiming turns AS a runner is the capability that must stay owner-only —
    heartbeat is its front door."""
    resp = _client(teammate).post(
        f"/api/harness/runners/{runner.id}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert resp.status_code == 404
    runner.refresh_from_db()
    assert runner.last_heartbeat_at is None


def test_seeing_a_runner_does_not_confer_retiring_it(teammate, runner):
    resp = _client(teammate).post(f"/api/harness/runners/{runner.id}/retire")
    assert resp.status_code == 404
    runner.refresh_from_db()
    assert runner.status != Runner.RETIRED


def test_a_non_member_still_sees_nothing(stranger, runner):
    """The workspace is the boundary the read moved ONTO, so it has to hold."""
    assert _listed(stranger) == []


def test_a_runner_in_another_workspace_is_not_listed(teammate, owner, runner):
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    Runner.objects.create(name="someone-elses", kind=Runner.EMDASH, capabilities={},
                          paired_by=owner, workspace=other)
    assert [r["name"] for r in _listed(teammate)] == ["jj-mbp-cdp"]


def test_a_null_workspace_runner_is_visible_only_to_whoever_paired_it(owner, teammate, workspace):
    """The act-on predicate's `workspace_id__isnull=True` leg must NOT come along
    into the widened read. There it is backstopped by ownership ("your own legacy
    runner"); with ownership dropped the same leg means "everyone's" — the
    NULL-means-allow shape removed from six predicates already. A runner with no
    workspace has no tenant to share, so sharing a tenant cannot reveal it.
    """
    legacy = Runner.objects.create(name="legacy-box", kind=Runner.EMDASH, capabilities={},
                                   paired_by=owner, workspace=None)
    assert "legacy-box" in [r["name"] for r in _listed(owner)]
    assert "legacy-box" not in [r["name"] for r in _listed(teammate)]
    assert legacy.workspace_id is None  # the row really is untenanted


def test_a_member_can_pin_a_turn_to_a_runner_they_did_not_pair(teammate, workspace, runner):
    """The pin arm asks 'can the caller SEE this runner?' — its own comment says
    so ("a runner the caller cannot see must 422 as unknown, never be attachable
    because its UUID was guessed"). It confers nothing new: any member can already
    enqueue a turn this runner will claim, and the claim path re-checks the tenant.
    """
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    resp = _client(teammate).post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "api", "idempotency_key": "pin-1",
         "prompt": "/echo:turn", "runner_id": str(runner.id)},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content


def test_a_non_member_still_cannot_pin_to_the_runner(stranger, workspace, runner):
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    resp = _client(stranger).post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "api", "idempotency_key": "pin-2",
         "prompt": "/echo:turn", "runner_id": str(runner.id)},
        content_type="application/json",
    )
    # 404 on the agent (no membership) is reached first; either way nothing is pinned.
    assert resp.status_code in (404, 422)
