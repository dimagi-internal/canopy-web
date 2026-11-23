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
    # Every tool keeps its whole description and its arguments' own descriptions: that is
    # where a site says what format an argument takes.
    assert "x" * 400 in out["text"] and "y" * 400 in out["text"]
    assert '"required":["sql"]' in out["text"] and "which org" in out["text"]
    # The page's own backing tool comes first.
    assert out["text"].index("marketplace_orgs_get:") < out["text"].index("data_query:")
    # And how to call them from this page: its scope and its ids, as given.
    assert page_brief.SCOPE_RULE in out["text"] and "filters.drilled" in out["text"]
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


# --- the panel opening, before anything is asked ---------------------------------------


def test_opening_the_panel_rings_the_lean_agents_runners_by_agent_only(w, client):
    from django.core.cache import cache

    from tests.token_helpers import delegated_headers

    cache.clear()
    cloud = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    RunnerAssignment.objects.create(agent=w["agent"], runner=cloud, rank=0)
    with mock.patch("apps.realtime.groups.publish") as publish:
        r = client.post("/api/embed/prepare", {"agent_slug": "ace"}, content_type="application/json",
                        **delegated_headers(w["app"], w["owner"]))
        again = client.post("/api/embed/prepare", {"agent_slug": "ace"}, content_type="application/json",
                            **delegated_headers(w["app"], w["owner"]))
    assert r.status_code == 200 and r.json() == {"rung": True}
    assert again.json() == {"rung": False}  # at most every PREPARE_EVERY
    (group, frame), _ = publish.call_args
    # Names the agent only: there is no conversation, and nothing is started.
    assert frame == {"type": "runner.prepare", "agent": "ace", "session_id": ""}
    assert not Session.objects.exclude(pk=w["chat"].pk).exclude(pk=w["session"].pk).exists()
    assert Turn.objects.count() == 2


def test_an_agent_the_site_does_not_offer_is_not_found(w, client):
    from tests.token_helpers import delegated_headers

    r = client.post("/api/embed/prepare", {"agent_slug": "nobody"}, content_type="application/json",
                    **delegated_headers(w["app"], w["owner"]))
    assert r.status_code == 404
    assert client.post("/api/embed/prepare", {"agent_slug": "ace"},
                       content_type="application/json").status_code in (401, 403, 404)



def test_a_long_tool_list_shortens_tool_descriptions_never_argument_formats():
    tools = [{"name": f"t{n}", "description": "Does a thing. " + "z" * 5000,
              "input_schema": {"type": "object", "properties": {
                  "worker_keys": {"type": "array", "description": "<opportunity_id>::<username>"}}}}
             for n in range(30)]
    lines = page_brief.tools_text(tools, backing="t7")
    text = "\n".join(lines)
    assert lines[0].startswith("- t7:") and "z" * 5000 in lines[0]  # the backing tool, whole
    assert text.count("<opportunity_id>::<username>") >= 20
    assert "z" * 5000 not in "\n".join(lines[1:])


def test_the_agents_own_page_guidance_rides_the_brief(w, host):
    from apps.host_grants import host_gateway

    declare(w["agent"], {"connect": {**LEAN["connect"],
                                     "page_guidance": "Call every workflow tool with the page's scope."}})
    with mock.patch.object(host_gateway, "resolve", side_effect=host_gateway.GatewayRefusal("no", "no grant")):
        out = page_brief.build(Turn.objects.get(pk=w["owner_turn"].pk))
    assert '<agent_guidance for="connect-labs">' in out["text"]
    assert "Call every workflow tool with the page's scope." in out["text"]


def test_page_guidance_is_short_text_with_sites():
    with pytest.raises(InterfaceError, match="at most 4000"):
        parse({"capabilities": {"x": {"sites": ["s"], "page_guidance": "a" * 4001}}})
    with pytest.raises(InterfaceError, match="needs `sites:`"):
        parse({"capabilities": {"x": {"page_guidance": "hi"}}})
    out = parse({"capabilities": {"x": {"sites": ["s"], "page_guidance": "  hi  "}}})["capabilities"]["x"]
    assert out["page_guidance"] == "hi"
    assert "page_guidance" not in parse({"capabilities": {"x": {"sites": ["s"]}}})["capabilities"]["x"]


def test_the_settings_write_and_clear_page_guidance(w):
    profile_settings.set_site(w["agent"], "connect-labs", {"page_guidance": "Use visible_ids."}, by=w["owner"])
    w["agent"].refresh_from_db()
    row = next(r for r in profile_settings.host_sites_of(w["agent"]) if r["site"] == "connect-labs")
    assert row["page_guidance"] == "Use visible_ids."
    profile_settings.set_site(w["agent"], "connect-labs", {"page_guidance": ""}, by=w["owner"])
    w["agent"].refresh_from_db()
    row = next(r for r in profile_settings.host_sites_of(w["agent"]) if r["site"] == "connect-labs")
    assert row["page_guidance"] == ""


# --- a page permission that waits for the person's passkey is waited for -------------------


@pytest.fixture()
def waiting(w):
    """The owner's page permission, there and still waiting for their passkey."""
    from django.core.cache import cache

    cache.clear()
    grant = sg.grant(w, subject=f"sub-{w['owner'].pk}", contact=None, user=w["owner"],
                     confirmed_at=None)
    yield grant
    cache.clear()


def _two_tools():
    async def list_tools(ctx):
        return [{"name": "marketplace_orgs_get", "description": "List orgs.",
                 "input_schema": {"type": "object", "properties": {}}}]
    return list_tools


