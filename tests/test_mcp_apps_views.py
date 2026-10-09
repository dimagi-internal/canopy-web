"""MCP Apps Views: which rows get one, and what a View may do — as the VIEWER.

Spec 2026-10-08 §3-6 and the owner decisions in its last section. Driven through
the real REST routes (Django test client, real auth) against a real FastMCP host
(`tests/mcp_apps_host.py`) reached in-process. Each fake grant carries its own
token, so every assertion about WHOSE grant a call used is checked at the host.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx2
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions import agui
from apps.canopy_sessions.consumers import without_tools
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.common.encryption import encrypt_secret
from apps.contacts.models import Contact
from apps.harness import caller_context
from apps.harness.models import Turn
from apps.mcp.models import MCPAuditLog
from apps.tokens import client_identity, host_gateway, mcp_apps, mcp_apps_views
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken, HostGrant, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.mcp_apps_host import VIEW_HTML, VIEW_URI, FakeLabs

pytestmark = pytest.mark.django_db

RESOURCE = "https://labs.test/mcp/"
CALL_ID = "toolu_01COACH"
TOKENS = {"owner": "grant-owner", "editor": "grant-editor", "viewer": "grant-viewer",
          "contact": "grant-contact"}


@pytest.fixture()
def labs(monkeypatch):
    fake = FakeLabs(tokens=set(TOKENS.values()),
                    csp={"connectDomains": ["https://labs.test", "https://evil.test"]})
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=fake.app))
    return fake


def _member(ws, name, role=WorkspaceMembership.EDITOR):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw", first_name=name.title())
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    return u


def _grant(app, *, token, user=None, contact=None, minutes=10):
    return HostGrant.objects.create(
        app=app, subject=token, user=user, contact=contact, access_token_enc=encrypt_secret(token),
        scope="workflow:act", resource=RESOURCE, dpop_jkt=client_identity.dpop_jkt(),
        expires_at=timezone.now() + timedelta(minutes=minutes))


def _tool_pair(session, *, call_id=CALL_ID, name="mcp__claude_ai_connect_labs__workflow_run_action",
               input_=None, start=10, turn=None, result=None):
    Message.objects.create(session=session, turn=turn, turn_index=start, role=Message.TOOL_USE,
                           content={"type": "tool_use", "id": call_id, "name": name,
                                    "input": input_ if input_ is not None else
                                    {"run_id": 5, "action": "coach", "arguments": {"worker": 9}}})
    Message.objects.create(session=session, turn=turn, turn_index=start + 1, role=Message.TOOL_RESULT,
                           plaintext="preview ready",
                           content={"type": "tool_result", "tool_use_id": call_id,
                                    "content": result if result is not None else
                                    [{"type": "text", "text": "Preview: coach worker 9"}]})


@pytest.fixture()
def w():
    owner = User.objects.create_user("jon", "jon@dimagi.com", "pw", first_name="Jon")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    editor, viewer, nogrant = _member(ws, "eddie"), _member(ws, "vera"), _member(ws, "nora")
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, interface=parse(
        {"capabilities": {"connect": {"callers": ["contact"], "sites": ["connect-labs"],
                                      "ceiling": ["workflow_*", "marketplace_*"],
                                      "tools": ["mcp__*canopy-web__who_is_asking"]}}}))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.host_issuer = "https://labs.test"
    app.host_mcp_resource = RESOURCE
    app.allowed_frame_origins = ["https://labs.test"]
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    session = Session.objects.create(workspace=ws, agent=agent, created_by=owner,
                                     metadata={"embed_app": "connect-labs"})
    SessionParticipant.objects.create(session=session, user=editor, role=SessionParticipant.EDITOR)
    SessionParticipant.objects.create(session=session, user=viewer, role=SessionParticipant.VIEWER)
    SessionParticipant.objects.create(session=session, user=nogrant, role=SessionParticipant.EDITOR)
    turn = Turn.objects.create(chat_session=session, prompt="coach worker 9", initiator_user=owner,
                               initiator_kind="user")
    _tool_pair(session, turn=turn)
    for who, user in (("owner", owner), ("editor", editor), ("viewer", viewer)):
        _grant(app, token=TOKENS[who], user=user)
    mcp_apps.record_tools(app.pk, [
        ("workflow_run_action", {"ui": {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}}),
        ("workflow_action_preview_view", {"ui": {"resourceUri": VIEW_URI, "visibility": ["app"]}}),
    ])
    return {"owner": owner, "editor": editor, "viewer": viewer, "nogrant": nogrant, "ws": ws,
            "agent": agent, "app": app, "session": session, "turn": turn}


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def through(labs, fn):
    """Run a (sync) test-client call while the fake host's lifespan is up."""
    async def main():
        async with labs.lifespan():
            return await sync_to_async(fn, thread_sensitive=True)()
    return async_to_sync(main)()


