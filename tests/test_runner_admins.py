"""Administering a runner vs speaking FOR one.

`owner` was doing both jobs, which made the human who happened to run the
pairing command the only person who could ever fix the box. On 2026-09-08 that
bit for real: a signed-out cloud runner could not be re-authenticated by the
identity it actually runs as.

Neither existing tier could take over. Workspace OWNER is too narrow — the
workspace in question has exactly one, which IS the single point of failure.
Workspace MEMBER is far too wide: it auto-joins a whole email domain, so it would
hand the fleet's Claude credentials to everyone with that address. So the grant is
explicit and per-runner, and these tests are the boundary.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.harness.models import Runner, RunnerAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def runner_owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def agent_identity():
    """The identity the box actually runs as — a member, never the owner."""
    return User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")


@pytest.fixture()
def bystander():
    """Auto-joined by domain. Exactly who must NOT inherit credentials."""
    return User.objects.create_user("someone", "someone@dimagi.com", "pw")


@pytest.fixture()
def workspace(runner_owner, agent_identity, bystander):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=runner_owner)
    WorkspaceMembership.objects.create(user=runner_owner, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (agent_identity, bystander):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return ws


@pytest.fixture()
def runner(runner_owner, workspace):
    return Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD,
                                 owner=runner_owner, workspace=workspace)


def client_for(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(c, url, body=None):
    return c.post(url, data=json.dumps(body or {}), content_type="application/json")


def base(runner) -> str:
    return f"/api/harness/runners/{runner.id}"


# ── the defect ─────────────────────────────────────────────────────────────

def test_a_member_cannot_administer_by_default(runner, agent_identity):
    """The starting state, and correct: membership alone is not administration."""
    c = client_for(agent_identity)
    assert _post(c, f"{base(runner)}/mint").status_code == 404
    assert c.get(f"{base(runner)}/credential/status").status_code == 404


def test_a_grant_lets_the_agent_identity_sign_its_own_box_in(runner, runner_owner, agent_identity):
    """The fix. The box runs as this identity; after an explicit grant it can
    re-authenticate it without borrowing the owner's login."""
    assert _post(client_for(runner_owner), f"{base(runner)}/admins",
                 {"email": "ace@dimagi-ai.com"}).status_code == 200

    c = client_for(agent_identity)
    assert c.get(f"{base(runner)}/credential/status").status_code == 200
    r = _post(c, f"{base(runner)}/mint")
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "requested"


def test_the_grant_is_per_runner_not_a_blanket(runner, runner_owner, agent_identity, workspace):
    """A grant on one box must not carry to the next one that shows up."""
    other = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD,
                                  owner=runner_owner, workspace=workspace)
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(agent_identity)
    assert _post(c, f"{base(runner)}/mint").status_code == 200
    assert _post(c, f"{base(other)}/mint").status_code == 404


def test_an_ungranted_member_still_gets_nothing(runner, runner_owner, agent_identity, bystander):
    """The reason this is a grant and not a tier: everyone on the domain is a
    member, and credentials must not follow membership."""
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(bystander)
    assert _post(c, f"{base(runner)}/mint").status_code == 404
    assert c.get(f"{base(runner)}/credential/status").status_code == 404


# ── what a grant does NOT buy ──────────────────────────────────────────────

def test_an_administrator_cannot_speak_as_the_runner(runner, runner_owner, agent_identity):
    """Administration is not impersonation: an administrator may not read the
    box's actual secret values, which only the runner fetches. (Starting and
    reading drills IS administration since 2026-10-04 — see the drill tests
    below.)"""
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(agent_identity)
    assert c.get(f"{base(runner)}/credential").status_code == 404


# ── readiness drills: a runner-ADMIN feature (owner decision 2026-10-04) ────

@pytest.fixture()
def drilled_agent(runner, workspace):
    from apps.agents.models import Agent
    from apps.harness.models import RunnerAssignment

    agent = Agent.objects.create(slug="eva", name="Eva", workspace=workspace)
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return agent


def _make_agent_admin(agent, user):
    from apps.agents.models import AgentAdmin

    AgentAdmin.objects.create(agent=agent, user=user)


def _drill(c, runner, body=None):
    return _post(c, f"{base(runner)}/drill", body)