def test_the_brief_waits_while_the_person_is_asked_then_lists_once_they_confirm(w, waiting):
    from apps.host_grants import host_gateway, host_grants

    turn = Turn.objects.get(pk=w["owner_turn"].pk)
    with mock.patch("apps.realtime.groups.publish") as publish, \
            mock.patch.object(host_gateway, "list_tools", _two_tools()):
        first = page_brief.build(turn)
        # Waiting, not refused: no text yet, the panel asked, and the note is up.
        assert first["waiting"] is True and first["text"] == "" and first["wait_left"] > 100
        waiting.refresh_from_db()
        assert waiting.requested_at is not None
        frames = [c.args[1] for c in publish.call_args_list]
        assert {"type": "host_grant.requested", "grant_id": waiting.pk,
                "user_id": waiting.user_id} in frames
        assert any(f.get("type") == "chat.live_text" and f["text"] == host_gateway.WAIT_NOTE
                   for f in frames)
        # Asking again never asks the person twice.
        publish.reset_mock()
        assert page_brief.build(turn, poll=True)["waiting"] is True
        assert not [c for c in publish.call_args_list if c.args[1]["type"] == "host_grant.requested"]
        # They confirm: the next look lists the tools.
        host_grants.confirm(waiting)
        out = page_brief.build(turn, poll=True)
    assert out["waiting"] is False and out["refused"] == "" and out["tools"] == 1
    assert "marketplace_orgs_get" in out["text"]
    assert publish.call_args_list[-1].args[1]["text"] == host_gateway.WAIT_NOTE_CONFIRMED


def test_once_the_wait_is_over_the_brief_says_approval_was_not_given(w, waiting):
    from apps.host_grants import host_gateway

    turn = Turn.objects.get(pk=w["owner_turn"].pk)
    with mock.patch("apps.realtime.groups.publish"):
        assert page_brief.build(turn)["waiting"] is True
        with mock.patch.object(host_gateway, "GRANT_WAIT_SECONDS", 0):
            out = page_brief.build(turn, poll=True)
    assert out["waiting"] is False and out["tools"] == 0
    assert out["refused"] == host_gateway.page_grant_not_given()
    assert "approval was not given" in out["text"] and "ask again" in out["text"]


def test_a_stopped_or_expired_permission_is_never_waited_for(w):
    from django.utils import timezone

    sg.grant(w, subject=f"sub-{w['owner'].pk}", contact=None, user=w["owner"],
             revoked_at=timezone.now())
    out = page_brief.build(Turn.objects.get(pk=w["owner_turn"].pk))
    assert not out.get("waiting") and out["refused"] and "could not be listed" in out["text"]


def test_the_brief_route_hands_back_waiting_and_logs_only_the_outcome(w, client):
    from django.utils import timezone

    runner = Runner.objects.create(name="cloud", owner=w["owner"], kind=Runner.CLOUD)
    Turn.objects.filter(pk=w["owner_turn"].pk).update(status=Turn.RUNNING, claimed_by=runner,
                                                      claimed_at=timezone.now())
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="runner")
    url = f"/api/harness/turns/{w['owner_turn'].pk}/page-brief"
    with mock.patch.object(page_brief, "build", return_value={
            "text": "", "site": "connect-labs", "tools": 0, "refused": "unconfirmed",
            "waiting": True, "wait_left": 118.0}) as build:
        r = client.get(url + "?poll=true", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200 and r.json()["waiting"] is True and r.json()["wait_left"] == 118.0
    assert build.call_args.kwargs == {"poll": True}
    assert not Turn.objects.get(pk=w["owner_turn"].pk).events.exists()


def _resolve_waiting(turn_id, **kw):
    from apps.host_grants import host_gateway

    async def no_sleep(_s):
        return None
    return async_to_sync(host_gateway.resolve_waiting)(turn_id, sleep=no_sleep, **kw)


def test_a_tool_call_waits_for_the_passkey_and_goes_on_once_confirmed(w, waiting):
    """The same wait for a turn that is not lean: its `site_tools` / `site_call` hold
    while the person is asked, and go through the moment they confirm."""
    from apps.host_grants import host_gateway, host_grants

    turn_id = str(w["owner_turn"].pk)
    looks = []
    real = host_gateway.resolve

    def resolve(tid, **kw):
        looks.append(kw.get("ask_person", True))
        if len(looks) == 3:
            host_grants.confirm(waiting)
        return real(tid, **kw)

    with mock.patch("apps.realtime.groups.publish"), \
            mock.patch.object(host_gateway, "resolve", resolve):
        ctx = _resolve_waiting(turn_id, call_wait=60)
    assert ctx.scope == waiting.scope
    # Asked once, then looked again without asking.
    assert looks == [True, False, False]


def test_a_tool_calls_wait_is_bounded_and_then_says_approval_was_not_given(w, waiting):
    from apps.host_grants import host_gateway

    turn_id = str(w["owner_turn"].pk)
    with mock.patch("apps.realtime.groups.publish"):
        # One call holds its request open only so long, then says to call again.
        with pytest.raises(host_gateway.GatewayRefusal) as held:
            _resolve_waiting(turn_id, call_wait=4)
        assert held.value.waiting and "Call this again now" in held.value.message
        # The turn's clock runs out: approval was not given.
        with mock.patch.object(host_gateway, "GRANT_WAIT_SECONDS", 0), \
                pytest.raises(host_gateway.GatewayRefusal) as out:
            _resolve_waiting(turn_id, call_wait=60)
    assert not out.value.waiting and out.value.message == host_gateway.page_grant_not_given()


def test_a_refusal_that_is_not_waiting_is_never_held(w):
    from apps.host_grants import host_gateway

    with pytest.raises(host_gateway.GatewayRefusal) as out:
        _resolve_waiting(str(w["owner_turn"].pk))
    assert not out.value.waiting and out.value.code == "no_grant"