def base(w):
    return f"/api/canopy-sessions/{w['session'].pk}/apps/{CALL_ID}"


def post(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json")


# --- §3 which rows ------------------------------------------------------------------------


def test_the_rest_transcript_marks_the_result_with_its_view(w):
    rows = client_for(w["owner"]).get(f"/api/canopy-sessions/{w['session'].pk}").json()["messages"]
    by_role = {m["role"]: m for m in rows}
    assert by_role["tool_result"]["app"] == {
        "tool_call_id": CALL_ID, "site": "connect-labs", "tool": "workflow_run_action",
        "resource_uri": VIEW_URI, "path": "direct"}
    assert by_role["tool_use"].get("app") is None


def test_path_g_site_call_gets_the_view_too(w):
    _tool_pair(w["session"], call_id="toolu_G", start=20, name="mcp__canopy-web__site_call",
               input_={"tool": "workflow_run_action", "arguments": {"run_id": 5, "action": "coach"}})
    rows = client_for(w["owner"]).get(f"/api/canopy-sessions/{w['session'].pk}").json()["messages"]
    g = [m for m in rows if m["role"] == "tool_result" and m["content"]["tool_use_id"] == "toolu_G"]
    assert g[0]["app"]["path"] == "gateway" and g[0]["app"]["tool"] == "workflow_run_action"


def test_an_unindexed_tool_or_a_session_off_site_gets_no_view(w):
    _tool_pair(w["session"], call_id="toolu_X", start=30, name="mcp__x__marketplace_orgs_get",
               input_={})
    w["session"].metadata = {}
    w["session"].save()
    rows = client_for(w["owner"]).get(f"/api/canopy-sessions/{w['session'].pk}").json()["messages"]
    assert not any(m.get("app") for m in rows)


def test_a_live_tool_result_frame_carries_the_view_and_ag_ui_projects_it(w):
    app = mcp_apps_views.app_for_result_block(w["session"], {"tool_use_id": CALL_ID})
    frame = {"event": "chat.tool_result",
             "data": {"tool_message_id": "m1", "turn_index": 11, "app": app,
                      "block": {"tool_use_id": CALL_ID, "content": "raw result"}}}
    [event] = agui.project(frame, thread_id="t")
    assert agui.encode(event)["metadata"]["canopy"]["app"]["tool_call_id"] == CALL_ID
    # The widget gets the View row, never the raw result.
    stripped = without_tools(frame)
    assert stripped["data"]["block"] == {"tool_use_id": CALL_ID}
    assert without_tools({"event": "chat.tool_result", "data": {"block": {}}}) is None


def test_the_widget_snapshot_keeps_only_the_view_row_stripped(w):
    snap = {"event": "session.state", "data": {"messages": [
        {"role": "tool_use", "content": {"id": CALL_ID}},
        {"role": "tool_result", "content": {"tool_use_id": CALL_ID, "content": "secret"},
         "plaintext": "secret", "app": {"tool_call_id": CALL_ID}},
        {"role": "assistant", "content": {}, "plaintext": "hi"}]}}
    rows = without_tools(snap)["data"]["messages"]
    assert [r["role"] for r in rows] == ["tool_result", "assistant"]
    assert "secret" not in json.dumps(rows)


# --- §4-5 the View's requests, as the VIEWER -------------------------------------------------


def test_a_runner_rows_result_text_reaches_the_view(w, labs):
    """The runner's transcript keeps a result's text in `plaintext`, with only
    {is_error, tool_use_id} in `content`. A View must still get that text: Labs'
    coaching card reads the caller's arguments from it (2026-10-09)."""
    text = '{"arguments": {"workers": [{"key": "10::a", "picture": {"params": {"topics": ["X1"]}}}]}}'
    Message.objects.filter(session=w["session"], role=Message.TOOL_RESULT).update(
        plaintext=text, content={"is_error": False, "tool_use_id": CALL_ID})
    r = through(labs, lambda: client_for(w["editor"]).get(base(w) + "/resource"))
    assert r.status_code == 200, r.content
    assert r.json()["tool_result"]["content"][0]["text"] == text


def test_the_resource_is_read_as_the_viewer_and_carries_the_call(w, labs):
    r = through(labs, lambda: client_for(w["editor"]).get(base(w) + "/resource"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["html"] == VIEW_HTML and body["can_act"] is True
    assert body["tool_input"] == {"run_id": 5, "action": "coach", "arguments": {"worker": 9}}
    assert body["tool_result"]["content"][0]["text"] == "Preview: coach worker 9"
    # Narrowed to the site's own origins; the foreign one dropped.
    assert body["csp"] == {"connectDomains": ["https://labs.test"]}
    assert body["sandbox_src"].startswith("/mcp-apps/sandbox/?csp=")
    assert body["prefers_border"] is True
    assert ("resources/read", TOKENS["editor"]) in labs.calls
    w["app"].refresh_from_db()
    assert w["app"].mcp_apps_index["resources"][VIEW_URI]["sha256"]


def test_the_viewers_grant_is_used_not_the_initiators(w, labs):
    """Jon asked (the turn's initiator); Eddie clicks. Eddie's token reaches Labs, and
    the confirm token Labs mints is bound to Eddie — so only Eddie's send works."""
    c = client_for(w["editor"])
    preview = through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_action_preview_view", "arguments": {"run_id": 5, "action": "coach"}}))
    assert preview.status_code == 200, preview.content
    confirm = preview.json()["structuredContent"]["confirm"]
    assert confirm == f"confirm-{TOKENS['editor']}"
    assert labs.calls[-1] == ("workflow_action_preview_view", TOKENS["editor"])
    sent = through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_run_action",
        "arguments": {"run_id": 5, "action": "coach", "confirm": confirm}}))
    assert sent.json()["isError"] is False
    assert sent.json()["structuredContent"] == {"sent": True, "execution_id": 77}
    assert TOKENS["owner"] not in {tok for _, tok in labs.calls}


