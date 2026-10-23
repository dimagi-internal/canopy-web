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

import httpx2
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from canopy_sdk import resources
from canopy_sdk import site as site_doc
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.test import Client
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ResourceLink, TextContent

from apps.agents.interface import parse
from apps.canopy_sessions import agui
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import AppViewSubscription, Message, Session, SessionParticipant
from apps.common.encryption import encrypt_secret
from apps.harness import ledger, sensitivity
from apps.harness.models import Runner, RunnerFlag, Turn, TurnEvent
from apps.host_grants import client_identity, host_gateway, resources_updated, site_needs
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.mcp_apps_host import mcp_apps
from apps.realtime.groups import session_group
from apps.tokens.models import HostGrant
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

    return HostGrant.objects.create(
        app=app, subject=token, user=user, access_token_enc=encrypt_secret(token), scope="data:read",
        resource=sg.RESOURCE, dpop_jkt=client_identity.dpop_jkt(),
        expires_at=timezone.now() + timedelta(minutes=10))


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
    mcp_apps.record_tools(w["app"].pk, [("data_query", {"ui": {"resourceUri": VIEW,
                                                               "visibility": ["model", "app"]}})])
    w["site_key"] = generate_private_key("EdDSA")
    w["app"].public_keys = [public_pem(w["site_key"])]
    w["app"].save(update_fields=["public_keys"])
    return w


def _answer(w, host) -> dict:
    """`site_call`'s answer for `data_query`, as the gateway returns it to the agent."""
    ctx = host_gateway.context_for_grant(w["app"], w["grants"]["owner"], turn_id="", agent_slug="ace",
                                         ceiling=[])

    async def main():
        async with host.lifespan():
            return await host_gateway.call_tool(ctx, "data_query", {"sql": "select * from visits"})
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
    [link] = [b for b in answer["content"] if b.get("type") == "resource_link"]
    return link


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
    facts = json.loads(answer["content"][0]["text"])
    assert facts["row_count"] == 2 and facts["columns"] == ["name", "visits"]


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
        out = sensitivity.strip_result(payload, session=w["mine"], text=text)
        assert out["resource_links"] == [{"type": "resource_link", "uri": "labs-result://abc", "name": "r",
                                          "mimeType": "application/json"}]
        assert out["withheld"] is True and out["tool_use_id"] == CALL
        assert SENTINEL not in json.dumps(out)


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
    assert row.content["resource_links"] == [link] and row.plaintext == "" and row.sealed is None
    assert SENTINEL not in json.dumps(row.content)
    turn = Turn.objects.create(chat_session=w["mine"], prompt="q", initiator_user=w["owner"],
                               initiator_kind="user", idempotency_key=uuid.uuid4().hex)
    ledger.append_events(turn, [{"kind": "tool_end", "payload": {"tool_use_id": CALL, "is_error": False,
                                                                 "output": json.dumps(answer)}}])
    event = TurnEvent.objects.get(turn=turn)
    assert event.payload["resource_links"] == [link] and event.payload["redacted"] == "sensitive"
    assert SENTINEL not in json.dumps(event.payload)


# --- the View gets the links, and reads the rows as its viewer ---------------------------


def test_a_sensitive_view_gets_the_links_only_and_reads_the_rows_as_its_viewer(w, host):
    link = _link(_converse(w, host, sensitive=True))
    r = through(host, lambda: client_for(w["owner"]).get(base(w) + "/resource"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["tool_result"] == {"content": [link], "structuredContent": None,
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
    assert result["content"] == answer["content"] and _link(result) == _link(answer)
    assert "_meta" not in result


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


def test_site_read_of_someone_elses_result_is_the_hosts_refusal(w, host):
    sg.grant(w)
    theirs = host.store.save("query", {"rows": ROWS}, owner="sub-vera", name="r")["uri"]
    with sg.visitor(w), pytest.raises(Exception, match="forbidden"):
        sg.call(host, "site_read", {"uri": theirs})


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
    uri = host.store.save("query", {"rows": ROWS}, owner="u-42", name="r")["uri"]
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
