"""A site acting for a canopy user reaches `site ∩ user`, not the user's whole canopy.

The setup every test here shares is the one that made this matter: a person
who is an OWNER of two workspaces arrives through a connected site registered
in one of them, offering one agent. The token the site holds must let them talk
to that agent — and nothing else they could do if they signed in to canopy
themselves. See `apps/tokens/delegation.py`.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    me = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=me)
    ceo = Workspace.objects.create(slug="ceo-office", display_name="CEO", created_by=me)
    for ws in (connect, ceo):
        WorkspaceMembership.objects.create(user=me, workspace=ws, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=connect)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=connect)   # same tenant, not offered
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ceo)         # other tenant
    site = AppCredential.create_credential(name="ace-web", created_by=me, workspace=connect)
    AppCredentialAgent.objects.create(app=site, agent=ace)
    raw, _ = DelegatedToken.issue(app=site, user=me, ttl_seconds=600)
    return {"me": me, "ace": ace, "echo": echo, "eva": eva, "site": site,
            "auth": {"HTTP_AUTHORIZATION": f"Bearer {raw}"}, "raw": raw}


def _session(agent, owner):
    return Session.objects.create(workspace_id=agent.workspace_id, created_by=owner, agent=agent)


# --- which surface ------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("get", "/api/me/"),
    ("get", "/api/workspaces/"),
    ("get", "/api/agents/"),
    ("post", "/api/tokens/"),
    ("post", "/api/harness/turns/"),
])
def test_the_token_does_not_reach_the_rest_of_canopy(world, method, path):
    r = getattr(Client(), method)(path, content_type="application/json", **world["auth"])

    assert r.status_code == 403
    assert r.json()["detail"].startswith("not_delegable")


def test_the_same_person_with_their_own_credential_still_does(world):
    """The limit is on the SITE, not on the person."""
    raw, _ = PersonalToken.create_for_user(user=world["me"], label="own")
    assert Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 200


def test_the_chat_surface_is_reachable(world):
    c = Client()
    assert c.get("/api/embed/agents", **world["auth"]).status_code == 200
    assert c.get("/api/canopy-sessions/", **world["auth"]).status_code == 200
    assert c.get("/api/harness/turns/", **world["auth"]).status_code == 200


def test_a_canopy_session_at_the_browser_is_the_person_not_the_site(world):
    """canopy embedding its own widget is same-origin: the cookie is the
    identity, so the surface limit does not apply — the agent limit still does."""
    c = Client()
    c.force_login(world["me"])
    assert c.get("/api/me/", **world["auth"]).status_code == 200
    _session(world["echo"], world["me"])
    assert c.get("/api/canopy-sessions/", **world["auth"]).json() == []


# --- which agents -------------------------------------------------------------


def test_it_opens_a_chat_only_with_an_agent_the_site_offers(world):
    c = Client()
    ok = c.post("/api/w/connect/canopy-sessions/", data={"agent_slug": "ace"},
                content_type="application/json", **world["auth"])
    refused = c.post("/api/w/connect/canopy-sessions/", data={"agent_slug": "echo"},
                     content_type="application/json", **world["auth"])

    assert ok.status_code == 200, ok.content
    assert refused.status_code == 404


def test_it_cannot_open_the_users_other_chats_by_id(world):
    """The user's own chats with other agents — here and in their other
    workspace — are the same 404 a stranger gets."""
    c = Client()
    for agent in (world["echo"], world["eva"]):
        s = _session(agent, world["me"])
        assert c.get(f"/api/canopy-sessions/{s.id}", **world["auth"]).status_code == 404
        assert c.post(f"/api/canopy-sessions/{s.id}/send", data={"text": "hi"},
                      content_type="application/json", **world["auth"]).status_code == 404
    mine = _session(world["ace"], world["me"])
    assert c.get(f"/api/canopy-sessions/{mine.id}", **world["auth"]).status_code == 200


def test_it_sees_only_its_own_agents_turns(world):
    c = Client()
    offered = Turn.objects.create(agent=world["ace"], origin=Turn.ORIGIN_API, idempotency_key="a")
    other = Turn.objects.create(agent=world["eva"], origin=Turn.ORIGIN_API, idempotency_key="b")

    listed = {t["id"] for t in c.get("/api/harness/turns/", **world["auth"]).json()}

    assert listed == {str(offered.pk)}
    assert c.get(f"/api/harness/turns/{other.pk}", **world["auth"]).status_code == 404
    assert c.get(f"/api/harness/turns/{offered.pk}", **world["auth"]).status_code == 200


def test_it_sees_only_the_runners_serving_its_agents(world):
    mine = Runner.objects.create(name="ace-box", kind=Runner.EMDASH, paired_by=world["me"])
    theirs = Runner.objects.create(name="eva-box", kind=Runner.EMDASH, paired_by=world["me"])
    RunnerAssignment.objects.create(agent=world["ace"], runner=mine, rank=0)
    RunnerAssignment.objects.create(agent=world["eva"], runner=theirs, rank=0)

    names = {r["name"] for r in Client().get("/api/harness/runners/", **world["auth"]).json()}

    assert "eva-box" not in names


def test_a_site_offering_nothing_reaches_nothing(world):
    AppCredentialAgent.objects.filter(app=world["site"]).delete()
    _session(world["ace"], world["me"])

    assert Client().get("/api/canopy-sessions/", **world["auth"]).json() == []


# --- the live socket ----------------------------------------------------------


def _ws_scope(path, raw):
    captured = {}

    async def app(scope, receive, send):
        captured.update(scope)

    scope = {"type": "websocket", "path": path, "query_string": f"token={raw}".encode(),
             "headers": []}
    async_to_sync(RealtimeAuthMiddleware(app))(scope, None, None)
    return captured


def test_the_token_opens_only_its_chat_socket(world):
    s = _session(world["ace"], world["me"])
    chat = _ws_scope(f"/ws/canopy-sessions/{s.id}/", world["raw"])
    presence = _ws_scope("/ws/presence/", world["raw"])

    assert chat["user"].pk == world["me"].pk
    assert chat["delegated_app"].pk == world["site"].pk
    assert not presence["user"].is_authenticated
