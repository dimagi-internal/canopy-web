"""Every surface that knows the runner flags reads them from ONE list:
`canopy_sdk.contract.RUNNER_FLAGS`. A flag added there must be declarable (the
flags API), requirable (arrival parsing), and drawn (`RunnerOut.known_flags`)
with no second edit — and no surface may keep a copy that silently disagrees.
"""
from __future__ import annotations

import json

import pytest
from canopy_sdk import contract
from canopy_sdk.host import HostConfig, sign_visitor_assertion
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

EXTRA = "test-flag"


@pytest.fixture()
def extra_flag(monkeypatch):
    monkeypatch.setattr(contract, "RUNNER_FLAGS", frozenset(contract.RUNNER_FLAGS | {EXTRA}))
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def test_canopy_keeps_no_copy_of_the_list():
    assert rr.RUNNER_FLAGS is contract.RUNNER_FLAGS


def test_the_flags_api_and_known_flags_follow_the_contract(extra_flag, owner, ws):
    runner = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner,
                                   workspace=ws)
    c = Client()
    c.force_login(owner)
    r = c.put(f"/api/harness/runners/{runner.id}/flags", json.dumps({"flags": [EXTRA]}),
              content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["flags"] == [EXTRA]
    assert EXTRA in r.json()["known_flags"]
    [row] = c.get("/api/harness/runners/").json()
    assert EXTRA in row["known_flags"]


def test_arrival_accepts_a_flag_the_contract_lists(extra_flag, owner, ws):
    key = generate_private_key("EdDSA")
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    cfg = HostConfig(signing_key=key, canopy_base_url=assertions.audience(),
                     app_name="connect-labs")
    object.__setattr__(cfg, "runner_requirements", (EXTRA,))
    r = Client().post(contract.ARRIVAL_PATH,
                      data={"assertion": sign_visitor_assertion(cfg, "u-1", name="V"),
                            "agent_slug": "ace"},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    assert ContactToken.objects.get().runner_requirements == [EXTRA]