def test_a_read_only_call_from_a_view_leaves_no_receipt(w, labs):
    """A View's status poll is audited but not recorded in the chat: a receipt witnesses a
    person changing something (2026-10-09: "Done: workflow_action_status" beside a send)."""
    c = client_for(w["editor"])
    polled = through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_action_status", "arguments": {"run_id": 5, "execution_id": 77}}))
    assert polled.json()["isError"] is False
    w["session"].refresh_from_db()
    assert mcp_apps_views.receipts_for(w["session"], CALL_ID) == []
    assert MCPAuditLog.objects.filter(tool="app_view_call", ok=True).count() == 1


def test_a_commit_leaves_a_receipt_and_a_preview_does_not(w, labs):
    c = client_for(w["editor"])
    through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_action_preview_view", "arguments": {"run_id": 5, "action": "coach"}}))
    through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_run_action",
        "arguments": {"run_id": 5, "action": "coach", "confirm": f"confirm-{TOKENS['editor']}"}}))
    w["session"].refresh_from_db()
    [receipt] = mcp_apps_views.receipts_for(w["session"], CALL_ID)
    assert receipt["tool"] == "workflow_run_action" and receipt["is_error"] is False
    assert receipt["by"] == {"name": "Eddie", "user_id": w["editor"].pk}
    assert "execution_id" in receipt["result"]
    # The next turn sees it in the caller envelope — canopy's record, not the View's.
    env = caller_context.build(Turn.objects.get(pk=w["turn"].pk))
    assert env["apps"]["receipts"][0]["tool"] == "workflow_run_action"
    rows = MCPAuditLog.objects.filter(tool="app_view_call", ok=True)
    assert rows.count() == 2 and all(f"viewer=user:{w['editor'].pk}" in r.args_summary for r in rows)
    assert not any(TOKENS["editor"] in r.args_summary for r in MCPAuditLog.objects.all())


