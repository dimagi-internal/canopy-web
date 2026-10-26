"""Host-held results (spec 2026-10-23-host-held-results-design): a host's tool returns
facts plus a `resource_link` to an artifact the HOST keeps; canopy carries the link,
never the data; the person's View and the agent read it back from the host, as
themselves.

Driven end to end against a real FastMCP host that keeps its own results with standard
MCP (`tests.fixtures.host_results`: a small store, `resources/read` on its scheme, the
host's ACL hook) — keeping results is the host's business, not the SDK's — reached
in-process through the gateway — so what canopy keeps, what a View receives and
what `site_read` returns are what a real Labs would produce. The canary at the end
checks that nothing canopy stores for a sensitive conversation holds a row.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx2
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from canopy_sdk import contract, resources
from canopy_sdk import site as site_doc
from canopy_sdk.host import ViewRefused, verify_view
from canopy_sdk.keys import generate_private_key, public_pem
from canopy_sdk.stores import MemoryJtiStore
from canopy_transcript import conversational_messages
from canopy_transcript.rows import TOOL_TEXT_MAX
from django.contrib.auth.models import User
from django.test import Client
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools import ToolResult
from mcp.types import ResourceLink, TextContent

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions import agui
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import AppViewSubscription, Message, Session, SessionParticipant
from apps.common.encryption import encrypt_secret
from apps.harness import acting_person, caller_tokens, ledger, sensitivity, turn_asker
from apps.harness.models import Runner, RunnerFlag, Turn, TurnAgentToken, TurnEvent
from apps.host_grants import client_identity, host_gateway, resources_updated, site_needs
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.mcp_apps_host import mcp_apps, mcp_apps_views
from apps.realtime.groups import session_group
from apps.tokens.models import HostGrant, PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import site_gateway as sg
from tests.fixtures.host_results import META_KEY, ResultStore, facts
from tests.fixtures.sensitive import vault_key

pytestmark = pytest.mark.django_db

VIEW = "ui://labs/data-result"
CALL = "toolu_01RESULTS"
#: A row the host returns. It must reach the person and the agent, and never rest in canopy.
SENTINEL = "AMINA-CANARY-7731"
ROWS = [[SENTINEL, 12], ["Bisi", 3]]
SUBJECTS = {sg.HOST_TOKEN: "u-42", "grant-owner": "sub-owner", "grant-eddie": "sub-eddie",
            "grant-vera": "sub-vera"}
SITE_TOOL = "mcp__plugin_canopy_canopy-web__site_call"
#: A realistic result: Labs' facts for 40 rows of 8 columns, 20 sampled, are over 8 KB.
BIG_COLUMNS = ["worker", "visits", "district", "village", "form_id", "submitted", "status", "notes"]
BIG_ROWS = [[SENTINEL if i == 0 else f"worker-{i:03d}", i, "Kano Municipal", f"Village {i}",
             f"form-{i:05d}-{'x' * 40}", "2026-10-01T12:00:00Z", "approved", "n" * 300]
            for i in range(40)]


#: sg.IFACE with no owner's ceiling: the host's grant decides what the visitor reaches.
IFACE = {"capabilities": {**sg.IFACE["capabilities"],
                          "connect": {k: v for k, v in sg.IFACE["capabilities"]["connect"].items()
                                      if k != "ceiling"}}}


def may_read(reader, artifact):
    """The host's own ACL: a colleague who may see the underlying data."""
    return reader == "sub-eddie"


def caller_subject():
    """Who the host's MCP request acts for, from its bearer (the fake host's verifier)."""
    auth = (get_http_headers(include_all=True) or {}).get("authorization", "")
    return SUBJECTS.get(auth.partition(" ")[2])


class ResultsHost:
    """A Labs in miniature, keeping its own results: `data_query` returns facts and a
    `labs-result://` link (`store.save`); `resources/read` on the scheme answers as the
    reader (`store.mount`); the View is `ui://labs/data-result`."""

    def __init__(self, store: ResultStore):
        self.seen: list[dict] = []
        self.store = store
        server = FastMCP("labs")

        @server.tool(meta={"ui": {"resourceUri": VIEW, "visibility": ["model", "app"]}})
        async def data_query(sql: str):
            link = store.save("query", {"columns": ["name", "visits"], "rows": ROWS, "sql": sql},
                              owner=caller_subject(), name="Visits by worker", description="2 rows")
            told = facts(["name", "visits"], ROWS, sample=1)
            return [TextContent(type="text", text=json.dumps(told)), ResourceLink.model_validate(link)]

        @server.tool(meta={"ui": {"resourceUri": VIEW, "visibility": ["model", "app"]}})
        async def big_query(sql: str):
            """Labs' real shape: facts over 8 KB (20 sample rows of 8 columns), as text AND
            as structuredContent, then the link."""
            link = store.save("query", {"columns": BIG_COLUMNS, "rows": BIG_ROWS, "sql": sql},
                              owner=caller_subject(), name="Visits by worker", description="40 rows")
            told = {**facts(BIG_COLUMNS, BIG_ROWS, sample=20), "sql": sql}
            return ToolResult(content=[TextContent(type="text", text=json.dumps(told)),
                                       ResourceLink.model_validate(link)], structured_content=told)

        @server.resource(VIEW, mime_type="text/html;profile=mcp-app")
        def view() -> str:
            return "<!doctype html><html><body>results</body></html>"

        store.mount(server, caller_subject)
        self.inner = server.http_app(path="/mcp/", stateless_http=True, json_response=True)

    async def app(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self.seen.append(headers)
            scheme, _, token = headers.get("authorization", "").partition(" ")
            if scheme != "DPoP" or token not in SUBJECTS or not headers.get("dpop"):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"bad authorization"})
                return
        await self.inner(scope, receive, send)

    def lifespan(self):
        return self.inner.router.lifespan_context(self.inner)


@pytest.fixture()
def host(monkeypatch):
    h = ResultsHost(ResultStore("labs-result", may_read=may_read))
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=h.app))
    return h


@pytest.fixture(autouse=True)
def declared(monkeypatch):
    """The site's signed site.json declares `labs-result` (read through `needs_for`)."""
    doc = site_doc.SiteNeeds(site_name="connect-labs", issued_at=0,
                             site=site_doc.SiteFacts(resource_schemes=("labs-result",)))
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", doc))


def through(host, fn):
    async def main():
        async with host.lifespan():
            return await sync_to_async(fn, thread_sensitive=True)()
    return async_to_sync(main)()


def _member(ws, name):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw", first_name=name.title())
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return u


def _grant(app, token, user):
    from datetime import timedelta

    from django.utils import timezone

    return sg.acted_with(HostGrant.objects.create(
        app=app, subject=token, user=user, access_token_enc=encrypt_secret(token), scope="data:read",
        resource=sg.RESOURCE, dpop_jkt=client_identity.dpop_jkt(),
        expires_at=timezone.now() + timedelta(minutes=10)))


