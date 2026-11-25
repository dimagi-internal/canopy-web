"""A box runs its vault keeper when its keeper version moves, not on a clock.

* Someone newly waiting for a key a box holds moves that box's keeper version (and its
  sync version, so `/sync` answers again), once: `zk_notify.sweep` rings as it records
  the target's notice, and a target already noticed rings nothing.
* `/sync` carries `keeper_version`, and a ring changes its ETag.
* A delegated credential, and a ring with no vault named, reach the boxes that keep.
* `Runner.save` never writes the keeper version back from a stale instance.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.test import Client

from apps.agents.credentials import delegations, keeper_doorbell, zk_notify
from apps.agents.models import Agent, Vault
from apps.harness.models import Runner
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.keeper_fleet import keeper_fleet as _fleet

pytestmark = pytest.mark.django_db


def _kv(runner) -> tuple[int, int]:
    r = Runner.objects.get(pk=runner.pk)
    return r.keeper_version, r.sync_version


def test_a_new_waiting_machine_rings_its_keeper_once():
    runner = _fleet(1, shared=False)
    before = _kv(runner)
    zk_notify.sweep()
    assert _kv(runner) == (before[0] + 1, before[1] + 1)
    zk_notify.sweep()
    assert _kv(runner) == (before[0] + 1, before[1] + 1)


def test_a_box_that_keeps_nothing_is_not_rung():
    runner = _fleet(1, shared=False)
    bystander = Runner.objects.create(name="bystander", kind=Runner.CLOUD, status=Runner.ONLINE)
    zk_notify.sweep()
    keeper_doorbell.ring()
    assert _kv(bystander) == (0, 0)
    assert _kv(runner)[0] >= 2


def test_sync_answers_the_keeper_version_and_a_ring_changes_its_etag():
    runner = _fleet(1, shared=False)
    c = Client()
    c.force_login(runner.owner)
    first = c.get(f"/api/harness/runners/{runner.pk}/sync")
    assert first.status_code == 200 and first.json()["keeper_version"] == _kv(runner)[0]
    assert c.get(f"/api/harness/runners/{runner.pk}/sync", HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
    keeper_doorbell.ring(vault=Vault.objects.get(kind=Vault.AGENT, agent=Agent.objects.get(slug="a0")))
    again = c.get(f"/api/harness/runners/{runner.pk}/sync", HTTP_IF_NONE_MATCH=first["ETag"])
    assert again.status_code == 200 and again.json()["keeper_version"] == first.json()["keeper_version"] + 1


def test_delegating_a_credential_rings_the_machines_that_run_the_agent():
    runner = _fleet(1, shared=False)
    agent = Agent.objects.get(slug="a0")
    before = _kv(runner)[0]
    fake = FakeGitHub(pr={"dimagi-internal/a0": 422})
    with mock.patch.object(delegations.requests, "get", fake.get), \
            mock.patch.object(delegations.requests, "post", fake.post):
        delegations.set_github(agent, agent.owner, "github_pat_y")
    assert _kv(runner)[0] == before + 1


def test_a_stale_runner_row_never_writes_the_keeper_version_back():
    runner = _fleet(1, shared=False)
    stale = Runner.objects.get(pk=runner.pk)
    keeper_doorbell.ring()
    moved = _kv(runner)
    stale.host = "elsewhere"
    stale.save()
    stale.save(update_fields=["host", "keeper_version", "sync_version"])
    assert _kv(runner) == moved
