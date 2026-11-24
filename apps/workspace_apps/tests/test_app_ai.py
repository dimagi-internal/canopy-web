"""`canopy.ai.complete`: an app that declares `ai` asks canopy's model from a member page, as the viewer
(apps/workspace_apps/ai.py, docs/architecture/app-ai.md)."""
from __future__ import annotations

import copy
import json

import pytest

from apps.workspace_apps import ai, services, template_diff
from apps.workspace_apps import manifest as mf
from apps.workspace_apps.models import AppAiCall
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _client

pytestmark = pytest.mark.django_db

APP = {
    "key": "sales", "name": "Sales", "version": "1",
    "ai": {"purpose": "Summarizes a contact's history and drafts replies."},
    "collections": {"note": {"read": "member", "fields": {"body": {"type": "text"}}}},
    "actions": {"add_note": {"role": "member", "op": "create", "collection": "note", "input": ["body"]}},
    "pages": [{"slug": "home", "title": "Home", "audience": "member", "code": "function View() { return <div/>; }"}],
}
URL = f"{BASE}/sales/ai/complete"
ASK = {"model": "fast", "system": "Be brief.", "messages": [{"role": "user", "content": "Summarize."}],
       "feature": "record-summary"}


def _install(world, m=None):
    r = _client(world["admin"]).post("/api/workspace-apps/dimagi/", {"manifest": m or APP}, content_type=J)
    assert r.status_code == 201, r.content


@pytest.fixture()
def model(monkeypatch, settings):
    """Vertex as a fake: records what was sent, answers Gemini-shaped."""
    settings.HCP_VERTEX_PROJECT = "proj"
    settings.APP_AI_MODELS = {"fast": "vertex/gemini-x-flash", "smart": "vertex/anthropic/claude-x"}
    sent = []
    monkeypatch.setattr(ai, "_retention", {})
    monkeypatch.setattr(ai.embeddings, "check_retention", lambda m: (True, "caching is off"))

    def post(model_id, body):
        sent.append((model_id, body))
        if "anthropic" in model_id:
            return {"content": [{"type": "text", "text": "claude says hi"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 7, "output_tokens": 3}}
        return {"candidates": [{"content": {"role": "model", "parts": [
                    {"text": "thinking…", "thought": True}, {"text": "Hi there.", "thoughtSignature": "sig1"}]},
                    "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4, "thoughtsTokenCount": 9}}

    monkeypatch.setattr(ai, "_post", post)
    return sent


# ---- the declaration ---------------------------------------------------------------------------

def test_manifest_keeps_ai_only_when_declared():
    assert mf.validate(APP)["ai"] == {"purpose": APP["ai"]["purpose"]}
    plain = {k: v for k, v in APP.items() if k != "ai"}
    assert "ai" not in mf.validate(plain)


@pytest.mark.parametrize("raw, why", [
    ({}, "purpose"), ({"purpose": "  "}, "purpose"), ({"purpose": "x", "models": ["fast"]}, "must be"),
    ("yes", "must be"), ({"purpose": "x" * 301}, "at most"),
])
def test_manifest_refuses_a_bad_ai(raw, why):
    m = copy.deepcopy(APP)
    m["ai"] = raw
    with pytest.raises(ValueError, match=why):
        mf.validate(m)


def test_adding_ai_is_an_admins_change_dropping_it_is_not():
    plain = mf.validate({k: v for k, v in APP.items() if k != "ai"})
    declared = mf.validate(APP)
    assert any("AI model" in f for f in template_diff.changes(plain, declared))
    assert not any("AI" in f for f in template_diff.changes(declared, plain))


# ---- calling -----------------------------------------------------------------------------------

def test_a_member_page_asks_and_the_call_is_logged(world, model):
    _install(world)
    r = _client(world["viewer"]).post(URL, ASK, content_type=J)
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["text"] == "Hi there." and out["model"] == "fast" and out["stop_reason"] == "end_turn"
    assert out["content"] == [{"type": "text", "text": "Hi there.", "signature": "sig1"}]  # the thought is dropped
    model_id, body = model[0]
    assert model_id == "vertex/gemini-x-flash"
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Summarize."}]}]
    call = AppAiCall.objects.get()
    assert (call.user, call.alias, call.model, call.status, call.feature) == (
        world["viewer"], "fast", "vertex/gemini-x-flash", "ok", "record-summary")
    assert (call.input_tokens, call.output_tokens) == (11, 13)


def test_an_app_that_does_not_declare_ai_is_refused(world, model):
    _install(world, {k: v for k, v in APP.items() if k != "ai"})
    r = _client(world["viewer"]).post(URL, ASK, content_type=J)
    assert r.status_code == 403
    assert not model and not AppAiCall.objects.exists()


def test_a_stranger_gets_nothing(world, model):
    _install(world)
    assert _client(world["outsider"]).post(URL, ASK, content_type=J).status_code == 404
    assert not model


def test_a_model_id_is_not_an_alias(world, model):
    _install(world)
    r = _client(world["viewer"]).post(URL, {**ASK, "model": "gemini-x-flash"}, content_type=J)
    assert r.status_code == 422
    assert not model


def test_off_without_vertex(world, model, settings):
    settings.HCP_VERTEX_PROJECT = ""
    _install(world)
    assert _client(world["viewer"]).post(URL, ASK, content_type=J).status_code == 503
    assert not model


def test_nothing_is_sent_unless_the_provider_keeps_nothing(world, model, monkeypatch):
    monkeypatch.setattr(ai.embeddings, "check_retention", lambda m: (False, "caching is on"))
    _install(world)
    assert _client(world["viewer"]).post(URL, ASK, content_type=J).status_code == 503
    assert not model
    assert AppAiCall.objects.get().status == "refused"


def test_the_per_viewer_limit(world, model, settings):
    settings.APP_AI_PER_HOUR = 2
    _install(world)
    c = _client(world["viewer"])
    assert [c.post(URL, ASK, content_type=J).status_code for _ in range(3)] == [200, 200, 429]
    assert len(model) == 2
    # Someone else still has theirs.
    assert _client(world["editor"]).post(URL, ASK, content_type=J).status_code == 200


def test_smart_on_claude_with_a_json_schema(world, model):
    _install(world)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    r = _client(world["viewer"]).post(URL, {**ASK, "model": "smart", "json_schema": schema}, content_type=J)
    assert r.status_code == 200, r.content
    model_id, body = model[0]
    assert model_id == "vertex/anthropic/claude-x"
    assert body["tool_choice"] == {"type": "tool", "name": "respond"}
    assert body["tools"][0]["input_schema"] == schema and body["system"] == "Be brief."


# ---- the translations --------------------------------------------------------------------------

TOOL_TURN = {
    "model": "fast", "system": "",
    "tools": [{"name": "soql", "description": "Query", "input_schema": {"type": "object"}}],
    "messages": [
        {"role": "user", "content": "Who is Avery?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "soql",
                                            "input": {"q": "SELECT Id FROM Contact"}, "signature": "s1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": {"rows": 1}}]},
    ],
}


def test_gemini_tool_round_trip():
    body = ai._gemini_body(ai.clean(TOOL_TURN))
    assert body["tools"] == [{"functionDeclarations": [{"name": "soql", "description": "Query",
                                                        "parameters": {"type": "object"}}]}]
    assert body["contents"][1] == {"role": "model", "parts": [
        {"functionCall": {"name": "soql", "args": {"q": "SELECT Id FROM Contact"}}, "thoughtSignature": "s1"}]}
    assert body["contents"][2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "soql", "response": {"result": {"rows": 1}}}}]}
    ans = ai._gemini_answer({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "soql", "args": {"q": "x"}}, "thoughtSignature": "s2"}]}, "finishReason": "STOP"}]})
    assert ans["stop_reason"] == "tool_use"
    (use,) = ans["content"]
    assert use["type"] == "tool_use" and use["name"] == "soql" and use["signature"] == "s2" and use["id"]