@pytest.fixture()
def w():
    """sg.world (a contact visitor's conversation, for `site_read`) plus a member's own
    conversation on the same site with two colleagues, each with their own grant."""
    w = sg.world()
    w["agent"].interface = parse(IFACE)
    w["agent"].save(update_fields=["interface"])
    w["eddie"], w["vera"] = _member(w["ws"], "eddie"), _member(w["ws"], "vera")
    w["mine"] = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["owner"],
                                       metadata={"embed_app": "connect-labs"})
    for u in (w["eddie"], w["vera"]):
        SessionParticipant.objects.create(session=w["mine"], user=u, role=SessionParticipant.EDITOR)
    w["grants"] = {"owner": _grant(w["app"], "grant-owner", w["owner"]),
                   "eddie": _grant(w["app"], "grant-eddie", w["eddie"]),
                   "vera": _grant(w["app"], "grant-vera", w["vera"])}
    mcp_apps.record_tools(w["app"].pk, [(t, {"ui": {"resourceUri": VIEW, "visibility": ["model", "app"]}})
                                        for t in ("data_query", "big_query")])
    w["site_key"] = generate_private_key("EdDSA")
    w["app"].public_keys = [public_pem(w["site_key"])]
    w["app"].save(update_fields=["public_keys"])
    return w


def _answer(w, host, tool="data_query") -> dict:
    """`site_call`'s answer for `tool`, as the gateway returns it to the agent."""
    ctx = host_gateway.context_for_grant(w["app"], w["grants"]["owner"], turn_id="", agent_slug="ace",
                                         ceiling=[])

    async def main():
        async with host.lifespan():
            return await host_gateway.call_tool(ctx, tool, {"sql": "select * from visits"})
    return async_to_sync(main)()


def _converse(w, host, *, sensitive: bool) -> dict:
    """The agent called `site_call` → `data_query` in `mine`; its rows land as a runner ships them."""
    session = w["mine"]
    if sensitive:
        from apps.canopy_sessions import services as chat_services

        vault_key(w["owner"])
        chat_services.add_runner_requirements(session, ["sensitive_data"])
    answer = _answer(w, host)
    chat_transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": "how many visits?"},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"type": "tool_use", "id": CALL, "name": SITE_TOOL,
                     "input": {"tool": "data_query", "arguments": {"sql": "select * from visits"}}}},
        {"index": 65, "role": "tool_result", "text": json.dumps(answer),
         "content": {"tool_use_id": CALL, "is_error": False}},
        {"index": 128, "role": "assistant", "text": "12 visits"},
    ])
    return answer


def _link(answer) -> dict:
    [link] = [b for b in answer.get("links", []) + answer["content"] if b.get("type") == "resource_link"]
    return link


def _kept(link) -> dict:
    """A link as canopy keeps it: its address only (`canopy_sdk.resources.validate_link`)."""
    return {k: link[k] for k in ("type", "uri", "name", "mimeType")}


def _sealed_kept(link) -> dict:
    """...and in a sensitive conversation, with the scheme for a name: no host words."""
    return {**_kept(link), "name": "labs-result"}


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def base(w):
    return f"/api/canopy-sessions/{w['mine'].pk}/apps/{CALL}"


def post(c, url, body, **extra):
    return c.post(url, data=json.dumps(body), content_type="application/json", **extra)


# --- what the host returns, through the gateway -----------------------------------------


def test_the_gateway_keeps_a_links_wire_names(w, host):
    answer = _answer(w, host)
    link = _link(answer)
    assert link["uri"].startswith("labs-result://") and link["mimeType"] == "application/json"
    assert link["name"] == "Visits by worker"
    assert answer["links"] == [link] and list(answer)[:4] == ["site", "tool", "is_error", "links"]
    facts = json.loads(answer["content"][0]["text"])
    assert facts["row_count"] == 2 and facts["columns"] == ["name", "visits"]


def test_site_call_puts_the_link_first_and_carries_the_facts_once(w, host):
    answer = _answer(w, host, "big_query")
    assert answer["structured"]["row_count"] == 40
    # MCP's text copy of `structured` is left out: the agent reads `structured`.
    assert answer["content"] == [] and len(answer["links"]) == 1
    text = json.dumps(answer)
    assert text.index("labs-result://") < 300 < TOOL_TEXT_MAX < len(text)


# --- strip_result keeps declared links, drops everything else ---------------------------


def _bad_links():
    return [{"type": "resource_link", "uri": "other-scheme://x", "name": "n", "mimeType": "a/b"},
            {"type": "resource_link", "uri": "https://evil.test/rows", "name": "n", "mimeType": "a/b"},
            {"type": "resource_link", "uri": "labs-result://" + "x" * 3000, "name": "n", "mimeType": "a/b"},
            {"type": "resource", "resource": {"uri": "labs-result://y", "text": SENTINEL}}]


def test_strip_result_keeps_declared_links_and_drops_the_rest(w):
    good = {"type": "resource_link", "uri": "labs-result://abc", "name": "r", "mimeType": "application/json",
            "_meta": {"rows": ROWS}}
    answer = {"site": "connect-labs", "tool": "data_query", "is_error": False, "structured": {"rows": ROWS},
              "content": [{"type": "text", "text": SENTINEL}, good, *_bad_links()]}
    for payload, text in (({"tool_use_id": CALL, "content": json.dumps(answer)}, ""),   # a gateway answer
                          ({"tool_use_id": CALL, "is_error": False}, json.dumps(answer)),  # in plaintext
                          ({"tool_use_id": CALL, "content": answer["content"]}, "")):     # path D blocks
        out = sensitivity.strip_result(payload, session=w["mine"], text=text, tool=SITE_TOOL)
        assert out["resource_links"] == [{"type": "resource_link", "uri": "labs-result://abc",
                                          "name": "labs-result", "mimeType": "application/json"}]
        assert out["withheld"] is True and out["tool_use_id"] == CALL
        assert SENTINEL not in json.dumps(out)


def test_strip_result_keeps_only_what_it_names(w):
    payload = {"tool_use_id": CALL, "is_error": False, "status": "complete", "stdout": SENTINEL,
               "summary": {"rows": ROWS}, "preview": SENTINEL, "resource_links": "not a list"}
    out = sensitivity.strip_result(payload, session=w["mine"])
    assert out == {"tool_use_id": CALL, "is_error": False, "status": "complete", "withheld": True}


def test_a_link_canopy_keeps_carries_no_description_and_fits_its_bounds(w):
    long_uri = {"type": "resource_link", "uri": "labs-result://" + "x" * 300, "name": "r", "mimeType": "a/b"}
    good = {"type": "resource_link", "uri": "labs-result://abc", "name": "Amina's visits",
            "mimeType": "application/json", "description": f"rows: {SENTINEL}"}
    many = [{**good, "uri": f"labs-result://{i}"} for i in range(25)]
    out = sensitivity.strip_result({"tool_use_id": CALL, "resource_links": [long_uri, good, *many]},
                                   session=w["mine"], tool=SITE_TOOL)
    assert len(out["resource_links"]) == 10 and out["resource_links"][0]["uri"] == "labs-result://abc"
    assert SENTINEL not in json.dumps(out) and "Amina" not in json.dumps(out)


#: What a prompt-injected agent could print from any tool to smuggle a sealed result out
#: in plaintext: link-shaped JSON, ten links of a 256-character URI and a long mimeType.
SMUGGLED = json.dumps({"content": [
    {"type": "resource_link", "uri": "labs-result://" + (SENTINEL + "-") * 13, "name": "r",
     "mimeType": "text/" + SENTINEL.lower() + "-x" * 40} for _ in range(10)] + [
    {"type": "resource_link", "uri": f"labs-result://{SENTINEL}-{i}", "name": "r",
     "mimeType": "application/json"} for i in range(10)]})


