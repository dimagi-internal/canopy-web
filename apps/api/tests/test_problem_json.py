"""Every error is RFC 7807 problem+json, built one way.

`title` is stable per `type` (for `about:blank`, the status's reason phrase);
the per-occurrence message is `detail`.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model

from apps.api.errors import problem_response, status_title
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


def test_problem_response_shape_and_members():
    resp = problem_response(409, detail="already there", error="dup", n=2)
    assert resp.status_code == 409
    assert resp["content-type"] == "application/problem+json"
    assert json.loads(resp.content) == {
        "type": "about:blank", "title": "Conflict", "status": 409,
        "detail": "already there", "error": "dup", "n": 2,
    }


def test_problem_response_escapes_any_text():
    body = json.loads(problem_response(404, detail='a "quoted" \\ slug\n').content)
    assert body["detail"] == 'a "quoted" \\ slug\n'


def test_status_title_is_the_reason_phrase():
    assert status_title(404) == "Not Found"
    assert status_title(599) == "Error"


@pytest.mark.django_db
def test_unknown_workspace_slug_with_a_quote_is_valid_json(client):
    u = User.objects.create(username="m@dimagi.com", email="m@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u)
    wsvc.ensure_member(ws, u, WorkspaceMembership.OWNER)
    client.force_login(u)
    r = client.get('/api/w/a%22b%5C/agents/')
    assert r.status_code == 404
    body = json.loads(r.content)  # used to be invalid JSON: the slug went in unescaped
    assert body["title"] == "Not Found"
    assert body["detail"] == "workspace 'a\"b\\' not found"


@pytest.mark.django_db
def test_bare_http_error_title_is_stable_and_message_is_detail(client):
    u = User.objects.create(username="m@dimagi.com", email="m@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u)
    wsvc.ensure_member(ws, u, WorkspaceMembership.OWNER)
    client.force_login(u)
    r = client.get("/api/agents/nope-not-an-agent/")
    assert r.status_code == 404
    body = r.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert "nope-not-an-agent" in body["detail"]
