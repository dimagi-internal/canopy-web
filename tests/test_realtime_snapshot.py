"""Tenancy coverage for `apps.realtime.snapshot.supervisor_snapshot`.

The one changed site in the `agents/0013` NOT-NULL PR with zero test coverage:
the reviewer deleted its tenancy filter outright (agents in every workspace
visible to every connected supervisor socket) and the full 1500+ test suite
still passed. `test_realtime_supervisor_consumer.py` exercises the consumer's
connect/auth lifecycle end-to-end but only ever constructs a single-workspace
fixture, so it never puts an agent the connecting user cannot see in front of
it. This pins the predicate directly against the snapshot builder — the
function whose whole job is to decide what a user's socket is allowed to see.
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.realtime.snapshot import supervisor_snapshot
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db


def test_snapshot_excludes_an_agent_in_a_workspace_the_user_is_not_a_member_of():
    """An agent homed in workspace A must never appear in the supervisor
    snapshot for a user who is only a member of workspace B."""
    ws_a = a_workspace("workspace-a")
    ws_b = a_workspace("workspace-b")
    user_b = a_member(ws_b, email="member-b@dimagi.com")

    Agent.objects.create(slug="agent-a", name="Agent A", workspace=ws_a)
    agent_b = Agent.objects.create(slug="agent-b", name="Agent B", workspace=ws_b)

    snap = supervisor_snapshot(user_b)

    assert "agent-a" not in snap["waiting"]
    assert agent_b.slug in snap["waiting"]


def test_snapshot_includes_agents_from_every_workspace_the_user_belongs_to():
    """A user in two workspaces sees both agents — the filter is membership,
    not "exactly one" workspace."""
    ws_a = a_workspace("workspace-a")
    ws_b = a_workspace("workspace-b")
    user = a_member(ws_a, email="both@dimagi.com")
    a_member(ws_b, email="both@dimagi.com")  # same user, second membership

    agent_a = Agent.objects.create(slug="agent-a", name="Agent A", workspace=ws_a)
    agent_b = Agent.objects.create(slug="agent-b", name="Agent B", workspace=ws_b)

    snap = supervisor_snapshot(user)

    assert agent_a.slug in snap["waiting"]
    assert agent_b.slug in snap["waiting"]