def test_only_the_sites_own_tools_keep_links_in_a_sensitive_conversation(w, host):
    """A link is the site's address only when the site's tool returned it. The same JSON
    printed by a shell, a file read or any other tool is the agent's words, and goes."""
    from apps.canopy_sessions import services as chat_services

    vault_key(w["owner"])
    chat_services.add_runner_requirements(w["mine"], ["sensitive_data"])
    rows = []
    for i, tool in enumerate(("Bash", "Read", "mcp__other__not_the_sites", "mcp__evil__site_call", "Grep")):
        call = f"toolu_smuggle_{i}"
        rows += [{"index": 10 * i + 1, "role": "tool_use", "text": "",
                  "content": {"type": "tool_use", "id": call, "name": tool, "input": {}}},
                 {"index": 10 * i + 2, "role": "tool_result", "text": SMUGGLED,
                  "content": {"tool_use_id": call, "is_error": False}}]
    chat_transcript_ingest.persist_transcript_rows(w["mine"], rows)
    stored = list(Message.objects.filter(session=w["mine"], role=Message.TOOL_RESULT))
    assert len(stored) == 5
    for m in stored:
        assert "resource_links" not in m.content and SENTINEL not in json.dumps(m.content), m.content
        assert SENTINEL not in m.plaintext and m.sealed is None
    # ...and a result whose call was stored by an earlier batch is placed from the store.
    payload = {"tool_use_id": "toolu_smuggle_0", "content": SMUGGLED}
    assert "resource_links" not in sensitivity.strip_result(payload, session=w["mine"])


def test_the_sites_own_links_are_kept_only_in_their_declared_shape(w, monkeypatch):
    doc = site_doc.SiteNeeds(site_name="connect-labs", issued_at=0, site=site_doc.SiteFacts(
        resource_schemes=("labs-result",), resource_id_lengths=(("labs-result", 24),)))
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", doc))
    rid = "A1b2C3d4E5f6G7h8I9j0K_-z"
    payload = {"tool_use_id": CALL, "content": SMUGGLED + json.dumps([
        {"type": "resource_link", "uri": f"labs-result://{rid}", "name": "r", "mimeType": "application/json"},
        {"type": "resource_link", "uri": f"labs-result://{rid}x", "name": "r", "mimeType": "application/json"},
        {"type": "resource_link", "uri": f"labs-result://{rid}", "name": "r", "mimeType": "text/amina"}])}
    out = sensitivity.strip_result({**payload, "content": json.loads(SMUGGLED)["content"] + json.loads(
        payload["content"][len(SMUGGLED):])}, session=w["mine"], tool=SITE_TOOL)
    assert out["resource_links"] == [{"type": "resource_link", "uri": f"labs-result://{rid}",
                                      "name": "labs-result", "mimeType": "application/json"}]
    assert SENTINEL not in json.dumps(out)


def test_a_stripped_results_status_and_ids_are_not_free_text(w):
    out = sensitivity.strip_result({"tool_use_id": CALL, "status": f"completed: {SENTINEL}",
                                    "id": f"{SENTINEL} visits 12", "name": "x" * 300}, session=w["mine"])
    assert out == {"tool_use_id": CALL, "withheld": True}
    assert sensitivity.strip_result({"tool_use_id": CALL, "status": "failed"})["status"] == "failed"


def test_without_a_declared_scheme_or_a_site_no_link_is_kept(w, monkeypatch):
    payload = {"tool_use_id": CALL, "content": [{"type": "resource_link", "uri": "labs-result://abc",
                                                 "name": "r", "mimeType": "a/b"}]}
    assert "resource_links" not in sensitivity.strip_result(payload)                    # no session
    off_site = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["owner"])
    assert "resource_links" not in sensitivity.strip_result(payload, session=off_site)  # no site
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", None, reason="down"))
    assert "resource_links" not in sensitivity.strip_result(payload, session=w["mine"])  # unreadable
    # A payload cannot smuggle its own pre-made list past the check.
    forged = {"tool_use_id": CALL, "resource_links": [{"uri": "https://evil.test"}]}
    assert "resource_links" not in sensitivity.strip_result(forged, session=w["mine"])


def test_the_stored_row_and_the_ledger_list_the_links_not_the_data(w, host):
    answer = _converse(w, host, sensitive=True)
    link = _link(answer)
    row = Message.objects.get(session=w["mine"], role=Message.TOOL_RESULT)
    assert row.content["resource_links"] == [_sealed_kept(link)] and row.plaintext == ""
    assert row.sealed is None
    assert SENTINEL not in json.dumps(row.content)
    turn = Turn.objects.create(chat_session=w["mine"], prompt="q", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=uuid.uuid4().hex)
    ledger.append_events(turn, [{"kind": "tool_end", "payload": {"tool_use_id": CALL, "is_error": False,
                                                                 "output": json.dumps(answer)}}])
    event = TurnEvent.objects.get(turn=turn)
    assert event.payload["resource_links"] == [_sealed_kept(link)]
    assert event.payload["redacted"] == "sensitive"
    assert SENTINEL not in json.dumps(event.payload)


def test_a_runners_events_keep_the_link_only_when_the_call_in_the_batch_is_the_sites(w, host):
    answer = _converse(w, host, sensitive=True)
    link = _link(answer)
    turn = Turn.objects.create(chat_session=w["mine"], prompt="q", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=uuid.uuid4().hex)
    for tool, kept in ((SITE_TOOL, [_sealed_kept(link)]), ("Bash", None)):
        call = f"toolu_ev_{tool[-4:]}"
        ledger.append_events(turn, [
            {"kind": "tool_start", "payload": {"id": call, "name": tool, "input": {}}},
            {"kind": "tool_end", "payload": {"tool_use_id": call, "is_error": False,
                                             "content": json.dumps(answer)}}])
        end = TurnEvent.objects.get(turn=turn, kind="tool_end", payload__tool_use_id=call)
        assert end.payload.get("resource_links") == kept, tool
        assert SENTINEL not in json.dumps(end.payload)


# --- the View gets the links, and reads the rows as its viewer ---------------------------


def test_a_sensitive_view_gets_the_links_only_and_reads_the_rows_as_its_viewer(w, host):
    link = _link(_converse(w, host, sensitive=True))
    r = through(host, lambda: client_for(w["owner"]).get(base(w) + "/resource"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["tool_result"] == {"content": [_sealed_kept(link)], "structuredContent": None,
                                   "_meta": {"canopy/withheld": True}, "isError": False}
    assert SENTINEL not in r.content.decode()
    # The owner reads the rows from the host, as themselves; a colleague the host's ACL
    # admits may too; another is refused by the host, not by canopy.
    for who in ("owner", "eddie"):
        r = through(host, lambda who=who: post(client_for(w[who]), base(w) + "/read", {"uri": link["uri"]}))
        assert r.status_code == 200, r.content
        [content] = r.json()["contents"]
        assert json.loads(content["text"])["rows"][0] == [SENTINEL, 12]
        assert content["_meta"][META_KEY]["kind"] == "query"
    r = through(host, lambda: post(client_for(w["vera"]), base(w) + "/read", {"uri": link["uri"]}))
    assert r.status_code == 403 and "forbidden" in r.json()["detail"]


def test_an_ordinary_view_gets_the_result_as_it_was_links_included(w, host):
    answer = _converse(w, host, sensitive=False)
    r = through(host, lambda: client_for(w["owner"]).get(base(w) + "/resource"))
    result = r.json()["tool_result"]
    assert result["content"] == answer["links"] + answer["content"] and _link(result) == _link(answer)
    assert "_meta" not in result


# --- a realistic result (over 8 KB) still reaches its card ---------------------------------
#
# Driven through the transcript reader both runners use (`canopy_transcript`), which cuts a
# result's text at TOOL_TEXT_MAX: the record is what Claude Code writes, the rows are what a
# runner ships, and the View reads what was stored.


def _record(tool_name: str, tool_input: dict, result_content) -> list[dict]:
    """Claude Code's transcript records for one tool call and its result."""
    return [
        {"type": "user", "message": {"content": "how many visits?"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": CALL, "name": tool_name, "input": tool_input}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": CALL, "content": result_content}]}},
    ]


