"""A new agent is born private, and gets its canopy login in one click.

Its `canopy-pat` login is stored in the Canopy vault from the browser, with no terminal
command (marshellis audit, 2026-10-18: a family workspace had no other way to give an
agent a login at all).

Pinned here: the create default, `POST /canopy-login` storing the token SEALED and never
returning it, its refusals, and the setup checklist step that points at it.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client

from apps.agents import services, setup
from apps.agents.models import Agent, AgentCredential
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db
URL = "/api/agents/scout/canopy-login"
ZK_DIR = Path(__file__).resolve().parents[1] / "docs" / "vault" / "zk-v1"


def _ref():
    spec = importlib.util.spec_from_file_location("zk_v1_reference", ZK_DIR / "zk_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _ref()


def _keyset(user):
    Passkey.objects.create(user=user, credential_id=ref.b64u(b"cred-1"), public_key=b"x", prf_enabled=True)
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    seal = ref.seal_user_key(scalar, str(user.pk), ref.b64u(b"cred-1"), os.urandom(32), os.urandom(16),
                             os.urandom(12))
    return UserVaultKeyset.objects.create(user=user, public_jwk=ref.jwk(priv.public_key()), seals=[seal])


@pytest.fixture
def ws():
    return a_workspace("family")


@pytest.fixture
def owner(ws):
    return a_member(ws, email="mom@example.com", role=WorkspaceMembership.ADMIN)


@pytest.fixture
def agent(ws, owner):
    class _In:  # the shape upsert_agent reads
        slug, name, description, persona = "scout", "Scout", "", ""
        email, avatar_url = "scout@marshellis.com", ""

    return services.upsert_agent(_In(), workspace=ws, owner=owner)


def _post(user, *, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c.post(URL, {}, content_type="application/json")


def test_a_new_agent_is_born_private(agent):
    assert agent.zk_kv == 0


def test_one_click_stores_the_login_sealed_and_never_returns_it(owner, agent):
    _keyset(owner)
    resp = _post(owner)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert "raw" not in body and body["user_email"] == "scout@marshellis.com" and body["created_user"]
    cred = AgentCredential.objects.get(agent=agent, name="CANOPY_WEB_PAT")
    assert cred.value_zk.startswith("zks1:")  # sealed to the owner — canopy-web cannot read it
    agent.refresh_from_db()
    assert agent.pat_rotation_enabled and agent.pat_rotation_enabled_by == owner
    assert body["rotation_enabled"] is True


def test_it_refuses_before_minting_when_nobody_could_open_it(owner, agent):
    resp = _post(owner)
    assert resp.status_code == 409 and "set up your key" in resp.json()["detail"]
    agent.refresh_from_db()
    assert agent.user is None  # no login made, no token minted
    assert not AgentCredential.objects.filter(agent=agent, name="CANOPY_WEB_PAT").exists()


def test_a_browser_needs_a_fresh_passkey(owner, agent):
    _keyset(owner)
    resp = _post(owner, step_up=False)
    assert resp.status_code == 403


def test_the_checklist_has_a_step_for_it(owner, agent):
    step = next(s for s in setup.setup_steps(agent) if s["key"] == "canopy_login")
    assert step["status"] == setup.TODO and step["href"].endswith("#canopy-login")
    assert "canopy agent vault" not in str(setup.setup_steps(agent))  # no terminal command
    _keyset(owner)
    assert _post(owner).status_code == 201
    agent.refresh_from_db()
    step = next(s for s in setup.setup_steps(agent) if s["key"] == "canopy_login")
    assert step["status"] == setup.DONE and "renews itself" in step["detail"]


def test_the_login_is_stored_under_the_variable_its_code_reads():
    """A runner writes each canopy-web value out as the environment variable of its
    own name, and an agent reads its login from CANOPY_WEB_PAT. So that is the name
    the login lives under — `canopy-pat` would be dropped on
    the way, and an agent already storing CANOPY_WEB_PAT had its login reported
    missing."""
    from apps.agents.credentials.pat_rotation import CANOPY_PAT_REF

    assert CANOPY_PAT_REF == "CANOPY_WEB_PAT"
    owner = a_member()
    agent = Agent.objects.create(slug="vaulted", name="Vaulted", workspace=a_workspace(owner=owner),
                                 owner=owner,
                                 runtime_secrets=["CANOPY_WEB_PAT"])
    AgentCredential.objects.create(agent=agent, name="CANOPY_WEB_PAT")
    step = next(s for s in setup.setup_steps(agent) if s["key"] == "canopy_login")
    assert step["status"] == setup.DONE