def test_a_viewer_role_participant_sees_the_view_read_only_and_is_refused(w, labs):
    c = client_for(w["viewer"])
    r = through(labs, lambda: c.get(base(w) + "/resource"))
    assert r.status_code == 200 and r.json()["can_act"] is False
    assert "not act" in r.json()["read_only_reason"]
    labs.calls.clear()
    refused = through(labs, lambda: post(c, base(w) + "/call", {
        "name": "workflow_action_preview_view", "arguments": {"run_id": 5, "action": "coach"}}))
    assert refused.status_code == 403 and "viewer_cannot_act" in refused.json()["detail"]
    assert labs.calls == []
    assert MCPAuditLog.objects.filter(tool="app_view_call", error="viewer_cannot_act").exists()
    for path, verb, body in (("/context", "put", {"structuredContent": {"x": 1}}),
                             ("/message", "post", {"text": "hi"})):
        r = getattr(c, verb)(base(w) + path, data=json.dumps(body), content_type="application/json")
        assert r.status_code == 403


def test_without_a_live_grant_the_view_is_read_only_from_the_cache(w, labs):
    through(labs, lambda: client_for(w["editor"]).get(base(w) + "/resource"))  # warms the cache
    labs.calls.clear()
    r = through(labs, lambda: client_for(w["nogrant"]).get(base(w) + "/resource"))
    body = r.json()
    assert r.status_code == 200 and body["html"] == VIEW_HTML
    assert body["can_act"] is False and body["sign_in_url"] == "https://labs.test"
    assert labs.calls == []
    refused = through(labs, lambda: post(client_for(w["nogrant"]), base(w) + "/call", {
        "name": "workflow_action_preview_view", "arguments": {}}))
    assert refused.status_code == 403 and "no_grant" in refused.json()["detail"]


def test_an_expired_grant_is_no_grant(w, labs):
    HostGrant.objects.filter(user=w["editor"]).update(expires_at=timezone.now() - timedelta(minutes=1))
    r = through(labs, lambda: post(client_for(w["editor"]), base(w) + "/call", {
        "name": "workflow_action_preview_view", "arguments": {}}))
    assert r.status_code == 403 and "no_grant" in r.json()["detail"]


@pytest.mark.parametrize("name,code,status", [
    ("model_only_report", "not_app_visible", 403),     # gate 4: the host MUST reject
    ("no_such_tool", "not_on_server", 403),            # gate 3
    ("marketplace_orgs_get", "not_allowed", 403),      # gate 5: outside the owner's ceiling
])
def test_each_gate_refuses_with_its_own_code_and_never_calls(w, labs, name, code, status):
    Message.objects.filter(role=Message.TOOL_USE).update(turn=w["turn"])
    Turn.objects.filter(pk=w["turn"].pk).update(capability="connect")
    w["agent"].interface = parse({"capabilities": {"connect": {
        "callers": ["contact"], "sites": ["connect-labs"],
        "ceiling": ["workflow_*", "model_only_report", "no_such_tool"],
        "tools": ["mcp__*canopy-web__who_is_asking"]}}})
    w["agent"].save()
    labs.calls.clear()
    r = through(labs, lambda: post(client_for(w["editor"]), base(w) + "/call",
                                   {"name": name, "arguments": {"run_id": 5}}))
    assert r.status_code == status and code in r.json()["detail"], r.content
    assert name not in labs.tools_called()
    assert MCPAuditLog.objects.filter(tool="app_view_call", ok=False, error=code).exists()


def test_a_call_id_that_is_not_a_view_in_this_session_is_404(w, labs):
    url = f"/api/canopy-sessions/{w['session'].pk}/apps/toolu_nope/call"
    r = through(labs, lambda: post(client_for(w["editor"]), url, {"name": "workflow_run_action"}))
    assert r.status_code == 404 and "not_a_view" in r.json()["detail"]


def test_an_agents_pat_is_refused_and_the_routes_are_not_mcp_tools(w, labs):
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="agent")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = through(labs, lambda: post(c, base(w) + "/call", {"name": "workflow_action_preview_view"}))
    assert r.status_code == 403
    assert c.get(base(w) + "/resource").status_code == 403
    from apps.mcp import api_tools

    for op in ("app_view_resource", "app_view_call", "app_view_read", "app_view_context",
               "app_view_message"):
        assert op in api_tools.EXCLUDED


def test_another_sessions_reader_cannot_reach_the_view(w, labs):
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=stranger, workspace=w["ws"],
                                       role=WorkspaceMembership.EDITOR)
    r = client_for(stranger).get(base(w) + "/resource")
    assert r.status_code == 404


# --- §6 what the agent learns -------------------------------------------------------------