def _ship(w, records, *, sensitive: bool):
    session = w["mine"]
    if sensitive:
        from apps.canopy_sessions import services as chat_services

        vault_key(w["owner"])
        chat_services.add_runner_requirements(session, ["sensitive_data"])
    rows = conversational_messages(records, -1)
    [result] = [r for r in rows if r["role"] == "tool_result"]
    assert len(result["text"]) < TOOL_TEXT_MAX + 100   # the runner cut it...
    chat_transcript_ingest.persist_transcript_rows(session, rows)


def _gateway_records(w, host):
    import pydantic_core

    answer = _answer(w, host, "big_query")
    # What FastMCP hands Claude Code for `site_call`'s dict: one JSON text block.
    text = pydantic_core.to_json(answer).decode()
    assert len(text) > TOOL_TEXT_MAX
    return answer, _record(SITE_TOOL, {"tool": "big_query", "arguments": {"sql": "x"}},
                           [{"type": "text", "text": text}])


def _direct_records(w, host):
    """Labs attached to Claude Code directly: its own blocks, the link after 12 KB of facts."""
    answer = _answer(w, host, "big_query")
    blocks = [{"type": "text", "text": json.dumps(answer["structured"])}, *answer["links"]]
    assert len(blocks[0]["text"]) > TOOL_TEXT_MAX
    return answer, _record("mcp__connect_labs__big_query", {"sql": "x"}, blocks)


def _card(w, host, viewer="owner"):
    r = through(host, lambda: client_for(w[viewer]).get(base(w) + "/resource"))
    assert r.status_code == 200, r.content
    return r.json()["tool_result"]


def _reads_rows(w, host, link):
    r = through(host, lambda: post(client_for(w["owner"]), base(w) + "/read",
                                   {"uri": link["uri"] + "?page=1&page_size=25"}))
    assert r.status_code == 200, r.content
    rows = json.loads(r.json()["contents"][0]["text"])["rows"]
    assert len(rows) == 25 and rows[0][0] == SENTINEL


@pytest.mark.parametrize("path", ["gateway", "direct"])
def test_an_ordinary_card_over_8kb_still_has_its_link(w, host, path):
    answer, records = (_gateway_records if path == "gateway" else _direct_records)(w, host)
    _ship(w, records, sensitive=False)
    stored = Message.objects.get(session=w["mine"], role=Message.TOOL_RESULT)
    assert stored.content["resource_links"] == [_kept(_link(answer))]
    result = _card(w, host)
    assert _link(result) == _kept(_link(answer))
    _reads_rows(w, host, _link(result))


@pytest.mark.parametrize("path", ["gateway", "direct"])
def test_a_sensitive_card_over_8kb_still_has_its_link(w, host, path):
    answer, records = (_gateway_records if path == "gateway" else _direct_records)(w, host)
    _ship(w, records, sensitive=True)
    stored = Message.objects.get(session=w["mine"], role=Message.TOOL_RESULT)
    assert stored.content["resource_links"] == [_sealed_kept(_link(answer))]
    assert SENTINEL not in json.dumps(stored.content) and stored.plaintext == ""
    result = _card(w, host)
    assert result["content"] == [_sealed_kept(_link(answer))] and result["_meta"] == {"canopy/withheld": True}
    _reads_rows(w, host, _link(result))


def test_a_direct_result_no_longer_glues_a_link_marker_onto_its_facts():
    from canopy_transcript.rows import _tool_result_text

    blocks = [{"type": "text", "text": '{"row_count": 2}'},
              {"type": "resource_link", "uri": "labs-result://a", "name": "r", "mimeType": "a/b"}]
    assert json.loads(_tool_result_text(blocks)) == {"row_count": 2}
    assert _tool_result_text(blocks[1:]) == "[resource_link]"   # a link alone still pairs


# --- the gateway refuses an answer bigger than its cap -----------------------------------


def test_the_gateway_refuses_an_answer_over_its_byte_cap(w, host, settings):
    ctx = host_gateway.context_for_grant(w["app"], w["grants"]["owner"], turn_id="", agent_slug="ace",
                                         ceiling=[])
    uri = host.store.save("query", {"rows": BIG_ROWS}, owner="sub-owner", name="r")["uri"]

    def outcome(call):
        async def main():
            async with host.lifespan():
                try:
                    return await call()
                except host_gateway.GatewayRefusal as exc:
                    return exc
        return async_to_sync(main)()

    settings.HOST_GATEWAY_MAX_RESPONSE_BYTES = 4_000
    # `read_resource` (the agent's `site_read`) reads through `view_read`.
    for call in (lambda: host_gateway.call_tool(ctx, "big_query", {"sql": "x"}),
                 lambda: host_gateway.view_read(ctx, uri)):
        refused = outcome(call)
        assert isinstance(refused, host_gateway.GatewayRefusal), refused
        assert refused.code == "too_large" and "smaller page_size" in refused.message
    settings.HOST_GATEWAY_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    assert outcome(lambda: host_gateway.view_read(ctx, uri))


def _gzipping(app, seen: list):
    """A host that gzips every answer, whatever canopy asked for: the bomb's delivery."""
    import gzip

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        seen.append(dict((k.decode().lower(), v.decode()) for k, v in scope["headers"]))
        start, body = {}, bytearray()

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
                return
            body.extend(message.get("body", b""))
            if message.get("more_body"):
                return
            packed = gzip.compress(bytes(body))
            headers = [(k, v) for k, v in start.get("headers", [])
                       if k.lower() not in (b"content-length", b"content-encoding")]
            headers += [(b"content-encoding", b"gzip"), (b"content-length", str(len(packed)).encode())]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": packed})

        await app(scope, receive, capture)
    return wrapped


def test_the_byte_cap_counts_what_canopy_would_hold_not_what_was_compressed(w, host, monkeypatch):
    """40 KB of gzip can be 40 MB decoded. canopy asks for no coding and refuses one it
    is sent anyway, so a bomb is never decoded, however small it arrives."""
    import gzip

    seen: list = []
    monkeypatch.setattr(host_gateway, "_transport_override",
                        httpx2.ASGITransport(app=_gzipping(host.app, seen)))
    ctx = host_gateway.context_for_grant(w["app"], w["grants"]["owner"], turn_id="", agent_slug="ace",
                                         ceiling=[])
    bomb = "0" * (5 * 1024 * 1024)                     # over the 4 MB cap decoded...
    assert len(gzip.compress(bomb.encode())) < 40_000   # ...and tiny on the wire
    uri = host.store.save("query", {"rows": [[bomb]]}, owner="sub-owner", name="r")["uri"]

    async def main(call):
        async with host.lifespan():
            try:
                return await call()
            except host_gateway.GatewayRefusal as exc:
                return exc
    for call in (lambda: host_gateway.view_read(ctx, uri),
                 lambda: host_gateway.call_tool(ctx, "data_query", {"sql": "x"})):
        refused = async_to_sync(main)(call)
        assert isinstance(refused, host_gateway.GatewayRefusal), refused
        assert refused.code == "bad_encoding" and "compressed" in refused.message
    assert seen and all(h.get("accept-encoding") == "identity" for h in seen)


