"""Lean page turns: a question asked in a site's panel starts with only what a page needs.

An agent's owner sets `page_lean: true` (and optionally `page_model:`) on its host-site entry.
Then every turn held on that site's pages — the owner's included —

* is claimed with `page_site.lean`, so the runner loads canopy's plugin only;
* is shown only the page and gateway tools of canopy's MCP (`turn_scope.LEAN_PAGE_TOOLS`);
* can fetch its page brief — the page's state and the site's tools, exactly as `current_page`
  and `site_tools` would answer, as the person who sent — while its CLI starts;
* starts on `page_model` when one is set;
* gets its conversation prepared on the runner when the panel opens (`harness.prepare`),
* and streams the reply it is writing to the panel (`chat.live_text`), never stored.

None of it widens what a turn may do: the gateway, the grant and every refusal are the same.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest import mock

import pytest
from asgiref.sync import async_to_sync

from apps.agents import profile_settings, published_profiles
from apps.agents.published_profiles import InterfaceError, parse
from apps.canopy_sessions import agui, chat_keys
from apps.canopy_sessions.models import Session
from apps.harness import page_brief, prepare
from apps.harness.claim_payload import claim_payload
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.mcp.server import mcp
from apps.mcp.turn_scope import LEAN_PAGE_TOOLS, lean_page_tool
from apps.tokens.models import PersonalToken
from tests.access_helpers import declare
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db

LEAN = {
    "connect": {"sites": ["connect-labs"], "page_lean": True, "page_model": "claude-sonnet-5-5",
                "own_login": {"connect-labs": ["LABS_MCP_TOKEN"]},
                "tools": ["mcp__*canopy-web__who_is_asking"]},
}


@pytest.fixture()
def host(monkeypatch):
    return sg.host_mcp(monkeypatch)


@pytest.fixture()
def w():
    w = sg.world()
    declare(w["agent"], LEAN)
    w["chat"] = Session.objects.create(workspace=w["ws"], agent=w["agent"],
                                       metadata={"embed_app": "connect-labs"},
                                       page_state={"resource": "labs-run://kmc/1",
                                                   "backing_tool": "marketplace_orgs_get",
                                                   "visible_ids": [f"id-{n}" for n in range(100)]})
    w["owner_turn"] = Turn.objects.create(chat_session=w["chat"], prompt="which partner?",
                                          initiator_user=w["owner"], initiator_kind="user",
                                          idempotency_key=str(uuid.uuid4()))
    return w


@contextlib.contextmanager
def _owner_session(w):
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="agent login")
    key = chat_keys.mint(w["chat"])
    with sg.as_token(raw), mock.patch("apps.mcp.chat_scope.get_http_headers",
                                      return_value={chat_keys.HEADER.lower(): key}):
        yield


# --- the entry ------------------------------------------------------------------------


def test_the_entry_takes_page_lean_and_page_model_only_with_sites():
    out = parse({"capabilities": LEAN})["capabilities"]["connect"]
    assert out["page_lean"] is True and out["page_model"] == "claude-sonnet-5-5"
    with pytest.raises(InterfaceError, match="need `sites:`"):
        parse({"capabilities": {"x": {"page_lean": True}}})
    with pytest.raises(InterfaceError, match="page_model must be a model name"):
        parse({"capabilities": {"x": {"sites": ["s"], "page_model": "rm -rf /"}}})
    with pytest.raises(InterfaceError, match="page_lean must be true or false"):
        parse({"capabilities": {"x": {"sites": ["s"], "page_lean": "yes"}}})


def test_an_entry_without_them_parses_to_exactly_what_it_did():
    out = parse({"capabilities": {"x": {"sites": ["s"]}}})["capabilities"]["x"]
    assert "page_lean" not in out and "page_model" not in out


def test_the_settings_write_and_read_them(w):
    profile_settings.set_site(w["agent"], "connect-labs", {"page_lean": False, "page_model": "haiku"},
                              by=w["owner"])
    w["agent"].refresh_from_db()
    row = next(r for r in profile_settings.host_sites_of(w["agent"]) if r["site"] == "connect-labs")
    assert row["page_lean"] is False and row["page_model"] == "haiku"
    profile_settings.set_site(w["agent"], "connect-labs", {"page_lean": True}, by=w["owner"])
    w["agent"].refresh_from_db()
    row = next(r for r in profile_settings.host_sites_of(w["agent"]) if r["site"] == "connect-labs")
    assert row["page_lean"] is True and row["page_model"] == "haiku"


# --- the claim --------------------------------------------------------------------------


def test_the_claim_says_lean_and_starts_on_the_page_model(w):
    out = claim_payload(Turn.objects.get(pk=w["owner_turn"].pk))
    assert out["page_site"] == {"site": "connect-labs", "withheld": ["LABS_MCP_TOKEN"], "lean": True}
    assert out["cli_model"] == "claude-sonnet-5-5"
    # The conversation holds it (first claim wins), as for any model.
    assert Session.objects.get(pk=w["chat"].pk).cli_model == "claude-sonnet-5-5"
    # The agent is told nothing about how it was started.
    assert "lean" not in out["caller_context"]["site"]


def test_a_conversation_off_the_page_keeps_the_agents_own_model(w):
    w["agent"].cli_models = {"claude-code": "opus"}
    w["agent"].save(update_fields=["cli_models"])
    chat = Session.objects.create(workspace=w["ws"], agent=w["agent"])
    turn = Turn.objects.create(chat_session=chat, prompt="x", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=str(uuid.uuid4()))
    out = claim_payload(Turn.objects.get(pk=turn.pk))
    assert out["page_site"] is None and out["cli_model"] == "opus"


# --- canopy's MCP shows a lean page turn the page's tools only ---------------------------


def test_a_lean_page_turn_is_listed_the_page_tools_only(w):
    with _owner_session(w):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"site_tools", "site_call", "site_read", "current_page", "who_is_asking"} <= names
    assert all(lean_page_tool(n) for n in names), sorted(n for n in names if not lean_page_tool(n))
    assert names <= LEAN_PAGE_TOOLS | {n for n in names if n.startswith("page_")}


def test_a_page_turn_that_is_not_lean_still_sees_everything(w):
    declare(w["agent"], {"connect": {**LEAN["connect"], "page_lean": False}})
    with _owner_session(w):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert "create_task" in names and len(names) > 100


def test_lean_narrows_the_list_and_widens_no_call(w, host):
    """Narrowing is the list only; what a call may do is the gateway's, unchanged:
    with no grant the page turn is refused exactly as before."""
    with _owner_session(w), pytest.raises(Exception, match="back on the page"):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == []


# --- the page brief -----------------------------------------------------------------------


def test_the_brief_is_the_page_and_the_sites_tools_as_the_person(w, host):
    from apps.host_grants import host_gateway

    grant = sg.grant(w, subject=f"sub-{w['owner'].pk}", contact=None, user=w["owner"])
    seen = []

    async def list_tools(ctx):
        seen.append(ctx)
        return [{"name": "marketplace_orgs_get", "description": "List orgs. " + "x" * 400,
                 "input_schema": {"type": "object", "properties": {
                     "org": {"type": "integer", "description": "which org"}}}},
                {"name": "data_query", "description": "Run one SELECT. " + "y" * 400,
                 "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}},
                                  "required": ["sql"]}}]

    with mock.patch.object(host_gateway, "list_tools", list_tools):
        out = page_brief.build(Turn.objects.get(pk=w["owner_turn"].pk))
    # Listed through the gateway, under the OWNER's own confirmed grant for the page.
    assert [(c.turn_id, c.scope) for c in seen] == [(str(w["owner_turn"].pk), grant.scope)]
    assert out["refused"] == "" and out["site"] == "connect-labs" and out["tools"] == 2
    assert "labs-run://kmc/1" in out["text"] and "marketplace_orgs_get" in out["text"]
    # The page's own backing tool keeps its whole description; another gets one line.
    assert "x" * 400 in out["text"] and "y" * 400 not in out["text"]
    assert '"required":["sql"]' in out["text"] and "which org" not in out["text"]
    # A long selection is cut, and says so.
    assert "id-39" in out["text"] and "id-40\"" not in out["text"] and "visible_ids_total" in out["text"]
    assert host.calls == []


def test_without_a_grant_the_brief_lists_nothing_and_says_why(w, host):
    out = page_brief.build(Turn.objects.get(pk=w["owner_turn"].pk))
    assert out["tools"] == 0 and out["refused"]
    assert "could not be listed" in out["text"] and host.calls == []


def test_the_brief_route_serves_only_the_box_running_a_lean_turn(w, client):
    from django.utils import timezone

    runner = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    Turn.objects.filter(pk=w["owner_turn"].pk).update(status=Turn.RUNNING, claimed_by=runner,
                                                      claimed_at=timezone.now())
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="runner")
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    url = f"/api/harness/turns/{w['owner_turn'].pk}/page-brief"
    with mock.patch.object(page_brief, "build", return_value={
            "text": "<page_context/>", "site": "connect-labs", "tools": 3, "refused": ""}):
        r = client.get(url, **auth)
    assert r.status_code == 200 and r.json()["text"] == "<page_context/>"
    assert Turn.objects.get(pk=w["owner_turn"].pk).events.filter(kind="lifecycle").exists()
    declare(w["agent"], {"connect": {**LEAN["connect"], "page_lean": False}})
    r = client.get(url, **auth)
    assert r.status_code == 200 and r.json()["refused"] == "not_a_lean_page_turn"
    stranger, _ = PersonalToken.create_for_user(user=w["visitor"], label="x")
    assert client.get(url, HTTP_AUTHORIZATION=f"Bearer {stranger}").status_code == 404


# --- the reply being written ----------------------------------------------------------------


def test_live_text_goes_to_the_conversations_socket_and_is_never_stored(w, client):
    from django.utils import timezone

    runner = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    Turn.objects.filter(pk=w["owner_turn"].pk).update(status=Turn.RUNNING, claimed_by=runner,
                                                      claimed_at=timezone.now())
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="runner")
    with mock.patch("apps.realtime.groups.publish") as publish:
        r = client.post(f"/api/harness/turns/{w['owner_turn'].pk}/live-text",
                        {"message": 0, "text": "EHA is furthest"}, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200 and r.json() == {"sent": True}
    (group, frame), _ = publish.call_args
    assert frame == {"type": "chat.live_text", "turn_id": str(w["owner_turn"].pk),
                     "message": 0, "text": "EHA is furthest"}
    assert not Turn.objects.get(pk=w["owner_turn"].pk).events.exists()


def test_a_sensitive_conversations_words_never_stream(w, client):
    from django.utils import timezone

    runner = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    Turn.objects.filter(pk=w["owner_turn"].pk).update(status=Turn.RUNNING, claimed_by=runner,
                                                      claimed_at=timezone.now())
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="runner")
    with mock.patch("apps.harness.sensitivity.is_sensitive", return_value=True), \
            mock.patch("apps.realtime.groups.publish") as publish:
        r = client.post(f"/api/harness/turns/{w['owner_turn'].pk}/live-text",
                        {"message": 0, "text": "secret"}, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.json() == {"sent": False} and not publish.called


def test_live_text_rides_ag_ui_as_a_canopy_custom_event():
    [event] = agui.project({"event": "chat.live_text",
                            "data": {"message_id": "live:t:0", "turn_id": "t", "text": "Hi"}},
                           thread_id="s")
    out = agui.encode(event)
    assert out["type"] == "CUSTOM" and out["name"] == "canopy.chat.live_text"
    assert out["value"]["text"] == "Hi"


# --- preparing the runner ----------------------------------------------------------------------


def test_opening_a_lean_page_conversation_rings_its_cloud_runners_once(w):
    from django.core.cache import cache

    cache.clear()
    cloud = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    RunnerAssignment.objects.create(agent=w["agent"], runner=cloud, rank=0)
    with mock.patch("apps.realtime.groups.publish") as publish:
        assert prepare.ring(w["chat"]) == 1
        assert prepare.ring(w["chat"]) == 0  # at most every PREPARE_EVERY
    (group, frame), _ = publish.call_args
    assert frame == {"type": "runner.prepare", "agent": "ace", "session_id": str(w["chat"].pk)}


def test_a_conversation_that_is_not_lean_rings_nobody(w):
    from django.core.cache import cache

    cache.clear()
    declare(w["agent"], {"connect": {**LEAN["connect"], "page_lean": False}})
    w["agent"].refresh_from_db()
    chat = Session.objects.get(pk=w["chat"].pk)
    with mock.patch("apps.realtime.groups.publish") as publish:
        assert prepare.ring(chat) == 0
    assert not publish.called


def test_lean_page_turn_is_false_off_a_page(w):
    chat = Session.objects.create(workspace=w["ws"], agent=w["agent"])
    turn = Turn.objects.create(chat_session=chat, prompt="x", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=str(uuid.uuid4()))
    assert published_profiles.lean_page_turn(turn) is False
    assert published_profiles.lean_page_turn(w["owner_turn"]) is True