def test_a_runner_admin_who_admins_the_agent_can_start_and_list_drills(
    runner, runner_owner, agent_identity, drilled_agent
):
    from apps.harness.models import Turn

    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    _make_agent_admin(drilled_agent, agent_identity)
    c = client_for(agent_identity)
    r = _drill(c, runner)
    assert r.status_code == 200, r.content
    assert [d["agent_slug"] for d in r.json()] == ["eva"]
    listed = c.get(f"{base(runner)}/drills")
    assert listed.status_code == 200 and len(listed.json()) == 1
    # The turn is still canopy's (system), pinned to the box, and accountable
    # to the admin who started it — so they can read what they started.
    turn = Turn.objects.get()
    assert turn.pinned_runner_id == runner.id
    assert turn.initiator_user_id == agent_identity.id


def test_a_runner_admin_drills_only_the_agents_they_administer(
    runner, runner_owner, agent_identity, drilled_agent
):
    """Agent side: a runner admin who is only an EDITOR of the agent may not
    drill it — a drill runs as `system` in the agent's routing mode, which would
    lift the editor tier's always-`manual` rule."""
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(agent_identity)
    # Named explicitly: refused, saying which.
    r = _drill(c, runner, {"agents": ["eva"]})
    assert r.status_code == 403 and b"eva" in r.content
    # Defaulted: nothing they may drill → 422, and nothing was queued.
    assert _drill(c, runner).status_code == 422
    from apps.harness.models import Turn

    assert not Turn.objects.exists()


def test_a_plain_member_cannot_start_or_list_drills(runner, bystander, drilled_agent):
    """No grant → the same no-leak 404 as every runner-admin route, even for an
    agent admin of the drilled agent."""
    _make_agent_admin(drilled_agent, bystander)
    c = client_for(bystander)
    assert _drill(c, runner).status_code == 404
    assert c.get(f"{base(runner)}/drills").status_code == 404


def test_a_viewer_cannot_start_or_list_drills(runner, workspace, drilled_agent):
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=workspace,
                                       role=WorkspaceMembership.VIEWER)
    c = client_for(viewer)
    assert _drill(c, runner).status_code == 404
    assert c.get(f"{base(runner)}/drills").status_code == 404


def test_the_owner_still_drills_every_assigned_agent(runner, runner_owner, drilled_agent):
    r = _drill(client_for(runner_owner), runner)
    assert r.status_code == 200 and len(r.json()) == 1


def test_an_administrator_cannot_mint_more_administrators(runner, runner_owner, agent_identity):
    """Otherwise the grant is self-propagating and the list stops meaning
    "people the owner trusted"."""
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    other = User.objects.create_user("third", "third@dimagi.com", "pw")  # noqa: F841
    r = _post(client_for(agent_identity), f"{base(runner)}/admins",
              {"email": "third@dimagi.com"})
    assert r.status_code == 404


# ── grant hygiene ──────────────────────────────────────────────────────────

def test_granting_twice_is_idempotent(runner, runner_owner, agent_identity):
    c = client_for(runner_owner)
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    assert RunnerAdmin.objects.filter(runner=runner, user=agent_identity).count() == 1


def test_a_non_member_cannot_be_granted(runner, runner_owner):
    """Caught at grant time as a clear 422, rather than as a mystifying 404 the
    first time the grantee tries to use it."""
    User.objects.create_user("outsider", "outsider@example.com", "pw")
    r = _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "outsider@example.com"})
    assert r.status_code == 422


def test_revoking_takes_the_access_away(runner, runner_owner, agent_identity):
    c = client_for(runner_owner)
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    assert _post(client_for(agent_identity), f"{base(runner)}/mint").status_code == 200
    assert c.delete(f"{base(runner)}/admins/{agent_identity.id}").status_code == 204
    assert _post(client_for(agent_identity), f"{base(runner)}/mint").status_code == 404


def test_the_runner_owner_never_loses_administration(runner, runner_owner):
    """The owner owns the credential the box authenticates with, so they cannot
    be revoked — a box with no administrator is not a reachable state."""
    c = client_for(runner_owner)
    assert c.delete(f"{base(runner)}/admins/{runner_owner.id}").status_code == 404
    assert _post(c, f"{base(runner)}/mint").status_code == 200


def test_the_flags_are_reported_separately(runner, runner_owner, agent_identity):
    """can_manage and can_administer gate DIFFERENT routes. Reporting one for the
    other is how a UI renders a control that 404s."""
    _post(client_for(runner_owner), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    row = [r for r in client_for(agent_identity).get("/api/harness/runners/").json()
           if r["name"] == "cloud-ec2-1"][0]
    assert row["can_administer"] is True
    assert row["can_manage"] is False

    own = [r for r in client_for(runner_owner).get("/api/harness/runners/").json()
           if r["name"] == "cloud-ec2-1"][0]
    assert own["can_manage"] is True and own["can_administer"] is True
