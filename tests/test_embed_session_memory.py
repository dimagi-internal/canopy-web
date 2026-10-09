"""Agent memory in the embedded widget.

The widget may SHOW what applies in the visitor's own conversation —
`GET /api/embed/sessions/{id}/agent-memory` — but a site acting for the person
is not the person, so it can never CHANGE it: the write stays on
`/api/people/me/sessions/{id}/agent-memory/`, which a delegated token cannot
reach (apps/tokens/delegation.py). canopy's own widget is signed in by canopy's
session cookie, so there the person is acting and the write goes through.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _granted(grants_on_turn):
    # What the switches do for an agent the person has granted; how grants come to
    # exist is pinned in tests/test_hcp_agent_grants.py.
    yield


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=owner)
    hidden = Agent.objects.create(slug="hidden", name="Hidden", workspace=ws, owner=owner)
    admin = User.objects.create_user("admin-site", "admin-site@dimagi.com", "pw")
    app = AppCredential.create_credential(name="site", created_by=admin, workspace=ws)
    AppCredentialAgent.objects.create(app=app, agent=ace)
    return {"ws": ws, "lili": lili, "owner": owner, "ace": ace, "hidden": hidden, "app": app}


def _bearer(app, user):
    raw, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _session(user, agent):
    return Session.objects.create(agent=agent, created_by=user, workspace=agent.workspace)


def _make_available(user, **features):
    c = Client()
    c.force_login(user)
    body = {f: {"available": True, "default": d} for f, d in features.items()}
    r = c.put("/api/people/me/agent-memory/", data=json.dumps(body),
              content_type="application/json")
    assert r.status_code == 200, r.content


def test_the_widget_shows_the_effective_state_of_your_own_session(w):
    _make_available(w["lili"], record=True, use=False)
    s = _session(w["lili"], w["ace"])
    r = Client().get(f"/api/embed/sessions/{s.pk}/agent-memory", **_bearer(w["app"], w["lili"]))
    assert r.status_code == 200, r.content
    body = r.json()
    assert {k: body["record"][k] for k in ("available", "default", "override", "effective")} == {
        "available": True, "default": True, "override": None, "effective": True}
    assert body["use"]["available"] is True and body["use"]["effective"] is False
    assert body["manage_path"] == f"/w/connect/chat/{s.pk}"


def test_nothing_available_still_answers_so_the_widget_can_hide_it(w):
    s = _session(w["lili"], w["ace"])
    body = Client().get(f"/api/embed/sessions/{s.pk}/agent-memory",
                        **_bearer(w["app"], w["lili"])).json()
    assert body["record"]["available"] is False and body["use"]["available"] is False


def test_someone_elses_session_is_404(w):
    s = _session(w["owner"], w["ace"])
    r = Client().get(f"/api/embed/sessions/{s.pk}/agent-memory", **_bearer(w["app"], w["lili"]))
    assert r.status_code == 404


def test_a_session_with_an_agent_the_site_does_not_offer_is_404(w):
    s = _session(w["lili"], w["hidden"])
    r = Client().get(f"/api/embed/sessions/{s.pk}/agent-memory", **_bearer(w["app"], w["lili"]))
    assert r.status_code == 404


def test_a_browser_session_without_a_site_token_is_refused(w):
    s = _session(w["lili"], w["ace"])
    c = Client()
    c.force_login(w["lili"])
    assert c.get(f"/api/embed/sessions/{s.pk}/agent-memory").status_code == 403


def test_a_site_acting_for_the_person_cannot_change_it(w):
    """The write is outside a delegated token's surface: a host must never be able
    to switch on learning for its visitors."""
    _make_available(w["lili"], record=True, use=True)
    s = _session(w["lili"], w["ace"])
    r = Client().put(f"/api/people/me/sessions/{s.pk}/agent-memory/",
                     data=json.dumps({"record": "off"}), content_type="application/json",
                     **_bearer(w["app"], w["lili"]))
    assert r.status_code == 403
    assert "not_delegable" in r.json()["title"]


def test_canopys_own_widget_signed_in_by_session_can_change_it(w):
    """Same-origin: canopy's session cookie signs the request in, so the person is
    acting and the delegated surface limit does not apply."""
    _make_available(w["lili"], record=True, use=True)
    s = _session(w["lili"], w["ace"])
    c = Client()
    c.force_login(w["lili"])
    r = c.put(f"/api/people/me/sessions/{s.pk}/agent-memory/",
              data=json.dumps({"use": "off"}), content_type="application/json",
              **_bearer(w["app"], w["lili"]))
    assert r.status_code == 200, r.content
    assert r.json()["use"]["effective"] is False
