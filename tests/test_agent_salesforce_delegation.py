"""Agents act in Salesforce with DELEGATED access to one agent's own credential (#1291).

chrome-sales is a delegated identity: since 2026-10-07 the fleet acts in Salesforce
as Eva's SF user, and agents get no Salesforce accounts of their own. What these pin:

- lending needs the owner of BOTH agents;
- every agent's vault is zero-knowledge, so canopy-web cannot read a lender's stored
  credential to lend it: lending is refused, never recorded, and a loan recorded
  before hands a box nothing;
- the runner's resolve route carries "" when there is nothing to lend.

Salesforce is never called: `requests` is replaced by a fake.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import delegations
from apps.agents.models import Agent, AgentCredential, AgentDelegation
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

CREDS = {"clientId": "PlatformCLI", "clientSecret": "", "myDomain": "example.my.salesforce.com",
         "instanceUrl": "https://example.my.salesforce.com", "accessToken": "a",
         "refreshToken": "r1", "isSandbox": False}


class FakeSalesforce:
    def __init__(self, *, username="eva@example.com", accept=True):
        self.username, self.accept = username, accept
        self.refreshed_with: list[str] = []

    def _resp(self, status, body):
        r = mock.Mock(status_code=status, content=b"x")
        r.json.return_value = body
        return r

    def post(self, url, data=None, **_):
        assert url == "https://example.my.salesforce.com/services/oauth2/token"
        self.refreshed_with.append(data["refresh_token"])
        if not self.accept:
            return self._resp(400, {"error": "invalid_grant", "error_description": "expired access/refresh token"})
        return self._resp(200, {"access_token": "at", "instance_url": "https://example.my.salesforce.com"})

    def get(self, url, headers=None, **_):
        assert url.endswith("/services/oauth2/userinfo") and headers["Authorization"] == "Bearer at"
        return self._resp(200, {"preferred_username": self.username, "user_id": "005X", "organization_id": "00DX"})


@pytest.fixture
def sf():
    fake = FakeSalesforce()
    with mock.patch.object(delegations.requests, "post", fake.post), \
            mock.patch.object(delegations.requests, "get", fake.get):
        yield fake


@pytest.fixture
def owner():
    return User.objects.create_user("olive", "olive@dimagi.com", "pw")


@pytest.fixture
def ws(owner):
    w = a_workspace()
    wsvc.ensure_member(w, owner, WorkspaceMembership.OWNER)
    return w


@pytest.fixture
def eva(ws, owner):
    a = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)
    AgentCredential.objects.create(agent=a, name="salesforce", value_zk="zk1:ciphertext", kv=1)
    return a


@pytest.fixture
def hal(ws, owner):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ---- lending --------------------------------------------------------------------

def test_a_zero_knowledge_lender_is_refused_not_stored(hal, eva, owner, sf):
    with pytest.raises(delegations.DelegationError, match="zero-knowledge"):
        delegations.set_salesforce(hal, owner, "eva")
    assert not AgentDelegation.objects.filter(agent=hal).exists()
    assert sf.refreshed_with == []  # Salesforce is never asked


def test_a_lender_with_no_credential_is_refused(hal, eva, owner, sf):
    AgentCredential.objects.filter(agent=eva).delete()
    with pytest.raises(delegations.DelegationError, match="holds no Salesforce credential"):
        delegations.set_salesforce(hal, owner, "eva")


def test_only_the_owner_of_both_can_lend(hal, eva, owner, ws, sf):
    other = User.objects.create_user("x", "x@dimagi.com", "pw")
    wsvc.ensure_member(ws, other, WorkspaceMembership.EDITOR)
    with pytest.raises(delegations.DelegationError, match="hal's owner"):
        delegations.set_salesforce(hal, other, "eva")
    eva.owner = other
    eva.save()
    with pytest.raises(delegations.DelegationError, match="you own no agent 'eva'"):
        delegations.set_salesforce(hal, owner, "eva")


def test_the_route_refuses_with_the_reason(hal, eva, owner, sf):
    r = _client(owner).put("/api/agents/hal/salesforce", data='{"lender": "eva"}',
                           content_type="application/json")
    assert r.status_code == 422 and "zero-knowledge" in r.content.decode()


def test_a_loan_recorded_before_hands_out_nothing(hal, eva, owner):
    AgentDelegation.objects.create(user=owner, agent=hal, service=AgentDelegation.SALESFORCE,
                                   secret_enc="", lender=eva, meta={})
    assert delegations.salesforce_creds_for(hal) == ""
    assert delegations.salesforce_status(eva)["lent_to"] == ["hal"]


# ---- handing it to a box ----------------------------------------------------------

@pytest.fixture
def runner(owner, hal):
    r = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=owner, workspace=hal.workspace,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=hal, runner=r, rank=0)
    return r


def _resolve(runner, slug):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=runner.owner, label="runner")
    return Client().get(f"/api/agents/{slug}/credentials/resolve", HTTP_AUTHORIZATION=f"Bearer {raw}")


def test_resolve_carries_nothing_when_none_is_lent(hal, runner):
    assert _resolve(runner, "hal").json()["salesforce_creds"] == ""
