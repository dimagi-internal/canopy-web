"""A turn-scoped agent login: issued only to the runner holding the turn, dead after it.

Spec: docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md. The token
replaces the agent's stored, never-expiring `canopy-pat` for runner turns.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin, AgentCredentialRead
from apps.harness import claim, services, turn_agent_tokens
from apps.harness.models import (
    Runner,
    RunnerAccessToken,
    RunnerAssignment,
    RunnerDeviceKey,
    Turn,
    TurnAgentToken,
)
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent():
    owner = User.objects.create_user("olive", "olive@dimagi.com", "pw")
    ws = a_workspace()
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    login = User.objects.create_user("echo-agent", "echo@dimagi-ai.com", "pw")
    return Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner, user=login)


@pytest.fixture
def operator(agent):
    user = User.objects.create_user("op", "op@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, user, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=agent, user=user)  # runner_may_hold_agent
    return user


def _runner(operator, agent, name="cloud-ec2-1"):
    r = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=operator,
                              workspace=agent.workspace, last_heartbeat_at=timezone.now(),
                              status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


@pytest.fixture
def runner(operator, agent):
    return _runner(operator, agent)


def _runner_token(runner) -> str:
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return raw


def _turn(agent, *, runner=None, status=Turn.CLAIMED, **kw):
    return Turn.objects.create(agent=agent, origin=kw.pop("origin", Turn.ORIGIN_EMAIL),
                               prompt="x", idempotency_key=uuid.uuid4().hex, status=status,
                               claimed_by=runner, **kw)


def _ask(runner, turn, bearer):
    return Client().post(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/agent-token",
                         HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _me(token):
    return Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {token}")


# ---- issuance ------------------------------------------------------------------------

def test_the_claiming_runner_gets_a_login_as_the_turns_agent(agent, runner):
    turn = _turn(agent, runner=runner)
    r = _ask(runner, turn, _runner_token(runner))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["agent_slug"] == "echo" and body["turn_id"] == str(turn.pk)
    # A login as the AGENT — not the box's owner — bounded to 12 hours.
    tok = PersonalToken.lookup(body["token"])
    assert tok.user == agent.user
    assert tok.expires_at <= timezone.now() + dt.timedelta(hours=12, seconds=5)
    assert TurnAgentToken.objects.get(token=tok).turn == turn
    # Logged beside the agent's credential reads, with the box and the turn.
    read = AgentCredentialRead.objects.get(agent=agent, name="turn-agent-token")
    assert (read.runner, read.via, read.turn_id) == (runner, "turn", str(turn.pk))


def test_asking_again_mints_a_fresh_one_and_both_work_until_the_turn_ends(agent, runner):
    turn = _turn(agent, runner=runner)
    bearer = _runner_token(runner)
    a, b = (_ask(runner, turn, bearer).json()["token"] for _ in range(2))
    assert a != b and PersonalToken.lookup(a) and PersonalToken.lookup(b)


def test_a_personal_token_is_refused_even_the_runner_owners(agent, runner, operator):
    turn = _turn(agent, runner=runner)
    pat, _ = PersonalToken.create_for_user(user=operator, label="op")
    r = _ask(runner, turn, pat)
    assert r.status_code == 403
    assert not TurnAgentToken.objects.exists()


def test_a_session_login_is_refused(agent, runner, operator):
    turn = _turn(agent, runner=runner)
    c = Client()
    c.force_login(operator)
    assert c.post(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/agent-token").status_code == 403


@pytest.mark.parametrize("case", ["queued", "other_runner", "finished", "other_runners_token"])
def test_only_the_runner_executing_the_turn_gets_one(agent, runner, operator, case):
    other = _runner(operator, agent, name="laptop")
    turn = {
        "queued": lambda: _turn(agent, runner=None, status=Turn.QUEUED),
        "other_runner": lambda: _turn(agent, runner=other),
        "finished": lambda: _turn(agent, runner=runner, status=Turn.DONE),
        "other_runners_token": lambda: _turn(agent, runner=runner),
    }[case]()
    bearer = _runner_token(other if case == "other_runners_token" else runner)
    r = _ask(runner, turn, bearer)
    # Another box's token cannot even reach this runner's routes (runner_auth).
    assert r.status_code == (403 if case == "other_runners_token" else 404)
    assert not TurnAgentToken.objects.exists()


def test_a_box_whose_owner_may_no_longer_hold_the_agent_gets_nothing(agent, runner, operator):
    turn = _turn(agent, runner=runner)
    AgentAdmin.objects.filter(agent=agent, user=operator).delete()
    assert _ask(runner, turn, _runner_token(runner)).status_code == 404


def test_a_callers_confined_turn_gets_no_agent_login(agent, runner):
    turn = _turn(agent, runner=runner, capability="ask")
    r = _ask(runner, turn, _runner_token(runner))
    assert r.status_code == 409
    assert "confined" in r.content.decode()
    assert not TurnAgentToken.objects.exists()


def test_a_local_turn_acts_as_its_person_never_as_the_agent(agent, runner):
    turn = _turn(agent, runner=runner, origin=Turn.ORIGIN_LOCAL)
    r = _ask(runner, turn, _runner_token(runner))
    assert r.status_code == 409
    assert "local turn" in r.content.decode()


def test_an_agent_without_a_login_is_refused_with_the_reason(agent, runner):
    Agent.objects.filter(pk=agent.pk).update(user=None)
    turn = _turn(agent, runner=runner)
    r = _ask(runner, turn, _runner_token(runner))
    assert r.status_code == 409
    assert "no canopy login" in r.content.decode()


# ---- revocation ----------------------------------------------------------------------

def _second_agent(agent, operator, runner):
    # One executing turn per agent: a second live turn needs a second agent.
    login = User.objects.create_user("hal-agent", "hal@dimagi-ai.com", "pw")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=agent.workspace,
                               owner=agent.owner, user=login)
    AgentAdmin.objects.create(agent=hal, user=operator)
    RunnerAssignment.objects.create(agent=hal, runner=runner, rank=0)
    return hal


def _issued(agent, runner):
    turn = _turn(agent, runner=runner, status=Turn.RUNNING)
    raw = _ask(runner, turn, _runner_token(runner)).json()["token"]
    assert _me(raw).status_code == 200
    return turn, raw


@pytest.mark.parametrize("status", [Turn.DONE, Turn.FAILED, Turn.CANCELLED])
def test_finishing_the_turn_revokes_its_logins(agent, runner, status):
    turn, raw = _issued(agent, runner)
    # A session-ful failure stays FAILED (a sessionless one is requeued — also revoked).
    Turn.objects.filter(pk=turn.pk).update(session_key="task-1")
    turn.refresh_from_db()
    services.finish_turn(turn, status=status)
    assert PersonalToken.lookup(raw) is None
    assert _me(raw).status_code == 401


def test_a_turn_requeued_after_a_sessionless_failure_revokes_them_too(agent, runner):
    turn, raw = _issued(agent, runner)
    services.finish_turn(turn, status=Turn.FAILED)
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED
    assert PersonalToken.lookup(raw) is None


def test_a_lost_turn_revokes_them(agent, runner):
    turn, raw = _issued(agent, runner)
    Turn.objects.filter(pk=turn.pk).update(lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    claim.sweep_expired_leases()
    turn.refresh_from_db()
    assert turn.status == Turn.LOST
    assert PersonalToken.lookup(raw) is None


def test_a_cancelled_turn_revokes_them(agent, runner):
    turn, raw = _issued(agent, runner)
    services.cancel_turn(turn)  # asks the runner; the runner finishes it cancelled
    assert PersonalToken.lookup(raw) is not None  # still its turn until then
    services.finish_turn(turn, status=Turn.CANCELLED, result_note="cancelled")
    assert PersonalToken.lookup(raw) is None


def test_the_sweep_catches_a_turn_moved_by_a_queryset_update(agent, runner, operator):
    turn, raw = _issued(agent, runner)
    _, other_raw = _issued(_second_agent(agent, operator, runner), runner)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.QUEUED, claimed_by=None)
    assert turn_agent_tokens.revoke_stale() == 1
    assert PersonalToken.lookup(raw) is None
    assert PersonalToken.lookup(other_raw) is not None  # still executing: untouched


def test_revoking_is_scoped_to_the_turn(agent, runner, operator):
    turn, raw = _issued(agent, runner)
    _, kept = _issued(_second_agent(agent, operator, runner), runner)
    turn_agent_tokens.revoke_for_turn(turn.pk)
    assert PersonalToken.lookup(raw) is None and PersonalToken.lookup(kept) is not None