def test_model_context_overwrites_and_is_bounded_and_rides_the_envelope(w):
    c = client_for(w["editor"])
    assert c.put(base(w) + "/context", data=json.dumps({"structuredContent": {"outcome": "x"}}),
                 content_type="application/json").status_code == 200
    assert c.put(base(w) + "/context", data=json.dumps({"structuredContent": {"outcome": "declined"}}),
                 content_type="application/json").status_code == 200
    big = c.put(base(w) + "/context", data=json.dumps({"structuredContent": {"x": "y" * 9000}}),
                content_type="application/json")
    assert big.status_code == 422
    w["session"].refresh_from_db()
    block = mcp_apps_views.envelope_block(w["session"])
    assert [e["structuredContent"] for e in block["context"]] == [{"outcome": "declined"}]


def test_ui_message_is_a_message_from_the_viewer_that_starts_a_turn(w):
    r = post(client_for(w["editor"]), base(w) + "/message",
             {"text": "Sent - follow it with workflow_action_status", "client_id": "v1"})
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.initiator_user_id == w["editor"].pk
    assert Message.objects.filter(session=w["session"], role=Message.USER,
                                  plaintext__startswith="Sent").exists()


# --- the contact path (the embed panel) ---------------------------------------------------


def test_a_contact_in_their_own_session_acts_with_their_own_grant(w, labs):
    contact = Contact.objects.create(workspace=w["ws"], app=w["app"], external_id="c-1",
                                     display_name="Gillian", source=Contact.SOURCE_EMBED)
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], contact=contact,
                               metadata={"embed_app": "connect-labs"})
    _tool_pair(s, call_id="toolu_C", start=1)
    _grant(w["app"], token=TOKENS["contact"], contact=contact)
    raw, _ = ContactToken.issue(app=w["app"], contact=contact, ttl_seconds=600)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    url = f"/api/contact/sessions/{s.pk}/apps/toolu_C"
    # The widget's transcript carries the View row, stripped.
    page = c.get(f"/api/contact/sessions/{s.pk}/messages?before=99").json()["messages"]
    assert [m["role"] for m in page] == ["tool_result"]
    assert page[0]["app"]["tool_call_id"] == "toolu_C" and page[0]["plaintext"] == ""
    r = through(labs, lambda: c.get(url + "/resource"))
    assert r.status_code == 200 and r.json()["can_act"] is True
    out = through(labs, lambda: post(c, url + "/call", {
        "name": "workflow_action_preview_view", "arguments": {"run_id": 5, "action": "coach"}}))
    assert out.json()["structuredContent"]["confirm"] == f"confirm-{TOKENS['contact']}"
    # Another contact's session is not theirs.
    other = Session.objects.create(workspace=w["ws"], agent=w["agent"],
                                   metadata={"embed_app": "connect-labs"})
    assert c.get(f"/api/contact/sessions/{other.pk}/apps/toolu_C/resource").status_code == 404


# --- keeping the index warm ---------------------------------------------------------------


def test_a_visitor_arrival_refreshes_the_index_in_the_background(w, labs, monkeypatch):
    AppCredential.objects.filter(pk=w["app"].pk).update(mcp_apps_index={})
    w["app"].refresh_from_db()
    grant = HostGrant.objects.get(user=w["editor"])
    async def main():
        async with labs.lifespan():
            await sync_to_async(mcp_apps_views._refresh, thread_sensitive=True)(w["app"].pk, grant.pk)
    async_to_sync(main)()
    w["app"].refresh_from_db()
    assert "workflow_run_action" in w["app"].mcp_apps_index["tools"]


# --- §9 Slack: the text fallback and a link, never a button ---------------------------------


def test_slack_gets_the_result_text_and_a_link_for_a_view_and_nothing_for_other_tools(w):
    from types import SimpleNamespace

    from apps.slack.relay import _message_for

    turn = Turn.objects.get(pk=w["turn"].pk)
    view_row = SimpleNamespace(kind="tool_result", payload={
        "tool_use_id": CALL_ID, "content": [{"type": "text", "text": "Preview: coach worker 9"}]})
    line = _message_for(view_row, turn)
    assert line.startswith("Preview: coach worker 9\nOpen it to act: ")
    assert line.endswith(f"/w/connect/chat/{w['session'].pk}")
    other = SimpleNamespace(kind="tool_result", payload={"tool_use_id": "toolu_none", "content": "x"})
    assert _message_for(other, turn) == ""
