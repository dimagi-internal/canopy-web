"""Shared by the agent-task suites: a family workspace with Bailey, and REST shorthands."""
from __future__ import annotations

import json

from django.test import Client

from apps.agents.models import Agent
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

SCHEMA = {
    "type": "object",
    "required": ["decisions"],
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object", "required": ["id", "action", "reason"],
        "properties": {"id": {"type": "string"}, "action": {"enum": ["keep", "archive", "delete"]},
                       "reason": {"type": "string"}},
    }}},
}
INPUT = {"emails": [{"id": "m1", "from": "school@example.org", "subject": "Trip form"}]}
GOOD = {"decisions": [{"id": "m1", "action": "keep", "reason": "a form to sign"}]}


def make_world():
    ws = a_workspace("family")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    viewer = a_member(ws, email="vi@dimagi.com", role=M.VIEWER)
    outsider = a_member(a_workspace("elsewhere"), email="out@example.com", role=M.OWNER)
    bot = a_member(ws, email="bailey@marshellis.com", role=M.VIEWER)
    agent = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=owner, user=bot)
    return {"ws": ws, "owner": owner, "editor": editor, "viewer": viewer, "outsider": outsider,
            "bot": bot, "agent": agent}


def _pat(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="script")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _post(c, path, body):
    return c.post(path, data=json.dumps(body), content_type="application/json")


def _create(c, **extra):
    body = {"agent": "bailey", "name": "gmail-triage.review", "input": INPUT, "output_schema": SCHEMA, **extra}
    return _post(c, "/api/agent-tasks/", body)