def test_lapsed_subscriptions_are_swept_everywhere(w):
    from datetime import timedelta

    from django.utils import timezone

    from apps.common import ticks
    from apps.mcp_apps_host import mcp_apps_views

    now = timezone.now()
    AppViewSubscription.objects.create(session=w["mine"], app=w["app"], tool_call_id=CALL,
                                       uri="labs-result://old", expires_at=now - timedelta(minutes=1))
    AppViewSubscription.objects.create(session=w["mine"], app=w["app"], tool_call_id=CALL,
                                       uri="labs-result://live", expires_at=now + timedelta(hours=1))
    assert "mcp_apps.subscription_sweep" in ticks.jobs()
    assert mcp_apps_views.sweep_subscriptions() == 1
    assert list(AppViewSubscription.objects.values_list("uri", flat=True)) == ["labs-result://live"]


# --- site_read: the agent reads a result as the visitor ----------------------------------


def _visitor_call(w, host, name, args):
    with sg.visitor(w):
        return sg.call(host, name, args)


def test_site_read_reads_as_the_visitor_and_audits_only_the_uri(w, host):
    sg.grant(w)
    out = _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    uri = _link(out.structured_content)["uri"]
    read = _visitor_call(w, host, "site_read", {"uri": uri + "?page=1&page_size=1"}).structured_content
    assert read["site"] == "connect-labs" and read["uri"].startswith(uri)
    [content] = read["contents"]
    assert json.loads(content["text"])["rows"] == [[SENTINEL, 12]]
    assert host.store.read(uri, "u-42").owner == "u-42"   # the visitor's, at the host
    [audit] = MCPAuditLog.objects.filter(tool="site_read")
    assert uri in audit.args_summary and SENTINEL not in audit.args_summary and audit.ok


def test_site_read_of_a_result_this_conversation_was_not_given_never_reaches_the_host(w, host):
    sg.grant(w)
    theirs = host.store.save("query", {"rows": ROWS}, owner="sub-vera", name="r")["uri"]
    host.seen.clear()
    for uri in (theirs, "https://evil.test/rows", "other-scheme://x"):
        with sg.visitor(w), pytest.raises(Exception, match="results the site gave me|does not declare"):
            sg.call(host, "site_read", {"uri": uri})
    assert host.seen == []


def test_site_read_of_a_returned_result_the_host_refuses_is_its_refusal(w, host, monkeypatch):
    """A link this conversation was given, which the host's own ACL then refuses."""
    sg.grant(w)
    out = _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    uri = _link(out.structured_content)["uri"]
    monkeypatch.setattr(host.store, "read", lambda *a, **k: (_ for _ in ()).throw(Exception("forbidden")))
    with sg.visitor(w), pytest.raises(Exception):  # noqa: B017 - the host's refusal, whatever its words
        sg.call(host, "site_read", {"uri": uri})


def test_site_read_follows_the_owners_ceiling(w, host):
    """A result is read back only while the tool that produced it is inside the ceiling."""
    sg.grant(w)
    out = _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    uri = _link(out.structured_content)["uri"]
    caps = dict(IFACE["capabilities"])
    caps["connect"] = {**caps["connect"], "ceiling": ["mcp__*connect_labs__marketplace_*"]}
    w["agent"].interface = parse({"capabilities": caps})
    w["agent"].save(update_fields=["interface"])
    host.seen.clear()
    with sg.visitor(w), pytest.raises(Exception, match="may not use"):
        sg.call(host, "site_read", {"uri": uri})
    assert host.seen == []


def _sensitive(w):
    """A sensitive conversation on a sensitive-data box (test_execution_statement's rule)."""
    from django.utils import timezone

    caps = dict(IFACE["capabilities"])
    caps["connect"] = {**caps["connect"], "data": "sensitive"}
    w["agent"].interface = parse({"capabilities": caps})
    w["agent"].interface_published_by = w["owner"]
    w["agent"].save(update_fields=["interface", "interface_published_by"])
    box = Runner.objects.create(name="zdr", workspace=w["ws"], kind=Runner.CLOUD, status=Runner.ONLINE,
                                owner=w["owner"], last_heartbeat_at=timezone.now(),
                                capabilities={"data_mode": "sensitive_data"})
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=w["owner"])
    w["session"].metadata = {**w["session"].metadata, "runner_requirements": ["sensitive_data"]}
    w["session"].save(update_fields=["metadata"])
    Turn.objects.filter(pk=w["turn"].pk).update(claimed_by=box)
    return box


def test_site_read_is_a_tool_a_sensitive_capability_holds(w, host):
    _sensitive(w)
    sg.grant(w)
    out = _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    uri = _link(out.structured_content)["uri"]
    with sg.visitor(w):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"site_tools", "site_call", "site_read"} <= names
    read = _visitor_call(w, host, "site_read", {"uri": uri}).structured_content
    assert json.loads(read["contents"][0]["text"])["rows"][0][0] == SENTINEL
    # And the structural check of `data: sensitive` admits it by name.
    parse({"capabilities": {"c": {"callers": ["contact"], "sites": ["connect-labs"], "data": "sensitive",
                                  "tools": ["mcp__*canopy-web__site_read", "Read"]}}})


@pytest.mark.parametrize("breach", ["undeclared", "runner", "no_grant"])
def test_site_read_is_refused_outside_the_requirements_and_never_reaches_the_host(w, host, breach):
    box = _sensitive(w)
    if breach != "no_grant":
        sg.grant(w)
    if breach == "undeclared":
        w["agent"].interface = parse(IFACE)   # `connect` without `data: sensitive`
        w["agent"].save(update_fields=["interface"])
    if breach == "runner":
        RunnerFlag.objects.filter(runner=box).delete()
    uri = host.store.save("query", {"rows": ROWS}, owner="u-42", name="r")["uri"]
    # Refused by the caller token itself (an undeclared capability's sensitive turn
    # stands for nothing) or by the gateway — either way before any request.
    with pytest.raises(Exception):  # noqa: B017 - which door refuses is not the point
        with sg.visitor(w):
            sg.call(host, "site_read", {"uri": uri})
    assert host.seen == [], "a refused read must not reach the host by any route"


# --- subscriptions: the host's "it changed" reaches the View ------------------------------


@pytest.fixture()
def sent(monkeypatch):
    from apps.realtime import groups

    out: list = []
    monkeypatch.setattr(groups, "publish", lambda group, message: out.append((group, message)))
    return out


def _notify(w, uris, key=None):
    token = resources.updated_notification("connect-labs", key or w["site_key"], resources_updated.audiences()[0],
                                           uris, workspace="connect")
    return Client().post("/api/sites/resources-updated", data="{}", content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}"), token