def test_claude_tool_round_trip():
    body = ai._claude_body(ai.clean(TOOL_TURN))
    assert body["messages"][1]["content"] == [{"type": "tool_use", "id": "c1", "name": "soql",
                                               "input": {"q": "SELECT Id FROM Contact"}}]
    assert body["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "c1",
                                               "content": json.dumps({"rows": 1})}]
    ans = ai._claude_answer({"content": [{"type": "tool_use", "id": "t", "name": "soql", "input": {}}],
                             "stop_reason": "tool_use"}, json_mode=False)
    assert ans["stop_reason"] == "tool_use"


@pytest.mark.parametrize("raw, why", [
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "assistant", "content": "hi"}]}, "first message"),
    ({"messages": [{"role": "user", "content": [{"type": "tool_use", "name": "x"}]}]}, "type must be"),
    ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 100000}, "max_tokens"),
    ({"messages": [{"role": "user", "content": "x"}], "json_schema": {}, "tools": [{"name": "t"}]}, "not both"),
])
def test_clean_refuses(raw, why):
    with pytest.raises(ai.Refused, match=why):
        ai.clean(raw)


def test_a_viewer_who_left_the_workspace_is_refused(world, model):
    _install(world)
    WorkspaceMembership.objects.filter(user=world["viewer"]).delete()
    assert _client(world["viewer"]).post(URL, ASK, content_type=J).status_code == 404


def test_store_detail_shows_the_purpose(world):
    _install(world)
    assert services.store_detail("dimagi", "dimagi/sales")["ai"] == APP["ai"]["purpose"]
