"""The admin resolve path (canopy#850).

`GET /credentials/resolve` has two principals: a caller pairing a live runner the
agent routes to (`via: runner`), and the agent's owner or an admin resolving for a
local Claude Code session (`via: admin`). Still bearer-only.
`GET /credentials/access` answers the same gate for the caller without values.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents import services as asvc
from apps.agents.models import Agent, AgentAdmin
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.testing import give_vault_key
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace
from tests.fixtures.device_proof import accept_device_proof

pytestmark = pytest.mark.django_db

WS = "credsrc-ws"
SECRET = "s3cret-not-a-real-token"


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _bearer(user) -> Client:
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {_pat(user)}"
    return c


def _session(user) -> Client:
    """A browser that has just touched its passkey: moving an agent's secrets needs a
    fresh step-up from a browser (test_agent_canopy_login pins the refusal)."""
    from apps.passkeys.testing import grant_step_up

    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


@pytest.fixture
def cs(monkeypatch):
    ws = a_workspace(WS)
    owner = a_member(ws, email="cs-owner@dimagi.com", role=WorkspaceMembership.EDITOR)
    admin = a_member(ws, email="cs-admin@dimagi.com", role=WorkspaceMembership.EDITOR)
    editor = a_member(ws, email="cs-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="csbot", name="CS", workspace=ws, owner=owner,
                                 runtime_secrets=["canopy-pat"])
    AgentAdmin.objects.create(agent=agent, user=admin)
    give_vault_key(owner)  # someone to seal its value to: every value is zero-knowledge
    asvc.set_agent_credentials(agent, {"canopy-pat": SECRET}, user=owner)
    accept_device_proof(monkeypatch)  # these tests are about who may resolve
    return {"ws": ws, "owner": owner, "admin": admin, "editor": editor, "agent": agent}


def _box(user, agent) -> Runner:
    r = Runner.objects.create(
        name=f"box-{user.pk}", kind=Runner.CLOUD, owner=user, status=Runner.ONLINE,
        workspace_id=WS, last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


# --- resolve: runner OR admin, bearer-only -----------------------------------

def _resolve_events(agent):
    from apps.events.models import Event

    return list(Event.objects.filter(kind="agent.credentials.resolved", key__startswith=f"{agent.slug}:"))


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_admin_without_a_runner_resolves_and_is_audited_as_admin(cs, who):
    res = _bearer(cs[who]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["values"]["canopy-pat"].startswith("zks1:") and SECRET not in res.content.decode()
    [ev] = _resolve_events(cs["agent"])
    assert ev.payload["via"] == "admin"


def test_a_runner_resolve_is_audited_as_runner(cs):
    box = _box(cs["admin"], cs["agent"])
    res = _bearer(cs["admin"]).get("/api/agents/csbot/credentials/resolve",
                                   HTTP_X_CANOPY_RUNNER=str(box.pk))
    assert res.status_code == 200, res.content
    [ev] = _resolve_events(cs["agent"])
    assert ev.payload["via"] == "runner"


def test_a_runner_owner_resolving_without_naming_the_box_is_audited_as_admin(cs):
    """canopy-web#1563: pairing a box is a fact about the caller; a request
    that names no box is the admin's own session."""
    _box(cs["admin"], cs["agent"])
    res = _bearer(cs["admin"]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 200, res.content
    [ev] = _resolve_events(cs["agent"])
    assert ev.payload["via"] == "admin"


def test_an_admin_session_cookie_is_still_refused(cs):
    res = _session(cs["owner"]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 403
    assert SECRET not in res.content.decode()


def test_a_plain_member_is_refused_and_told_what_access_to_get(cs):
    res = _bearer(cs["editor"]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 403
    body = res.content.decode()
    assert SECRET not in body
    assert "owner or an agent admin" in body and "cs-owner@dimagi.com" in body
    assert _resolve_events(cs["agent"]) == []


def test_a_non_member_gets_404_not_a_reason(cs):
    stranger = get_user_model().objects.create_user(username="x", email="x@elsewhere.org")
    res = _bearer(stranger).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 404


# --- access: the cheap check, no values --------------------------------------

def test_access_for_an_admin_says_admin(cs):
    res = _session(cs["admin"]).get("/api/agents/csbot/credentials/access")
    assert res.status_code == 200
    assert res.json() == {"agent": "csbot", "may_resolve": True, "via": "admin",
                          "reason": "", "delegated_withheld": []}


def test_access_for_a_runner_pair_says_runner_over_bearer(cs):
    _box(cs["owner"], cs["agent"])
    body = _bearer(cs["owner"]).get("/api/agents/csbot/credentials/access").json()
    assert body["may_resolve"] is True and body["via"] == "runner"


def test_access_for_a_member_says_no_and_why(cs):
    body = _session(cs["editor"]).get("/api/agents/csbot/credentials/access").json()
    assert "credential_source" not in body
    assert body["may_resolve"] is False and body["via"] is None
    assert "admin" in body["reason"]
    assert SECRET not in str(body)


def test_access_hides_the_agent_from_a_non_member(cs):
    stranger = get_user_model().objects.create_user(username="y", email="y@elsewhere.org")
    assert _session(stranger).get("/api/agents/csbot/credentials/access").status_code == 404