def test_subscribe_then_the_hosts_signed_update_reaches_the_view(w, host, sent):
    link = _link(_converse(w, host, sensitive=False))
    r = through(host, lambda: post(client_for(w["owner"]), base(w) + "/subscribe", {"uri": link["uri"]}))
    assert r.status_code == 200, r.content
    assert AppViewSubscription.objects.filter(session=w["mine"], tool_call_id=CALL, uri=link["uri"]).exists()

    r, token = _notify(w, [link["uri"], "labs-result://not-watched"])
    assert r.status_code == 200 and r.json() == {"notified": 1}
    assert sent == [(session_group(w["mine"].pk),
                     {"type": "app.resource_updated", "tool_call_id": CALL, "uri": link["uri"]})]

    # Single use; another key; nothing watched after unsubscribing.
    replay = Client().post("/api/sites/resources-updated", data="{}", content_type="application/json",
                           HTTP_AUTHORIZATION=f"Bearer {token}")
    assert replay.status_code == 401 and "replayed" in replay.json()["detail"]
    assert _notify(w, [link["uri"]], key=generate_private_key("EdDSA"))[0].status_code == 401
    assert post(client_for(w["owner"]), base(w) + "/unsubscribe", {"uri": link["uri"]}).status_code == 200
    assert _notify(w, [link["uri"]])[0].json() == {"notified": 0}


