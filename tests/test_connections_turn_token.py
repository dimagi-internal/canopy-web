"""A runner executing an agent's turn gets that AGENT's Salesforce token.

Pins what only this route decides: the turn lookup (claimed by this runner and
executing, else 404), the agent's own or granted connection and never the
initiator's, caller-confined turns refused, the 409 bodies a runner branches on,
and that the audit event never carries a token.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.connections.models import Connection, ConnectionGrant
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import FakeSF, _resp, _stored, sf_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return get_user_model().objects.create_user(username="op", email="op@dimagi.com")


@pytest.fixture
def ws(operator):
    w = Workspace.objects.create(slug="w", display_name="W", created_by=operator)
    WorkspaceMembership.objects.create(workspace=w, user=operator, role=WorkspaceMembership.EDITOR)
    return w


def _agent(ws, operator, slug):
    a = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=operator)
    AgentAdmin.objects.get_or_create(agent=a, user=operator)
    return a


@pytest.fixture
def eva(ws, operator):
    return _agent(ws, operator, "eva")


@pytest.fixture
def echo(ws, operator):
    return _agent(ws, operator, "echo")


@pytest.fixture
def runner(operator, echo):
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=operator, workspace=echo.workspace,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=echo, runner=r, rank=0)
    return r


def _bearer(runner) -> str:
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return raw


def _turn(agent, runner=None, status=Turn.CLAIMED, **kw):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=status,
                               claimed_by=runner, **kw)


def _ask(runner, turn, provider="salesforce", **body):
    return Client().post(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/connection-token",
                         {"provider": provider, **body}, content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {_bearer(runner)}")


def test_issues_agent_token_for_claimed_turn(echo, runner, operator):
    _stored({"agent": echo}, operator)
    _stored({"user": operator}, operator, access="00D!persons")
    r = _ask(runner, _turn(echo, runner))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["access_token"] == "00D!cached"
    assert body["instance_url"] == "https://dimagi.my.salesforce.com"
    assert body["account_label"] == "jj@dimagi.com"
    ev = Event.objects.get(kind="connections.token_issued")
    assert ev.payload["agent"] == "echo" and ev.payload["runner"] == str(runner.pk)
    assert ev.payload["provider"] == "salesforce" and ev.payload["account_label"] == "jj@dimagi.com"
    assert "00D!cached" not in str(ev.payload) and "00D!cached" not in ev.summary


def test_granted_connection_used_by_borrowing_agent(eva, echo, runner, operator):
    conn = _stored({"agent": eva}, operator, access="00D!evas")
    turn = _turn(echo, runner)
    assert _ask(runner, turn).status_code == 409  # not before
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=operator)
    r = _ask(runner, turn)
    assert r.status_code == 200 and r.json()["access_token"] == "00D!evas"


def test_never_initiators_connection(echo, runner, operator):
    _stored({"user": operator}, operator)
    turn = _turn(echo, runner, initiator_user=operator)
    r = _ask(runner, turn)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"


@pytest.mark.parametrize("case", ["queued", "finished", "other_runner"])
def test_unclaimed_or_finished_turn_404(echo, runner, operator, case):
    _stored({"agent": echo}, operator)
    other = Runner.objects.create(name="o", kind=Runner.CLOUD, owner=operator, workspace=echo.workspace,
                                  last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = {"queued": lambda: _turn(echo, None, Turn.QUEUED),
            "finished": lambda: _turn(echo, runner, Turn.DONE),
            "other_runner": lambda: _turn(echo, other)}[case]()
    assert _ask(runner, turn).status_code == 404


def test_confined_turn_404(echo, runner, operator):
    _stored({"agent": echo}, operator)
    turn = _turn(echo, runner, capability="ask")
    assert _ask(runner, turn).status_code == 404
    assert not Event.objects.filter(kind="connections.token_issued").exists()


def test_reconnect_required_409(echo, runner, operator):
    _stored({"agent": echo}, operator, access="")
    with FakeSF(token=[_resp(400, {"error": "invalid_grant"})]):
        r = _ask(runner, _turn(echo, runner))
    assert r.status_code == 409 and r.json()["error"] == "reconnect_required"


def test_expired_cache_refreshes(echo, runner, operator):
    _stored({"agent": echo}, operator, expires_in=timedelta(seconds=-1))
    from tests.fixtures.connections import _token_body
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!fresh"))]):
        r = _ask(runner, _turn(echo, runner))
    assert r.status_code == 200 and r.json()["access_token"] == "00D!fresh"


def test_min_life_seconds_is_honoured(echo, runner, operator):
    """10 minutes left: enough by default, not for a runner asking for 900 s."""
    _stored({"agent": echo}, operator, expires_in=timedelta(minutes=10))
    from tests.fixtures.connections import _token_body
    turn = _turn(echo, runner)
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!fresh"))]) as fake:
        assert _ask(runner, turn).json()["access_token"] == "00D!cached"
        r = _ask(runner, turn, min_life_seconds=900)
    assert r.status_code == 200 and r.json()["access_token"] == "00D!fresh"
    assert len(fake.token_calls()) == 1


@pytest.mark.parametrize("bad", [-1, 3601])
def test_min_life_seconds_is_bounded(echo, runner, operator, bad):
    _stored({"agent": echo}, operator)
    assert _ask(runner, _turn(echo, runner), min_life_seconds=bad).status_code == 422


def test_refresh_forces_a_new_token(echo, runner, operator):
    _stored({"agent": echo}, operator)
    from tests.fixtures.connections import _token_body
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!forced"))]):
        r = _ask(runner, _turn(echo, runner), refresh=True)
    assert r.status_code == 200 and r.json()["access_token"] == "00D!forced"


def test_unknown_provider_404(echo, runner):
    assert _ask(runner, _turn(echo, runner), provider="nope").status_code == 404


def test_turn_with_no_agent_409(runner):
    t = Turn.objects.create(project="some/repo", origin=Turn.ORIGIN_EMAIL, prompt="x",
                            idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=runner)
    r = _ask(runner, t)
    assert r.status_code == 409 and r.json()["error"] == "no_agent"


def test_runner_that_no_longer_may_hold_the_agent_404(echo, runner, operator):
    """The claim required it, but an owner can be demoted mid-turn: asked again."""
    _stored({"agent": echo}, operator)
    turn = _turn(echo, runner)
    assert _ask(runner, turn).status_code == 200
    other = get_user_model().objects.create_user(username="new", email="new@dimagi.com")
    Agent.objects.filter(pk=echo.pk).update(owner=other)
    AgentAdmin.objects.filter(agent=echo, user=operator).delete()
    r = _ask(runner, turn)
    assert r.status_code == 404
    assert Event.objects.filter(kind="connections.token_issued").count() == 1  # only the first


def test_ambiguous_409(echo, runner, operator):
    first = _stored({"agent": echo}, operator)
    Connection.objects.create(
        provider="salesforce", agent=echo, account_id="00Dorg/005other", account_label="other@dimagi.com",
        instance_url=first.instance_url, refresh_token_enc=first.refresh_token_enc, connected_by=operator,
        app=first.app)
    r = _ask(runner, _turn(echo, runner))
    assert r.status_code == 409 and r.json()["error"] == "ambiguous"
    assert not Event.objects.filter(kind="connections.token_issued").exists()


def test_not_configured_409(echo, runner, operator):
    conn = _stored({"agent": echo}, operator, access="")
    conn.app.client_id = ""
    conn.app.client_secret = ""
    conn.app.save()
    r = _ask(runner, _turn(echo, runner))
    assert r.status_code == 409 and r.json()["error"] == "not_configured"


@pytest.mark.parametrize("answer,status", [
    (_resp(400, {"error": "invalid_client"}), 502),   # canopy's own configuration, not the grant
    (_resp(503, text="down"), 503),                     # the provider is unreachable
])
def test_provider_failures_map_to_502_and_503(echo, runner, operator, answer, status):
    conn = _stored({"agent": echo}, operator, access="")
    with FakeSF(token=[answer]):
        r = _ask(runner, _turn(echo, runner))
    assert r.status_code == status, r.content
    conn.refresh_from_db()
    assert conn.refresh_failed_at is None  # neither says anything about the grant
    assert not Event.objects.filter(kind="connections.token_issued").exists()


# ---- which providers a turn's agent can use (the runner stages only these) -------------

def _list(runner, turn):
    return Client().get(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/connections",
                        HTTP_AUTHORIZATION=f"Bearer {_bearer(runner)}")


def test_turn_connections_lists_own_and_granted_never_the_initiators(eva, echo, runner, operator):
    turn = _turn(echo, runner, initiator_user=operator)
    _stored({"user": operator}, operator)
    assert _list(runner, turn).json() == {"providers": []}
    conn = _stored({"agent": eva}, operator)
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=operator)
    assert _list(runner, turn).json() == {"providers": ["salesforce"]}
    assert not Event.objects.exists()                     # listing hands out nothing


def test_turn_connections_skips_a_provider_no_longer_registered(echo, runner, operator):
    Connection.objects.create(provider="retired", account_id="x", agent=echo, connected_by=operator,
                              app=sf_app(echo.workspace, provider="retired"))
    assert _list(runner, _turn(echo, runner)).json() == {"providers": []}


def test_turn_connections_refuses_what_the_token_route_refuses(echo, runner, operator):
    _stored({"agent": echo}, operator)
    assert _list(runner, _turn(echo, runner, capability="ask")).status_code == 404
    assert _list(runner, _turn(echo, runner, Turn.DONE)).status_code == 404
    t = Turn.objects.create(project="some/repo", origin=Turn.ORIGIN_EMAIL, prompt="x",
                            idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=runner)
    r = _list(runner, t)
    assert r.status_code == 409 and r.json()["error"] == "no_agent"


def test_a_granted_connections_token_issue_lands_in_the_owners_workspace(echo, runner, operator):
    """The owner's admins see who acted through their connection: the Event is in the
    OWNER's workspace, naming the grantee as `agent`."""
    other_ws = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=operator)
    WorkspaceMembership.objects.create(workspace=other_ws, user=operator, role=WorkspaceMembership.EDITOR)
    hal = _agent(other_ws, operator, "hal")
    conn = _stored({"agent": hal}, operator)
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=operator)
    turn = _turn(echo, runner)
    assert _ask(runner, turn).status_code == 200
    ev = Event.objects.get(kind="connections.token_issued")
    assert ev.workspace_id == other_ws.pk != echo.workspace_id
    assert ev.payload["owner"] == "agent:hal" and ev.payload["agent"] == "echo"
    assert ev.payload["to"] == "turn" and ev.payload["turn"] == str(turn.pk)
    assert ev.summary == f"agent:hal's Salesforce token for echo's turn on {runner.name}"
