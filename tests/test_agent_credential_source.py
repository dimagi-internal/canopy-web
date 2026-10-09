"""Credential source + the admin resolve path (canopy#850, design revision 2026-10-09).

Two halves:

- `Agent.credential_source` ("1password" | "canopy-web") says which backend
  `canopy cred` resolves an agent's secrets from. Set by the agent's owner or an
  admin only; the repo's self-publish upsert cannot move it.
- `GET /credentials/resolve` gains a second principal: besides a caller pairing a
  live runner the agent routes to (`via: runner`), the agent's owner or an admin
  may resolve for a local Claude Code session (`via: admin`). Still bearer-only.
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
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

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
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def cs():
    ws = a_workspace(WS)
    owner = a_member(ws, email="cs-owner@dimagi.com", role=WorkspaceMembership.EDITOR)
    admin = a_member(ws, email="cs-admin@dimagi.com", role=WorkspaceMembership.EDITOR)
    editor = a_member(ws, email="cs-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="csbot", name="CS", workspace=ws, owner=owner,
                                 runtime_secrets=["canopy-pat"])
    AgentAdmin.objects.create(agent=agent, user=admin)
    asvc.set_agent_credentials(agent, {"canopy-pat": SECRET}, user=owner)
    return {"ws": ws, "owner": owner, "admin": admin, "editor": editor, "agent": agent}


def _box(user, agent) -> Runner:
    r = Runner.objects.create(
        name=f"box-{user.pk}", kind=Runner.CLOUD, owner=user, status=Runner.ONLINE,
        workspace_id=WS, last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


# --- credential_source ---------------------------------------------------------

def test_credential_source_defaults_to_1password_and_is_on_the_read_schemas(cs):
    c = _session(cs["editor"])
    assert c.get("/api/agents/csbot/").json()["credential_source"] == "1password"
    listed = {a["slug"]: a for a in c.get("/api/agents/").json()["items"]}
    assert listed["csbot"]["credential_source"] == "1password"


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_owner_or_admin_sets_the_credential_source(cs, who):
    res = _session(cs[who]).patch(
        "/api/agents/csbot/credential-source",
        data={"credential_source": "canopy-web"}, content_type="application/json",
    )
    assert res.status_code == 200, res.content
    assert res.json()["credential_source"] == "canopy-web"
    cs["agent"].refresh_from_db()
    assert cs["agent"].credential_source == "canopy-web"


def test_an_editor_cannot_set_the_credential_source(cs):
    res = _session(cs["editor"]).patch(
        "/api/agents/csbot/credential-source",
        data={"credential_source": "canopy-web"}, content_type="application/json",
    )
    assert res.status_code == 403
    cs["agent"].refresh_from_db()
    assert cs["agent"].credential_source == "1password"


def test_an_unknown_credential_source_is_rejected(cs):
    res = _session(cs["owner"]).patch(
        "/api/agents/csbot/credential-source",
        data={"credential_source": "vault-of-glass"}, content_type="application/json",
    )
    assert res.status_code == 422


def test_the_self_publish_upsert_cannot_move_the_credential_source(cs):
    res = _session(cs["owner"]).post(
        "/api/agents/",
        data={"slug": "csbot", "name": "CS", "workspace": WS, "credential_source": "canopy-web"},
        content_type="application/json",
    )
    # StrictModel rejects the unknown field rather than silently dropping it.
    assert res.status_code == 422, res.content
    cs["agent"].refresh_from_db()
    assert cs["agent"].credential_source == "1password"


# --- resolve: runner OR admin, bearer-only -----------------------------------

def _resolve_events(agent):
    from apps.events.models import Event

    return list(Event.objects.filter(kind="agent.credentials.resolved", key__startswith=f"{agent.slug}:"))


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_admin_without_a_runner_resolves_and_is_audited_as_admin(cs, who):
    res = _bearer(cs[who]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 200, res.content
    assert res.json()["values"]["canopy-pat"] == SECRET
    [ev] = _resolve_events(cs["agent"])
    assert ev.payload["via"] == "admin"


def test_a_runner_resolve_is_audited_as_runner(cs):
    _box(cs["admin"], cs["agent"])
    res = _bearer(cs["admin"]).get("/api/agents/csbot/credentials/resolve")
    assert res.status_code == 200, res.content
    [ev] = _resolve_events(cs["agent"])
    assert ev.payload["via"] == "runner"


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
    assert res.json() == {"agent": "csbot", "credential_source": "1password",
                          "may_resolve": True, "via": "admin", "reason": ""}


def test_access_for_a_runner_pair_says_runner_over_bearer(cs):
    _box(cs["owner"], cs["agent"])
    body = _bearer(cs["owner"]).get("/api/agents/csbot/credentials/access").json()
    assert body["may_resolve"] is True and body["via"] == "runner"


def test_access_for_a_member_says_no_and_why(cs):
    cs["agent"].credential_source = "canopy-web"
    cs["agent"].save(update_fields=["credential_source"])
    body = _session(cs["editor"]).get("/api/agents/csbot/credentials/access").json()
    assert body["credential_source"] == "canopy-web"
    assert body["may_resolve"] is False and body["via"] is None
    assert "admin" in body["reason"]
    assert SECRET not in str(body)


def test_access_hides_the_agent_from_a_non_member(cs):
    stranger = get_user_model().objects.create_user(username="y", email="y@elsewhere.org")
    assert _session(stranger).get("/api/agents/csbot/credentials/access").status_code == 404