def test_another_sites_update_never_reaches_this_view(w, host, sent):
    from apps.tokens.models import AppCredential

    link = _link(_converse(w, host, sensitive=False))
    through(host, lambda: post(client_for(w["owner"]), base(w) + "/subscribe", {"uri": link["uri"]}))
    other = AppCredential.create_credential(name="other-site", created_by=w["owner"], workspace=w["ws"])
    other_key = generate_private_key("EdDSA")
    other.public_keys = [public_pem(other_key)]
    other.save(update_fields=["public_keys"])
    token = resources.updated_notification("other-site", other_key, resources_updated.audiences()[0],
                                           [link["uri"]], workspace="connect")
    r = Client().post("/api/sites/resources-updated", data="{}", content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 200 and r.json() == {"notified": 0} and sent == []


def test_subscribe_needs_a_view_a_grant_and_a_uri(w, host):
    link = _link(_converse(w, host, sensitive=False))
    nogrant = _member(w["ws"], "nora")
    SessionParticipant.objects.create(session=w["mine"], user=nogrant, role=SessionParticipant.EDITOR)
    r = through(host, lambda: post(client_for(nogrant), base(w) + "/subscribe", {"uri": link["uri"]}))
    assert r.status_code == 403 and "no_grant" in r.json()["detail"]
    r = post(client_for(w["owner"]), f"/api/canopy-sessions/{w['mine'].pk}/apps/nope/subscribe",
             {"uri": link["uri"]})
    assert r.status_code == 404
    r = through(host, lambda: post(client_for(w["owner"]), base(w) + "/subscribe", {"uri": "not a uri"}))
    assert r.status_code == 422
    assert not AppViewSubscription.objects.exists()


def test_the_frame_reaches_the_socket_and_ag_ui_carries_it():
    frame = {"event": "app.resource_updated", "data": {"tool_call_id": CALL, "uri": "labs-result://a"}}
    [event] = agui.project(frame, thread_id="t")
    encoded = agui.encode(event)
    assert encoded["name"] == "canopy.app.resource_updated"
    assert encoded["value"] == {"tool_call_id": CALL, "uri": "labs-result://a"}

    from apps.canopy_sessions.consumers import SessionConsumer, without_tools

    assert without_tools(frame) == frame   # the widget's Views hear it too
    got = []

    class Probe(SessionConsumer):
        async def send_json(self, content, close=False):
            got.append(content)

    async_to_sync(Probe().app_resource_updated)({"type": "app.resource_updated", "tool_call_id": CALL,
                                                 "uri": "labs-result://a"})
    assert got == [frame]


def test_the_view_and_host_routes_are_not_mcp_tools():
    from apps.api.api import api
    from apps.mcp import api_tools

    ops = {op.get("operationId") for item in api_tools.tool_spec(api.get_openapi_schema())["paths"].values()
           for op in item.values() if isinstance(op, dict)}
    assert not ops & {"app_view_subscribe", "app_view_unsubscribe", "resources_updated",
                      "contact_app_view_subscribe", "contact_app_view_unsubscribe"}


# --- the canary: nothing canopy stores for a sensitive conversation holds a row ------------


def test_nothing_canopy_stores_for_a_sensitive_conversation_holds_a_row(w, host, sent):
    from django.apps import apps

    link = _link(_converse(w, host, sensitive=True))
    turn = Turn.objects.create(chat_session=w["mine"], prompt="q", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=uuid.uuid4().hex)
    ledger.append_events(turn, [{"kind": "tool_end", "payload": {"tool_use_id": CALL,
                                                                 "output": json.dumps(_answer(w, host))}}])
    c = client_for(w["owner"])
    through(host, lambda: c.get(base(w) + "/resource"))
    read = through(host, lambda: post(c, base(w) + "/read", {"uri": link["uri"]}))
    assert SENTINEL in read.content.decode()   # the person saw the row...
    through(host, lambda: post(c, base(w) + "/subscribe", {"uri": link["uri"]}))
    _notify(w, [link["uri"]])
    put = c.put(base(w) + "/context", data=json.dumps({"structuredContent": {"rows": ROWS}}),
                content_type="application/json")
    assert put.status_code == 200

    # The HOST keeps it — that is the whole design — in its own store, not canopy's database.
    assert any(k.owner == "sub-owner" for k in host.store.rows.values())
    hits = []
    for model in apps.get_models():
        for row in model.objects.all().values():
            if SENTINEL in json.dumps(row, default=str):
                hits.append(model.__name__)
    assert hits == [], f"...and canopy kept it in {sorted(set(hits))}"


# --- Canopy-View: the person's own View, stated and never inferred -----------------------
#
# A host may give a person's View what it gives no agent (Connect Labs: the rows of a
# sensitive result their cleared agent already received). It used to infer "the person is
# looking" from a MISSING `Canopy-Execution`, so anything that could make canopy issue a
# delegated read without a statement read as the person. canopy now states it — only when
# the principal behind the View request is a person acting as themselves
# (`harness.acting_person`, by credential, never by door), only in a conversation they are
# in — bound to the person, the resource, the conversation, the DPoP key and the token.
# The host's recorded headers are verified here with the SDK's `verify_view`, as Labs does.

VIEW_H, EXEC_H = contract.VIEW_HEADER.lower(), contract.EXECUTION_HEADER.lower()


def _view_of(headers, *, uri, subject, token, rpc=None):
    config, keys, jtis = sg.host_side()
    return verify_view({k.title(): v for k, v in headers.items()}, config=config, client_keys=keys,
                       replay_store=jtis, uri=uri, subject=subject, jkt=client_identity.dpop_jkt(),
                       access_token=token, rpc=rpc or ("resources/read", {"uri": uri}))


def _read_as(w, host, who, uri):
    host.seen.clear()
    return through(host, lambda: post(client_for(w[who]), base(w) + "/read", {"uri": uri}))


# --- the person's own View says so, bound to them, this result, this conversation --------


def test_the_persons_own_view_read_carries_a_view_statement_and_no_execution(w, host):
    link = _link(_converse(w, host, sensitive=True))
    r = _read_as(w, host, "owner", link["uri"])
    assert r.status_code == 200 and SENTINEL in r.content.decode()
    stated = [h for h in host.seen if VIEW_H in h]
    assert len(stated) == 1, "the View's read — and only the read, not its initialize — carries Canopy-View"
    assert not any(EXEC_H in h for h in host.seen), "a View is never an agent's call"
    view = _view_of(stated[-1], uri=link["uri"], subject="grant-owner", token="grant-owner")
    assert view.session == str(w["mine"].pk) and view.agent == "ace"
    assert view.viewer == f"user:{w['owner'].pk}"
    # Bound to that very request: not to a read of another page or another call.
    for rpc in (("resources/read", {"uri": link["uri"] + "?page=2"}), ("tools/call", {"name": "data_query"})):
        with pytest.raises(ViewRefused, match="different request"):
            _view_of(stated[-1], uri=link["uri"], subject="grant-owner", token="grant-owner", rpc=rpc)


def test_a_view_statement_is_bound_to_its_person_resource_token_and_use(w, host):
    link = _link(_converse(w, host, sensitive=True))
    _read_as(w, host, "owner", link["uri"])
    headers = [h for h in host.seen if VIEW_H in h][-1]
    for kw, code in (({"subject": "grant-eddie"}, "wrong_person"),
                     ({"uri": "labs-result://someone-elses"}, "wrong_resource"),
                     ({"token": "grant-eddie"}, "unbound")):
        args = {"uri": link["uri"], "subject": "grant-owner", "token": "grant-owner", **kw}
        with pytest.raises(ViewRefused) as info:
            _view_of(headers, **args)
        assert info.value.code == code
    config, keys, _ = sg.host_side()
    store = MemoryJtiStore()
    once = dict(config=config, client_keys=keys, replay_store=store, uri=link["uri"], subject="grant-owner",
                jkt=client_identity.dpop_jkt(), access_token="grant-owner")
    verify_view({k.title(): v for k, v in headers.items()}, **once)
    with pytest.raises(ViewRefused, match="already been used"):
        verify_view({k.title(): v for k, v in headers.items()}, **once)


def test_a_views_own_tool_call_carries_a_view_statement_for_that_call(w, host):
    """A card's button (labs: preview, apply, a commit through `workflow_run_action`) is
    admitted by the host only on canopy's word that it is the person's own View, for THIS
    `tools/call`, the View's `ui://` resource as its uri."""
    _converse(w, host, sensitive=False)
    host.seen.clear()
    args = {"sql": "select 1"}
    r = through(host, lambda: post(client_for(w["owner"]), base(w) + "/call",
                                   {"name": "data_query", "arguments": args}))
    assert r.status_code == 200, r.content
    stated = [h for h in host.seen if VIEW_H in h]
    assert len(stated) == 1, "the call — and only the call, not initialize or the listing — carries it"
    assert not any(EXEC_H in h for h in host.seen), "a View is never an agent's call"
    call = ("tools/call", {"name": "data_query", "arguments": args})
    view = _view_of(stated[0], uri=VIEW, subject="grant-owner", token="grant-owner", rpc=call)
    assert view.session == str(w["mine"].pk) and view.viewer == f"user:{w['owner'].pk}"
    # Bound to that very call: not another tool, not other arguments.
    for rpc in (("tools/call", {"name": "big_query", "arguments": args}),
                ("tools/call", {"name": "data_query", "arguments": {"sql": "drop"}})):
        with pytest.raises(ViewRefused, match="different request"):
            _view_of(stated[0], uri=VIEW, subject="grant-owner", token="grant-owner", rpc=rpc)


def test_an_agents_own_tool_call_never_carries_a_view_statement(w, host):
    sg.grant(w)
    host.seen.clear()
    _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    assert host.seen and not any(VIEW_H in h for h in host.seen)
    assert all(EXEC_H in h for h in host.seen)


def test_a_colleague_in_the_conversation_gets_a_statement_naming_them(w, host):
    link = _link(_converse(w, host, sensitive=True))
    _read_as(w, host, "eddie", link["uri"])
    headers = [h for h in host.seen if VIEW_H in h][-1]
    assert _view_of(headers, uri=link["uri"], subject="grant-eddie", token="grant-eddie").subject == "grant-eddie"


def test_an_agents_login_at_a_browser_gets_no_view_statement(w, host, monkeypatch):
    """An agent's own canopy login is not a person, whichever door it uses — even a
    session cookie in a conversation it was added to."""
    link = _link(_converse(w, host, sensitive=True))
    bot = User.objects.create_user("ace-bot", "ace-bot@agents.test", "pw")
    Agent.objects.filter(pk=w["agent"].pk).update(user=bot)
    WorkspaceMembership.objects.create(user=bot, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=w["mine"], user=bot, role=SessionParticipant.EDITOR)
    w["bot"] = bot
    monkeypatch.setitem(SUBJECTS, "grant-bot", "sub-bot")
    _grant(w["app"], "grant-bot", bot)  # a grant of its own, so the read reaches the host
    _read_as(w, host, "bot", link["uri"])   # the host's own ACL then refuses it; not the point
    assert host.seen, "the read went to the host..."
    assert not any(VIEW_H in h for h in host.seen), "...without saying the person is looking"


# --- an agent's door always carries its execution statement, never a view one ---------------


def test_site_read_carries_the_agents_execution_statement_and_never_a_view(w, host):
    sg.grant(w)
    out = _visitor_call(w, host, "site_call", {"tool": "data_query", "arguments": {"sql": "x"}})
    uri = _link(out.structured_content)["uri"]
    host.seen.clear()
    _visitor_call(w, host, "site_read", {"uri": uri})
    assert host.seen and all(EXEC_H in h for h in host.seen)
    assert not any(VIEW_H in h for h in host.seen)
    # Each execution statement is bound to its own JSON-RPC request.
    from canopy_sdk.host import verify_execution

    config, keys, _ = sg.host_side()
    rpcs = {jwt_claims(h[EXEC_H]).get("rpc") for h in host.seen}
    assert contract.rpc_digest("resources/read", {"uri": uri}) in rpcs and len(rpcs) == len(host.seen)
    read = [h for h in host.seen if jwt_claims(h[EXEC_H])["rpc"] == contract.rpc_digest("resources/read", {"uri": uri})]
    verify_execution({k.title(): v for k, v in read[0].items()}, config=config, client_keys=keys,
                     replay_store=MemoryJtiStore(), jkt=client_identity.dpop_jkt(),
                     access_token=sg.HOST_TOKEN, rpc=("resources/read", {"uri": uri}))


def jwt_claims(raw):
    import jwt

    return jwt.decode(raw, options={"verify_signature": False})


# --- can an agent reach the View's own door at all? (the suspicion, tested) -----------------


def _turn_in(w):
    return Turn.objects.create(chat_session=w["mine"], prompt="q", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=uuid.uuid4().hex)


def test_an_agents_credentials_cannot_reach_the_view_read_door(w, host):
    """Every credential an agent's turn holds is refused at the View routes, so no turn can
    make canopy issue a View's delegated read: a conversation's caller token (it reaches
    only routes MCP serves as tools, and these are not), the turn's own login and a PAT
    (refused by the route), and MCP (the routes are not tools)."""
    link = _link(_converse(w, host, sensitive=False))
    turn = _turn_in(w)
    caller = caller_tokens.mint(turn, scoped=True)
    bot = User.objects.create_user("ace-bot", "ace-bot@agents.test", "pw")
    Agent.objects.filter(pk=w["agent"].pk).update(user=bot)
    turn_login, tok = PersonalToken.create_for_user(user=bot, label="turn")
    runner = Runner.objects.create(name="box", workspace=w["ws"], owner=w["owner"])
    TurnAgentToken.objects.create(turn=turn, runner=runner, token=tok)
    own_pat, _ = PersonalToken.create_for_user(user=w["owner"], label="mine")
    host.seen.clear()
    for raw in (caller, turn_login, own_pat):
        r = post(Client(), base(w) + "/read", {"uri": link["uri"]}, HTTP_AUTHORIZATION=f"Bearer {raw}")
        assert r.status_code in (401, 403), (raw[:5], r.status_code, r.content)
    assert host.seen == [], "no agent credential made canopy read from the host"

    from apps.api.api import api
    from apps.mcp import api_tools

    ops = {op.get("operationId") for item in api_tools.tool_spec(api.get_openapi_schema())["paths"].values()
           for op in item.values() if isinstance(op, dict)}
    assert not ops & {"app_view_read", "app_view_call", "app_view_resource",
                      "contact_app_view_read", "contact_app_view_call", "contact_app_view_resource"}


# --- the principal rule, by credential --------------------------------------------------------


def _req(user=None, *, contact=None, cred=None, meta=None, **attrs):
    return SimpleNamespace(user=user, contact=contact, auth_credential=cred, META=meta or {}, **attrs)


def test_who_is_a_person_acting_as_themselves(w):
    from django.contrib.auth.models import AnonymousUser

    person, bot = w["owner"], User.objects.create_user("bot", "bot@agents.test", "pw")
    Agent.objects.filter(pk=w["agent"].pk).update(user=bot)
    turn = _turn_in(w)
    _, login = PersonalToken.create_for_user(user=w["eddie"], label="turn")
    TurnAgentToken.objects.create(turn=turn, runner=Runner.objects.create(name="b", workspace=w["ws"],
                                                                          owner=w["owner"]), token=login)
    _, own = PersonalToken.create_for_user(user=w["eddie"], label="mine")

    assert acting_person.refusal(_req(person)) is None                                   # a browser
    assert acting_person.refusal(_req(person, cred={"type": "delegated", "id": 1})) is None  # the widget
    assert acting_person.refusal(_req(w["eddie"], cred={"type": "pat", "id": own.pk})) is None  # own PAT
    assert acting_person.refusal(_req(person, cred={"type": "oauth", "id": 2})) is None    # own sign-in
    assert acting_person.refusal(_req(AnonymousUser(), contact=w["contact"])) is None      # a visitor

    assert acting_person.refusal(_req(person, cred={"type": "caller_token", "id": 1,
                                                    "turn_id": str(turn.pk)})) == "turn"
    assert acting_person.refusal(_req(person, cred={"type": "pat", "id": 9, "turn_id": "t"})) == "turn"
    assert acting_person.refusal(_req(person, meta={acting_person.TURN_HEADER: str(turn.pk)})) == "turn"
    assert acting_person.refusal(_req(w["eddie"], cred={"type": "pat", "id": login.pk})) == "turn_login"
    assert acting_person.refusal(_req(bot)) == "agent_login"
    assert acting_person.refusal(_req(person, runner_token=object())) == "runner"
    assert acting_person.refusal(_req(AnonymousUser())) == "anonymous"
    acting = User.objects.get(pk=w["vera"].pk)
    setattr(acting, turn_asker.ATTR, object())
    assert acting_person.refusal(_req(acting)) == "on_behalf"


def test_view_facts_only_for_a_person_in_the_conversation(w):
    grant = w["grants"]["owner"]
    session = w["mine"]
    facts = mcp_apps_views.view_facts(session, mcp_apps_views.Viewer(user=w["owner"], person=True), grant)
    assert facts == {"subject": "grant-owner", "session": str(session.pk),
                     "viewer": f"user:{w['owner'].pk}", "agent": "ace"}
    # Not acting as themselves (an agent's credential), or not in this conversation.
    assert mcp_apps_views.view_facts(session, mcp_apps_views.Viewer(user=w["owner"]), grant) is None
    outsider = User.objects.create_user("olga", "olga@dimagi.com", "pw")
    assert mcp_apps_views.view_facts(session, mcp_apps_views.Viewer(user=outsider, person=True), grant) is None
    # A contact, in their own conversation only.
    own = mcp_apps_views.Viewer(contact=w["contact"], person=True)
    assert mcp_apps_views.view_facts(w["session"], own, grant)["viewer"] == f"contact:{w['contact'].pk}"
    assert mcp_apps_views.view_facts(session, own, grant) is None


def test_a_context_never_carries_a_view_beside_an_execution(w):
    from apps.host_grants import host_gateway

    ctx = host_gateway.context_for_grant(w["app"], w["grants"]["owner"], turn_id="t", agent_slug="ace",
                                         ceiling=[], execution={"agent": "ace"},
                                         view={"subject": "s", "session": "x"})
    assert ctx.execution == {"agent": "ace"} and ctx.view is None


def test_the_view_read_probe_shows_both_halves(w, host):
    """`manage.py view_read_probe` (the live check after a deploy): the same read, without a
    statement and with Canopy-View. This test host admits both — a real Labs refuses the
    first — so what is checked is that only the second carried the statement."""
    from io import StringIO

    from django.core.management import call_command

    _converse(w, host, sensitive=True)
    host.seen.clear()
    out = StringIO()
    through(host, lambda: call_command("view_read_probe", session=str(w["mine"].pk),
                                       email=w["owner"].email, stdout=out))
    text = out.getvalue()
    assert "no statement: READ" in text and "Canopy-View: READ (2 rows" in text
    assert SENTINEL not in text
    with_view = [h for h in host.seen if VIEW_H in h]
    assert with_view and len(with_view) < len(host.seen)


def test_who_may_read_and_subscribe_through_a_view(w, host):
    """Not `may_act`: a VIEWER participant reads an ordinary conversation's results with
    their own grant. A sensitive conversation's are read only by its own people on its
    chat surface — and only they get Canopy-View."""
    from apps.workspaces.models import WorkspaceMembership as WM

    link = _link(_converse(w, host, sensitive=False))
    olga = User.objects.create_user("olga", "olga@dimagi.com", "pw")
    WM.objects.create(user=olga, workspace=w["ws"], role=WM.EDITOR)
    SessionParticipant.objects.create(session=w["mine"], user=olga, role=SessionParticipant.VIEWER)
    SUBJECTS["grant-olga"] = "sub-olga"
    try:
        _grant(w["app"], "grant-olga", olga)
        w["olga"] = olga
        r = _read_as(w, host, "olga", link["uri"])
        assert r.status_code in (200, 403) and host.seen   # reached the host: its ACL decides
        assert any(VIEW_H in h for h in host.seen)          # a person in the conversation
    finally:
        SUBJECTS.pop("grant-olga", None)

    viewer = mcp_apps_views.Viewer(user=w["owner"], person=True, chat_surface=False)
    mcp_apps_views.may_read(w["mine"], viewer)              # ordinary: the session ACL decides
    from apps.canopy_sessions import services as chat_services

    chat_services.add_runner_requirements(w["mine"], ["sensitive_data"])
    w["mine"].refresh_from_db()
    with pytest.raises(mcp_apps_views.ViewRefusal):
        mcp_apps_views.may_read(w["mine"], viewer)          # not on its chat surface
    outsider = User.objects.create_user("omar", "omar@dimagi.com", "pw")
    with pytest.raises(mcp_apps_views.ViewRefusal):
        mcp_apps_views.may_read(w["mine"], mcp_apps_views.Viewer(user=outsider, person=True, chat_surface=True))
    mcp_apps_views.may_read(w["mine"], mcp_apps_views.Viewer(user=w["owner"], person=True, chat_surface=True))
    assert mcp_apps_views.view_facts(w["mine"], viewer, w["grants"]["owner"]) is None
