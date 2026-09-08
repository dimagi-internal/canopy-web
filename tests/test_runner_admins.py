"""Administering a runner vs speaking FOR one.

`paired_by` was doing both jobs, which made the human who happened to run the
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
def pairer():
    return User.objects.create_user("pairer", "pairer@dimagi.com", "pw")


@pytest.fixture()
def agent_identity():
    """The identity the box actually runs as — a member, never the pairer."""
    return User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")


@pytest.fixture()
def bystander():
    """Auto-joined by domain. Exactly who must NOT inherit credentials."""
    return User.objects.create_user("someone", "someone@dimagi.com", "pw")


@pytest.fixture()
def workspace(pairer, agent_identity, bystander):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=pairer)
    WorkspaceMembership.objects.create(user=pairer, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (agent_identity, bystander):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return ws


@pytest.fixture()
def runner(pairer, workspace):
    return Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD,
                                 paired_by=pairer, workspace=workspace)


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


def test_a_grant_lets_the_agent_identity_sign_its_own_box_in(runner, pairer, agent_identity):
    """The fix. The box runs as this identity; after an explicit grant it can
    re-authenticate it without borrowing the pairer's login."""
    assert _post(client_for(pairer), f"{base(runner)}/admins",
                 {"email": "ace@dimagi-ai.com"}).status_code == 200

    c = client_for(agent_identity)
    assert c.get(f"{base(runner)}/credential/status").status_code == 200
    r = _post(c, f"{base(runner)}/mint")
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "requested"


def test_the_grant_is_per_runner_not_a_blanket(runner, pairer, agent_identity, workspace):
    """A grant on one box must not carry to the next one that shows up."""
    other = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD,
                                  paired_by=pairer, workspace=workspace)
    _post(client_for(pairer), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(agent_identity)
    assert _post(c, f"{base(runner)}/mint").status_code == 200
    assert _post(c, f"{base(other)}/mint").status_code == 404


def test_an_ungranted_member_still_gets_nothing(runner, pairer, agent_identity, bystander):
    """The reason this is a grant and not a tier: everyone on the domain is a
    member, and credentials must not follow membership."""
    _post(client_for(pairer), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(bystander)
    assert _post(c, f"{base(runner)}/mint").status_code == 404
    assert c.get(f"{base(runner)}/credential/status").status_code == 404


# ── what a grant does NOT buy ──────────────────────────────────────────────

def test_an_administrator_cannot_speak_as_the_runner(runner, pairer, agent_identity):
    """Administration is not impersonation. Drilling POSTs AS the box and derives
    a tenant from `paired_by`, so it stays with the pairer."""
    _post(client_for(pairer), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    c = client_for(agent_identity)
    assert c.get(f"{base(runner)}/drills").status_code == 404
    # …nor read the box's actual secret values, which only the runner fetches.
    assert c.get(f"{base(runner)}/credential").status_code == 404


def test_an_administrator_cannot_mint_more_administrators(runner, pairer, agent_identity):
    """Otherwise the grant is self-propagating and the list stops meaning
    "people the owner trusted"."""
    _post(client_for(pairer), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    other = User.objects.create_user("third", "third@dimagi.com", "pw")  # noqa: F841
    r = _post(client_for(agent_identity), f"{base(runner)}/admins",
              {"email": "third@dimagi.com"})
    assert r.status_code == 404


# ── grant hygiene ──────────────────────────────────────────────────────────

def test_granting_twice_is_idempotent(runner, pairer, agent_identity):
    c = client_for(pairer)
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    assert RunnerAdmin.objects.filter(runner=runner, user=agent_identity).count() == 1


def test_a_non_member_cannot_be_granted(runner, pairer):
    """Caught at grant time as a clear 422, rather than as a mystifying 404 the
    first time the grantee tries to use it."""
    User.objects.create_user("outsider", "outsider@example.com", "pw")
    r = _post(client_for(pairer), f"{base(runner)}/admins", {"email": "outsider@example.com"})
    assert r.status_code == 422


def test_revoking_takes_the_access_away(runner, pairer, agent_identity):
    c = client_for(pairer)
    _post(c, f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    assert _post(client_for(agent_identity), f"{base(runner)}/mint").status_code == 200
    assert c.delete(f"{base(runner)}/admins/{agent_identity.id}").status_code == 204
    assert _post(client_for(agent_identity), f"{base(runner)}/mint").status_code == 404


def test_the_pairer_never_loses_administration(runner, pairer):
    """The pairer owns the credential the box authenticates with, so they cannot
    be revoked — a box with no administrator is not a reachable state."""
    c = client_for(pairer)
    assert c.delete(f"{base(runner)}/admins/{pairer.id}").status_code == 404
    assert _post(c, f"{base(runner)}/mint").status_code == 200


def test_the_flags_are_reported_separately(runner, pairer, agent_identity):
    """can_manage and can_administer gate DIFFERENT routes. Reporting one for the
    other is how a UI renders a control that 404s."""
    _post(client_for(pairer), f"{base(runner)}/admins", {"email": "ace@dimagi-ai.com"})
    row = [r for r in client_for(agent_identity).get("/api/harness/runners/").json()
           if r["name"] == "cloud-ec2-1"][0]
    assert row["can_administer"] is True
    assert row["can_manage"] is False

    own = [r for r in client_for(pairer).get("/api/harness/runners/").json()
           if r["name"] == "cloud-ec2-1"][0]
    assert own["can_manage"] is True and own["can_administer"] is True
