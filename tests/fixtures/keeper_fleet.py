"""A fleet of zero-knowledge agents for the vault keeper's tests.

`n` agents, each with its own owner, cloud runner, machine and a delegated GitHub
token, and a second machine of that runner waiting for a copy. `shared=True` also has
the first runner run every agent, so it holds every vault key and token."""
from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.credentials import delegations
from apps.agents.models import Agent, AgentAdmin, AgentVaultWrap
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.machines import a_machine


def keeper_fleet(n: int, *, shared: bool, p: str = "a") -> Runner:
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
