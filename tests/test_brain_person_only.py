"""Every person-only brain route asks ONE question (`brain.person.refusal`): the person, as themselves, on their
browser session or a PAT they minted by hand, never over MCP. An OAuth client signed in as them (an AI client or a
connector) and a host site's or MCP host grant's delegated token speak FOR them, so consent, removal, the disclosure
summary, reporting lines and the brain page refuse them alike."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.test import Client

from apps.brain import person
from apps.oauth_server.models import OAuthClient, OAuthGrant
from apps.tokens.models import PersonalToken
from tests.fixtures.brain_world import agree, brain_world

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture()
def w():
    return brain_world()


def _oauth(user) -> str:
    client = OAuthClient.objects.create(client_id=f"c-{user.pk}", client_name="Claude", redirect_uris=[])
    grant = OAuthGrant.objects.create(client=client, user=user)
    raw, _ = PersonalToken.create_for_user(user=user, label="oauth", ttl=timedelta(hours=1), oauth_grant=grant)
    return raw


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    return raw


def _as(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


ROUTES = [
    ("put", "/api/me/learning/", {"classes": ["project"]}),
    ("get", "/api/me/learning/", None),
    ("delete", "/api/me/learning/", None),
    ("get", "/api/me/learning/disclosure/", None),
    ("get", "/api/me/learning/removed/", None),
    ("put", "/api/me/learning/sessions/{sid}/", None),
    ("delete", "/api/me/learning/sessions/{sid}/", None),
    ("get", "/api/brain/workspaces/acme/reporting/", None),
    ("get", "/api/w/acme/brain/state", None),
]


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_an_oauth_client_signed_in_as_the_person_is_refused(w, method, path, body):
    agree(w["alice"], "project")
    path = path.format(sid=w["session"].pk)
    kw = {"data": body, "content_type": J} if body is not None else {}
    r = getattr(Client(), method)(path, **kw, **_as(_oauth(w["alice"])))
    assert r.status_code == 403, (path, r.status_code, r.content[:200])


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_the_person_s_own_pat_still_works(w, method, path, body):
    agree(w["alice"], "project")
    path = path.format(sid=w["session"].pk)
    kw = {"data": body, "content_type": J} if body is not None else {}
    r = getattr(Client(), method)(path, **kw, **_as(_pat(w["alice"])))
    assert r.status_code in (200, 204), (path, r.status_code, r.content[:200])


@pytest.mark.parametrize("cred, via_mcp, ok", [
    (None, False, True),
    ({"type": "pat", "id": 1}, False, True),
    (None, True, False),
    ({"type": "oauth", "id": 2}, False, False),
    ({"type": "delegated", "id": 3}, False, False),
    ({"type": "act_as", "id": 4}, False, False),
    ({"type": "caller_token", "id": 5}, False, False),
    ({"type": "made-up", "id": 6}, False, False),
])
def test_one_question_decides_it(w, cred, via_mcp, ok):
    req = SimpleNamespace(user=w["alice"], META={}, auth_credential=cred, via_mcp=via_mcp)
    assert (person.refusal(req) is None) is ok
