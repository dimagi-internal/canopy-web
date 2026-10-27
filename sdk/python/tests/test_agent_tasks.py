"""Agent tasks (``canopy_sdk.agent_tasks``): the client's requests, its long-poll loop,
and how each ending surfaces. canopy's side is tested in canopy-web
(apps/agent_tasks/tests), including a run of this client against its real routes."""
from __future__ import annotations

import json

import pytest
from canopy_sdk import agent_tasks as at

SCHEMA = {"type": "object"}


class Fake:
    """A transport answering from a script of `(status, body)`, recording each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append({"method": method, "url": url, "body": json.loads(body) if body else None,
                           "headers": headers, "timeout": timeout})
        status, doc = self.answers.pop(0)
        return status, json.dumps(doc).encode()


def _client(fake, **kw):
    return at.AgentTasks("https://canopy.example.org/", "pat-1", transport=fake, **kw)


def test_create_posts_the_task_with_the_token():
    fake = Fake((201, {"id": "t1", "status": "queued"}))
    task = _client(fake).create("bailey", "gmail-triage.review", {"emails": []}, SCHEMA,
                                instructions="be brief", timeout_s=120)
    assert task["id"] == "t1"
    (call,) = fake.calls
    assert call["method"] == "POST" and call["url"] == "https://canopy.example.org/api/agent-tasks/"
    assert call["headers"]["Authorization"] == "Bearer pat-1"
    assert call["body"] == {"agent": "bailey", "name": "gmail-triage.review", "input": {"emails": []},
                            "output_schema": SCHEMA, "instructions": "be brief",
                            "routing": "prefer_local", "timeout_s": 120}


def test_a_workspace_goes_through_its_tenant_prefix():
    fake = Fake((200, {"id": "t1", "status": "queued"}))
    _client(fake, workspace="family").get("t1", wait_s=5)
    assert fake.calls[0]["url"] == "https://canopy.example.org/api/w/family/agent-tasks/t1?wait_s=5"
    assert fake.calls[0]["timeout"] > 5


def test_wait_s_is_capped_at_the_servers_ceiling():
    fake = Fake((200, {"id": "t1", "status": "queued"}))
    _client(fake).get("t1", wait_s=99)
    assert fake.calls[0]["url"].endswith("?wait_s=20")


def test_run_returns_the_result_once_done():
    fake = Fake((201, {"id": "t1", "status": "queued"}),
                (200, {"id": "t1", "status": "running"}),
                (200, {"id": "t1", "status": "done", "result": {"decisions": []}}))
    assert _client(fake).run("bailey", "x", {}, SCHEMA) == {"decisions": []}
    assert [c["method"] for c in fake.calls] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_run_raises_on_a_task_that_did_not_finish(status):
    fake = Fake((201, {"id": "t1", "status": "queued"}),
                (200, {"id": "t1", "status": status, "error": "the agent ended without a result"}))
    with pytest.raises(at.AgentTaskFailed) as exc:
        _client(fake).run("bailey", "x", {}, SCHEMA)
    assert "the agent ended without a result" in str(exc.value)
    assert exc.value.task["status"] == status


def test_wait_times_out(monkeypatch):
    clock = iter([0, 0, 25, 25, 50, 50])
    monkeypatch.setattr(at, "_monotonic", lambda: next(clock))
    fake = Fake((200, {"id": "t1", "status": "running"}), (200, {"id": "t1", "status": "running"}))
    with pytest.raises(at.AgentTaskTimeout):
        _client(fake).wait("t1", 40)
    assert len(fake.calls) == 2


def test_a_refusal_carries_status_detail_and_errors():
    fake = Fake((422, {"title": "Invalid output_schema", "detail": "not a valid JSON Schema",
                       "errors": [{"path": "/a", "message": "bad"}]}))
    with pytest.raises(at.AgentTaskError) as exc:
        _client(fake).create("bailey", "x", {}, {"type": "nope"})
    assert exc.value.status == 422 and exc.value.detail == "not a valid JSON Schema"
    assert exc.value.errors == [{"path": "/a", "message": "bad"}]


def test_it_needs_a_url_and_a_token():
    with pytest.raises(ValueError):
        at.AgentTasks("", "t")
    with pytest.raises(ValueError):
        at.AgentTasks("https://canopy.example.org", "")
