"""Helpers the workspace-app tests share: the API paths, a logged-in client, and
an action call."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client

J = "application/json"
BASE = "/api/workspace-apps/dimagi"
PUB = "/api/public-apps/dimagi/hiring"


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _client(user=None):
    c = Client()
    if user is not None:
        c.force_login(user)
    return c


def _act(client, action, input, record=None, base=BASE + "/hiring", draft=None):
    q = f"?draft={draft}" if draft else ""
    return client.post(f"{base}/actions/{action}{q}", {"record": record, "input": input}, content_type=J)


def _job(client, status="open", **kw):
    body = {"title": "Senior Engineer", "department": "Engineering", "description": "Build things.", **kw}
    r = _act(client, "create_job", body)
    assert r.status_code == 200, r.content
    jid = r.json()["id"]
    if status == "open":
        assert _act(client, "open_job", {}, record=jid).status_code == 200
    elif status == "closed":
        assert _act(client, "close_job", {}, record=jid).status_code == 200
    return jid
