"""Runners poll `GET /runners/{id}/vault-keeper` every few minutes, so what it costs is
bounded: the delegated half asks only about entries sealed to this box, and both halves
read the fleet once for every agent the box holds, not once per agent per question.

Fixture: `n` agents, each with its own owner, cloud runner, machine and a delegated
GitHub token, and a second machine of that runner waiting for a copy. `shared=True`
also has the first runner run every agent, so it holds every vault key and token."""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.credentials import accounts, delegations, vault_keeper
from apps.agents.models import Agent, AgentAdmin, AgentVaultWrap
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.machines import a_machine

pytestmark = pytest.mark.django_db


def _fleet(n: int, *, shared: bool, p: str = "a") -> Runner:
    ws = a_workspace()
    fake = FakeGitHub(pr={f"dimagi-internal/{p}{i}": 422 for i in range(n)})
    first = None
    with mock.patch.object(delegations.requests, "get", fake.get), \
            mock.patch.object(delegations.requests, "post", fake.post):
        for i in range(n):
            owner = User.objects.create_user(f"{p}o{i}", f"{p}o{i}@dimagi.com", "pw")
            wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
            agent = Agent.objects.create(slug=f"{p}{i}", name=f"{p}{i}", workspace=ws, owner=owner,
                                         repo_url=f"git@github.com:dimagi-internal/{p}{i}.git", zk_kv=1)
            runner = Runner.objects.create(name=f"{p}-cloud-{i}", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                           last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
            RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
            key, _, _ = a_machine(runner, owner)
            if first is None:
                first = (runner, key, owner)
            elif shared:
                AgentAdmin.objects.create(agent=agent, user=first[2])
                RunnerAssignment.objects.create(agent=agent, runner=first[0], rank=1)
                AgentVaultWrap.objects.create(agent=agent, kind=AgentVaultWrap.DEVICE, device_key=first[1],
                                              kv=1, wrap="w")
            delegations.set_github(agent, owner, "github_pat_x")
            AgentVaultWrap.objects.create(agent=agent, kind=AgentVaultWrap.DEVICE, device_key=key, kv=1, wrap="w")
            a_machine(runner, owner)  # approved after the token was sealed: it waits for a keeper
    return first[0]


def _count(fn, runner):
    with CaptureQueriesContext(connection) as q:
        out = fn(runner)
    return len(q), out


def test_the_delegated_work_does_not_grow_with_other_peoples_agents():
    small = _fleet(2, shared=False)
    n_small, work_small = _count(accounts.keeper_work, small)
    big = _fleet(8, shared=False, p="b")
    n_big, work_big = _count(accounts.keeper_work, big)
    assert [w["agent"] for w in work_small] == ["a0"] and [w["agent"] for w in work_big] == ["b0"]
    assert n_big == n_small


def test_a_box_with_nothing_sealed_to_it_answers_with_one_read():
    _fleet(3, shared=False)
    idle = Runner.objects.create(name="idle", kind=Runner.CLOUD, status=Runner.ONLINE)
    n, work = _count(accounts.keeper_work, idle)
    assert work == [] and n == 1


def test_a_box_that_holds_every_agent_lists_each_within_a_bound():
    runner = _fleet(6, shared=True)
    n_delegated, delegated = _count(accounts.keeper_work, runner)
    n_vault, vaults = _count(vault_keeper.work, runner)
    assert sorted(w["agent"] for w in delegated) == [f"a{i}" for i in range(6)]
    assert sorted(w["agent"] for w in vaults) == [f"a{i}" for i in range(6)]
    for w in delegated:
        assert len(w["waiting"]) >= 1 and w["device_jkt"]
    # Measured: 145 and 218 queries (877 and 767 when each question read the fleet).
    assert n_delegated <= 200, n_delegated
    assert n_vault <= 300, n_vault


def test_the_keeper_route_reads_each_vaults_members_once_and_answers_the_same():
    """`GET /vault-keeper` asks each vault's members from several rules (who keeps, who
    waits, whose endorsements count) and each owner's license of their boxes: inside
    its `read_once` blocks each is worked out once, and the answer does not change."""
    from apps.agents.credentials import licenses, vault_members

    runner = _fleet(6, shared=True)
    n_plain, plain = _count(lambda r: (vault_keeper.work(r), accounts.keeper_work(r)), runner)
    with licenses.read_once(), vault_members.read_once():
        n_once, once = _count(lambda r: (vault_keeper.work(r), accounts.keeper_work(r)), runner)
    assert once == plain
    # Measured: 219 against 363.
    assert n_once <= 250 < n_plain, (n_once, n_plain)
