"""The SDK's client (`canopy_sdk.agent_tasks`) against canopy's real routes: a script on
a PAT creates a task, the agent answers it, and `run` returns the result."""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from canopy_sdk import agent_tasks as sdk
from django.test import Client

from apps.agent_tasks import services
from apps.agent_tasks.models import AgentTask

from .helpers import GOOD, INPUT, SCHEMA, _pat, _post

pytestmark = pytest.mark.django_db


def _transport(on_poll=None):
    def call(method, url, body, headers, timeout):
        c = Client(HTTP_AUTHORIZATION=headers["Authorization"])
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if method == "GET":
            if on_poll is not None:
                on_poll()
            r = c.get(path)
        else:
            r = c.post(path, data=body or b"{}", content_type="application/json")
        return r.status_code, r.content
    return call


def _token(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="page backend")
    return raw


def test_run_returns_the_agents_validated_result(world, monkeypatch):
    monkeypatch.setattr(services, "_sleep", lambda _s: None)
    bot = _pat(world["bot"])

    def the_agent_answers():
        task = AgentTask.objects.filter(status__in=AgentTask.OPEN).first()
        if task is not None:
            assert bot.get(f"/api/agent-tasks/{task.pk}").json()["input"] == INPUT
            assert _post(bot, f"/api/agent-tasks/{task.pk}/result", {"result": GOOD}).status_code == 200

    client = sdk.AgentTasks("https://canopy.test", _token(world["owner"]),
                            transport=_transport(on_poll=the_agent_answers))
    assert client.run("bailey", "gmail-triage.review", INPUT, SCHEMA, timeout_s=60) == GOOD


def test_a_cancelled_task_raises(world, monkeypatch):
    monkeypatch.setattr(services, "_sleep", lambda _s: None)
    client = sdk.AgentTasks("https://canopy.test", _token(world["owner"]), transport=_transport())
    task = client.create("bailey", "gmail-triage.review", INPUT, SCHEMA)
    assert client.cancel(task["id"])["status"] == "cancelled"
    assert client.wait(task["id"], 5)["status"] == "cancelled"


def test_a_refusal_is_an_agent_task_error(world):
    client = sdk.AgentTasks("https://canopy.test", _token(world["viewer"]), transport=_transport())
    with pytest.raises(sdk.AgentTaskError) as exc:
        client.create("bailey", "gmail-triage.review", INPUT, SCHEMA)
    assert exc.value.status == 403 and "editor" in exc.value.detail
