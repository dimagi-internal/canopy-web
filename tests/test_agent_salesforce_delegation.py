"""Agents act in Salesforce with DELEGATED access to one agent's own credential (#1291).

chrome-sales is a delegated identity: since 2026-10-07 the fleet acts in Salesforce
as Eva's SF user, and agents get no Salesforce accounts of their own. What these pin:

- lending needs the owner of BOTH agents, and a credential Salesforce rejects is
  refused, never recorded;
- the row references the lender's credential — a re-mint reaches every borrower,
  and nothing is copied into the borrower;
- the delegation in force is the current owners': transfer either agent and it stops;
- the runner's resolve route carries the borrowed credential, and "" when there is none;
- the lender sees who borrows it.

Salesforce is never called: `requests` is replaced by a fake.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents import delegations
from apps.agents.models import Agent, AgentCredential, AgentDelegation
from apps.common.encryption import encrypt_secret
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
    AgentCredential.objects.create(agent=a, name="salesforce", value_enc=encrypt_secret(json.dumps(CREDS)))
    return a


@pytest.fixture
def hal(ws, owner):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ---- lending --------------------------------------------------------------------

def test_owner_lends_and_the_row_references_rather_than_copies(hal, eva, owner, sf):
    row = delegations.set_salesforce(hal, owner, "eva")
    assert row.lender == eva and row.secret_enc == ""           # nothing copied
    assert row.meta["username"] == "eva@example.com"
    assert json.loads(delegations.salesforce_creds_for(hal))["refreshToken"] == "r1"


def test_a_remint_by_the_lender_reaches_the_borrower(hal, eva, owner, sf):
    delegations.set_salesforce(hal, owner, "eva")
    AgentCredential.objects.filter(agent=eva, name="salesforce").update(
        value_enc=encrypt_secret(json.dumps({**CREDS, "refreshToken": "r2"})))
    assert json.loads(delegations.salesforce_creds_for(hal))["refreshToken"] == "r2"


def test_a_credential_salesforce_rejects_is_refused_not_stored(hal, eva, owner, sf):
    sf.accept = False
    with pytest.raises(delegations.DelegationError, match="expired access/refresh token"):
        delegations.set_salesforce(hal, owner, "eva")
    assert not AgentDelegation.objects.filter(agent=hal).exists()


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


def test_the_lender_may_live_in_another_workspace(hal, owner, sf):
    # Eva is in one tenant, the agents borrowing her identity in another.
    other_ws = a_workspace()
    wsvc.ensure_member(other_ws, owner, WorkspaceMembership.OWNER)
    lender = Agent.objects.create(slug="eva2", name="Eva", workspace=other_ws, owner=owner)
    AgentCredential.objects.create(agent=lender, name="salesforce", value_enc=encrypt_secret(json.dumps(CREDS)))
    delegations.set_salesforce(hal, owner, "eva2")
    assert json.loads(delegations.salesforce_creds_for(hal))["refreshToken"] == "r1"


def test_transferring_the_lender_stops_every_loan(hal, eva, owner, ws, sf):
    delegations.set_salesforce(hal, owner, "eva")
    other = User.objects.create_user("x", "x@dimagi.com", "pw")
    wsvc.ensure_member(ws, other, WorkspaceMembership.EDITOR)
    eva.owner = other
    eva.save()
    assert delegations.salesforce_creds_for(hal) == ""
    assert delegations.salesforce_status(hal)["set"] is False


def test_status_answers_both_directions(hal, eva, owner, sf):
    delegations.set_salesforce(hal, owner, "eva")
    assert delegations.salesforce_status(eva)["lent_to"] == ["hal"]
    st = delegations.salesforce_status(hal)
    assert st["lender"] == "eva" and st["username"] == "eva@example.com"


def test_routes_never_echo_the_credential(hal, eva, owner, sf):
    c = _client(owner)
    r = c.put("/api/agents/hal/salesforce", data=json.dumps({"lender": "eva"}),
              content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["lender"] == "eva" and "r1" not in r.content.decode()
    assert c.get("/api/agents/eva/salesforce").json()["lent_to"] == ["hal"]
    assert c.delete("/api/agents/hal/salesforce").json()["set"] is False


def test_the_route_refuses_with_salesforces_reason(hal, eva, owner, sf):
    sf.accept = False
    r = _client(owner).put("/api/agents/hal/salesforce", data=json.dumps({"lender": "eva"}),
                           content_type="application/json")
    assert r.status_code == 422 and "expired access/refresh token" in r.content.decode()


# ---- handing it to a box ----------------------------------------------------------

@pytest.fixture
def runner(owner, hal):
    r = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=owner,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=hal, runner=r, rank=0)
    return r


def _resolve(runner, slug):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=runner.owner, label="runner")
    return Client().get(f"/api/agents/{slug}/credentials/resolve", HTTP_AUTHORIZATION=f"Bearer {raw}")


def test_resolve_carries_the_borrowed_credential(hal, eva, owner, sf, runner):
    delegations.set_salesforce(hal, owner, "eva")
    r = _resolve(runner, "hal")
    assert r.status_code == 200, r.content
    assert json.loads(r.json()["salesforce_creds"])["refreshToken"] == "r1"


def test_resolve_carries_nothing_when_none_is_lent(hal, runner):
    assert _resolve(runner, "hal").json()["salesforce_creds"] == ""
