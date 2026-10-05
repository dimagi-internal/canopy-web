"""A project-dispatch turn (no agent_slug) borrows its project's configured
default identity agent — the fallback `turn_agent` added for project turns
whose `apps.projects.Project` has `default_identity_agent` set. Mirrors
runner/ec2/tests/test_github_per_turn.py's shape, server-side: that file pins
"a turn canopy refuses runs with NO GitHub identity rather than a leftover
one" for AGENT turns; this pins the same refusal, and its lifted fallback,
for PROJECT turns.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import delegations
from apps.agents.models import Agent, AgentDelegation
from apps.common.encryption import encrypt_secret
from apps.harness import services
from apps.harness.models import Turn
from apps.projects.models import Project
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


def _agent_with_github_delegation(slug, owner_email, *, login, name, token="tok"):
    op = User.objects.create_user(owner_email, owner_email, "pw")
    ws = Workspace.objects.create(slug=f"ws-{slug}", display_name=slug, created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug=slug, name=name, workspace=ws, owner=op)
    AgentDelegation.objects.create(
        user=op, agent=agent, service=AgentDelegation.GITHUB,
        secret_enc=encrypt_secret(token),
        meta={"login": login, "user_id": 1, "name": name},
    )
    return agent, ws


def _project_turn(project_slug, workspace, key):
    t, _ = services.enqueue_turn(project=project_slug, workspace=workspace,
                                 origin=Turn.ORIGIN_API, idempotency_key=key)
    return t


def test_a_project_turn_with_no_default_set_is_refused_with_no_identity():
    hal, ws = _agent_with_github_delegation("hal", "hal-owner@dimagi.com", login="hal-bot", name="Hal")
    Project.objects.create(slug="undefaulted", name="undefaulted", workspace=ws)
    turn = _project_turn("undefaulted", ws, "k-none")

    assert delegations.turn_agent(turn) is None
    with pytest.raises(delegations.DelegationError):
        delegations.github_token_for_turn(turn)


def test_a_project_turn_gets_its_projects_default_identity_agents_token():
    hal, ws = _agent_with_github_delegation("hal", "hal-owner@dimagi.com", login="hal-bot",
                                            name="Hal", token="tok-hal")
    Project.objects.create(slug="canopy-web", name="canopy-web", workspace=ws,
                           default_identity_agent=hal)
    turn = _project_turn("canopy-web", ws, "k-hal")

    assert delegations.turn_agent(turn) == hal
    issued = delegations.github_token_for_turn(turn)
    assert issued["token"] == "tok-hal"
    assert issued["github_login"] == "hal-bot"
    assert issued["git_name"] == "Hal"


def test_two_projects_with_different_default_agents_do_not_leak_into_each_other():
    hal, ws1 = _agent_with_github_delegation("hal", "hal-owner@dimagi.com", login="hal-bot",
                                             name="Hal", token="tok-hal")
    echo, ws2 = _agent_with_github_delegation("echo", "echo-owner@dimagi.com", login="echo-bot",
                                              name="Echo", token="tok-echo")
    Project.objects.create(slug="canopy-web", name="canopy-web", workspace=ws1,
                           default_identity_agent=hal)
    Project.objects.create(slug="connect-labs", name="connect-labs", workspace=ws2,
                           default_identity_agent=echo)
    hal_turn = _project_turn("canopy-web", ws1, "k-hal2")
    echo_turn = _project_turn("connect-labs", ws2, "k-echo2")

    assert delegations.github_token_for_turn(hal_turn)["token"] == "tok-hal"
    assert delegations.github_token_for_turn(echo_turn)["token"] == "tok-echo"
