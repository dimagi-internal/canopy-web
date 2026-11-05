"""`POST /api/connections/{provider}/request`: the one route through which a screen calls
Salesforce. Salesforce is stubbed at the `requests` boundary (`call_service.requests.request`),
its token endpoint by `FakeSF`, and everything between them is the real thing: the session
check, the surface's gate, the allowlist, the declaration, the connection and `server_tokens`.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import timedelta
from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.connections import call_service, server_key, server_tokens
from apps.connections.models import Connection
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from apps.tokens.models import AppCredential, DelegatedToken, PersonalToken
from apps.workspace_apps.models import AppInstallation, AppTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import FakeSF, _resp, _stored, _token_body, sealed_to_owner, sf_app

pytestmark = pytest.mark.django_db

J = "application/json"
URL = "/api/connections/salesforce/request"
CODE = "function View({ canopy }) { return <div/>; }"
LEAD = "00Q5g00000AbCdEFGH"
INSTANCE = "https://dimagi.my.salesforce.com"
ACCESS = "00D!access_1"
READ_QUERY = {"method": "GET", "path": "query"}
WRITE_LEAD = {"method": "PATCH", "path": "sobjects/Lead/*"}
DECL = {"provider": "salesforce", "as": "viewer", "reads": [READ_QUERY], "writes": [WRITE_LEAD]}


@pytest.fixture(autouse=True)
def _clean():
    server_tokens._forget_all()
    cache.clear()
    yield
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


@pytest.fixture
def w(key):
    """jj's own Salesforce, canopy-web allowed to use it, and a view of theirs that declares it."""
    world = TokensWorld()
    world.conn = world.shared(sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True),
                              canopy_web=server_key.thumbprint())
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Leads", "code": CODE, "connections": [DECL]})
    world.client = Client()
    world.client.force_login(world.person)
    return world


def view_surface(world, version=None) -> dict:
    return {"kind": "view", "id": world.view.pk, "version": version or world.view.version}


def body_of(world, **over) -> dict:
    return {"method": "GET", "path": "query", "query": {"q": "SELECT Id FROM Lead"},
            "surface": view_surface(world), **over}


def post(world, client=None, **over):
    return (client or world.client).post(URL, json.dumps(body_of(world, **over)), content_type=J)


def lead_write(world, client=None, **over):
    """A write, with Task 4's operation check out of the way."""
    fields = {"method": "PATCH", "path": f"sobjects/Lead/{LEAD}", "query": None, "body": {"Status": "Open"},
              "operation_id": "op-1", **over}
    with mock.patch.object(call_service, "_check_operation", return_value=None):
        return post(world, client, **fields)


def error_of(resp) -> str:
    return resp.json().get("error")


def install_app(world, *, enabled=True, public=False, connections=(DECL,)) -> AppInstallation:
    manifest = {"pages": [], "collections": {}, "connections": list(connections)}
    template = AppTemplate.objects.create(key="crm", name="CRM")
    return AppInstallation.objects.create(workspace=world.ws, key="crm", name="CRM", manifest=manifest,
                                          template=template, enabled=enabled, public=public)


# ---- the call ------------------------------------------------------------------------------

def test_viewer_read_returns_status_and_body(w):
    with FakeSF(), Salesforce(Answer(body={"totalSize": 7})) as sf:
        r = post(w)
    assert r.status_code == 200, r.content
    assert r.json() == {"status": 200, "body": {"totalSize": 7}}
    call = sf.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{INSTANCE}/services/data/v65.0/query?q=SELECT+Id+FROM+Lead"
    assert sf.bearer() == f"Bearer {ACCESS}"
    assert call["allow_redirects"] is False and call["stream"] is True and call["timeout"] == (5, 30)
    assert "sforce" not in " ".join(r.headers).lower(), "Salesforce's own headers are not returned"


def test_the_rule_method_is_sent_not_the_callers_spelling(w):
    with FakeSF(), Salesforce() as sf:
        assert post(w, method="get").status_code == 200
    assert sf.calls[0]["method"] == "GET"


def test_salesforce_error_status_passes_through(w):
    refused = [{"errorCode": "MALFORMED_QUERY", "message": "unexpected token"}]
    with FakeSF(), Salesforce(Answer(status=400, body=refused)):
        r = post(w)
    assert r.status_code == 200
    assert r.json() == {"status": 400, "body": refused}


def test_non_json_answer_is_null_body_and_first_1000_chars(w):
    html = ("<html>" + "x" * 2000).encode()
    with FakeSF(), Salesforce(Answer(status=500, raw=html, headers={"Content-Type": "text/html"})):
        r = post(w)
    assert r.json()["status"] == 500 and r.json()["body"] is None
    assert r.json()["text"] == html.decode()[:1000]


def test_an_empty_answer_is_null_body_with_no_text(w):
    with FakeSF(), Salesforce(Answer(status=204, raw=b"")):
        r = lead_write(w)
    assert r.json() == {"status": 204, "body": None}


# ---- who may call --------------------------------------------------------------------------

def test_pat_refused(w):
    raw, _ = PersonalToken.create_for_user(user=w.person, label="cli")
    with Salesforce() as sf:
        r = post(w, Client(HTTP_AUTHORIZATION=f"Bearer {raw}"))
    assert r.status_code == 403 and error_of(r) == "session_required"
    assert r.json()["detail"]
    assert sf.calls == []


def test_delegated_token_riding_with_a_session_refused(w):
    """Same-origin, the frame's calls carry the cookie and a site's token: the site is asking."""
    app = AppCredential.create_credential(name="site", created_by=w.person, workspace=w.ws)
    raw, _ = DelegatedToken.issue(app=app, user=w.person, ttl_seconds=600)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    c.force_login(w.person)
    with Salesforce() as sf:
        r = post(w, c)
    assert r.status_code == 403 and error_of(r) == "session_required"
    assert sf.calls == []


def test_anonymous_refused(w):
    with Salesforce() as sf:
        assert post(w, Client()).status_code == 401
    assert sf.calls == []


def test_a_session_post_needs_csrf(w):
    c = Client(enforce_csrf_checks=True)
    c.force_login(w.person)
    with Salesforce() as sf:
        assert post(w, c).status_code == 403
    assert sf.calls == []


def test_a_view_in_another_workspace_is_404(w):
    other = view_services.create(workspace_id=sf_app_workspace("elsewhere", w.op).slug, project=None, user=w.op,
                                 data={"title": "Theirs", "code": CODE, "connections": [DECL]})
    with Salesforce() as sf:
        r = post(w, surface={"kind": "view", "id": other.pk, "version": 1})
    assert r.status_code == 404
    assert sf.calls == []


def test_a_version_that_does_not_exist_is_404(w):
    with Salesforce() as sf:
        assert post(w, surface=view_surface(w, version=99)).status_code == 404
    assert sf.calls == []


def sf_app_workspace(slug, user) -> Workspace:
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    sf_app(ws)
    return ws


# ---- whose connection ----------------------------------------------------------------------

def test_other_workspace_connection_not_used(w):
    """jj has a Salesforce in another workspace of theirs, and none in the view's."""
    Connection.objects.filter(pk=w.conn.pk).delete()
    ws2 = sf_app_workspace("other", w.person)
    w.shared(sealed_to_owner({"user": w.person}, w.person, app=sf_app(ws2), server_use=True),
             canopy_web=server_key.thumbprint())
    with FakeSF(), Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "not_connected"
    assert "/w/" in r.json()["connect_url"] and "user " not in r.json()["detail"]
    assert sf.calls == []


def test_not_connected_says_where_to_connect(w):
    Connection.objects.filter(pk=w.conn.pk).delete()
    with FakeSF(), Salesforce():
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "not_connected"
    assert "/salesforce/" in r.json()["connect_url"] and "/w/" in r.json()["connect_url"]
    assert "Connect Salesforce in Settings → Connections." in r.json()["detail"]


def test_needs_reconnect_tells_the_owner_themselves_and_gives_the_link(w):
    Connection.objects.filter(pk=w.conn.pk).update(refresh_failed_at=timezone.now())
    with Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "not_connected"
    assert "owner" not in r.json()["detail"]
    assert r.json()["detail"].endswith("Connect Salesforce in Settings → Connections.")
    assert r.json()["connect_url"]
    assert sf.calls == []


def test_a_session_ended_gives_the_owner_the_link(w):
    with FakeSF(), Salesforce(Answer(status=401, body=[]), Answer(status=401, body=[])):
        r = post(w)
    assert error_of(r) == "session_ended"
    assert "owner" not in r.json()["detail"] and r.json()["connect_url"]
    assert r.json()["detail"].endswith("Connect Salesforce in Settings → Connections.")


def test_what_is_stored_not_trusted_tells_the_owner_themselves(w):
    with FakeSF(), Salesforce(), mock.patch.object(server_tokens, "access", side_effect=server_tokens._refused(
            w.conn, "a test")):
        r = post(w)
    assert error_of(r) == "server_refused"
    assert "owner" not in r.json()["detail"]
    assert r.json()["detail"].endswith("Connect Salesforce in Settings → Connections.")


def test_ambiguous(w):
    _stored({"user": w.person}, w.person, app=w.app, account_id="00Dorg/005other", account_label="second@dimagi.com",
            server_use=True)
    with Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "ambiguous"
    assert "second@dimagi.com" in r.json()["detail"] and "isconnect" in r.json()["detail"]
    assert sf.calls == []


def test_off_switch_sentence(w):
    Connection.objects.filter(pk=w.conn.pk).update(server_use=False)
    with Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "server_use_off"
    assert r.json()["detail"].endswith("You can turn this on in Settings → Connections.")
    assert "owner" not in r.json()["detail"]
    assert sf.calls == []


def test_with_no_key_nobody_is_told_to_find_a_switch(w):
    """`_switch_shown` hides the switch while canopy-web has no key, so no sentence may point at it."""
    with mock.patch.object(server_key, "available", return_value=False), Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "server_use_off"
    assert r.json()["detail"] == "This workspace hasn't turned on Salesforce for screens yet."
    assert sf.calls == []


def test_a_limited_connection_is_not_told_about_a_switch_it_does_not_have(w):
    Connection.objects.filter(pk=w.conn.pk).update(owner_turns_only=True)
    with Salesforce():
        r = post(w)
    assert error_of(r) == "server_use_off"
    assert "turn this on" not in r.json()["detail"] and "owner" not in r.json()["detail"]
    assert "Settings → Connections" in r.json()["detail"]


@pytest.mark.parametrize("limit", ["owner_turns_only", "expires_at"])
def test_limited_connection_refused(w, limit):
    value = True if limit == "owner_turns_only" else timezone.now() + timedelta(days=1)
    Connection.objects.filter(pk=w.conn.pk).update(**{limit: value})
    with Salesforce() as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "server_use_off"
    assert sf.calls == []


def test_shared_with_no_project_needs_approval(w):
    entry = {"provider": "salesforce", "as": "shared", "agent": "eva", "reads": [READ_QUERY], "writes": []}
    view = view_services.create(workspace_id="w", project=None, user=w.person,
                                data={"title": "Shared", "code": CODE, "connections": [entry]})
    with Salesforce() as sf:
        r = post(w, surface={"kind": "view", "id": view.pk, "version": 1}, **{"as": "shared"}, agent="eva")
    assert r.status_code == 409 and error_of(r) == "needs_approval"
    assert sf.calls == []


def test_a_write_with_no_operation_needs_one(w):
    with Salesforce() as sf:
        r = post(w, method="PATCH", path=f"sobjects/Lead/{LEAD}", query=None, body={"Status": "Open"})
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == []


# ---- what may be asked ----------------------------------------------------------------------

def test_undeclared_refused_before_token(w):
    with mock.patch.object(server_tokens, "access") as token, Salesforce() as sf:
        r = post(w, path="limits")
    assert r.status_code == 403 and error_of(r) == "not_declared"
    token.assert_not_called()
    assert sf.calls == []


@pytest.mark.parametrize("path", ["composite/batch", "../query", "query?x=y", "%2e%2e/query"])
def test_outside_the_allowlist_refused_before_token(w, path):
    with mock.patch.object(server_tokens, "access") as token:
        r = post(w, path=path)
    assert r.status_code == 403 and error_of(r) == "not_allowed"
    token.assert_not_called()


def test_a_method_override_in_the_query_refused(w):
    with mock.patch.object(server_tokens, "access") as token:
        r = post(w, query={"_HttpMethod": "DELETE"})
    assert r.status_code == 403 and error_of(r) == "not_allowed"
    token.assert_not_called()


def test_google_not_supported(w):
    r = w.client.post("/api/connections/google/request", json.dumps(body_of(w)), content_type=J)
    assert r.status_code == 409 and error_of(r) == "provider_not_supported"


def _redeclare(world, *reads):
    entry = {**DECL, "reads": [{"method": "GET", "path": p} for p in reads]}
    view_services.update(world.view, data={"connections": [entry]}, user=world.person)
    world.view.refresh_from_db()


def test_old_revision_declaration_is_the_one_used(w):
    """v2 adds `limits`: a page still drawing v1 keeps v1's shorter list, and v2 has its own."""
    _redeclare(w, "query", "limits")
    assert w.view.version == 2
    with FakeSF(), Salesforce():
        assert post(w, surface=view_surface(w, 1)).status_code == 200
        assert error_of(post(w, surface=view_surface(w, 1), path="limits", query=None)) == "not_declared"
        assert post(w, surface=view_surface(w, 2), path="limits", query=None).status_code == 200


def test_a_narrowing_reaches_a_page_on_an_old_revision_at_once(w):
    """v2 drops `query`. A page still drawing v1 gets no grace: both revisions must allow the call."""
    _redeclare(w, "limits")
    with Salesforce() as sf:
        r = post(w, surface=view_surface(w, 1))
    assert r.status_code == 403 and error_of(r) == "not_declared"
    assert sf.calls == []


def test_an_old_revision_call_inside_both_is_recorded_as_the_revision_named(w):
    entry = {**DECL, "reads": [READ_QUERY, {"method": "GET", "path": "limits"}]}
    view_services.update(w.view, data={"connections": [entry]}, user=w.person)
    w.view.refresh_from_db()
    with FakeSF(), Salesforce(Answer(status=204, raw=b"")):
        assert lead_write(w, surface=view_surface(w, 1)).status_code == 200
    assert Event.objects.get(kind="connections.connection_call").payload["revision"] == 1


# ---- an app page ----------------------------------------------------------------------------

def app_surface(world) -> dict:
    return {"kind": "app", "workspace": world.ws.slug, "key": "crm"}


def test_app_member_read_uses_the_live_manifest(w):
    install_app(w)
    with FakeSF(), Salesforce():
        assert post(w, surface=app_surface(w)).status_code == 200
        assert error_of(post(w, surface=app_surface(w), path="limits", query=None)) == "not_declared"


def test_public_app_surface_refused(w):
    """A public app, and someone signed in who is not a member: only the `public` surface is theirs."""
    install_app(w, public=True)
    stranger = type(w.person).objects.create_user(username="stranger", email="stranger@example.com")
    c = Client()
    c.force_login(stranger)
    with mock.patch.object(server_tokens, "access") as token, Salesforce() as sf:
        r = post(w, c, surface=app_surface(w))
    assert r.status_code == 404
    token.assert_not_called()
    assert sf.calls == []


def test_a_switched_off_app_is_404_even_to_its_manager(w):
    install_app(w, enabled=False)
    WorkspaceMembership.objects.filter(workspace=w.ws, user=w.person).update(role=WorkspaceMembership.OWNER)
    with Salesforce() as sf:
        assert post(w, surface=app_surface(w)).status_code == 404
    assert sf.calls == []


# ---- limits ---------------------------------------------------------------------------------

def test_rate_limit_429(w):
    with mock.patch.object(call_service, "CALLS_PER_MINUTE", 2), \
            mock.patch.object(server_tokens, "access", return_value=server_tokens.Access(ACCESS, INSTANCE, "d")) as token, \
            Salesforce() as sf:
        assert post(w).status_code == 200
        assert post(w).status_code == 200
        r = post(w)
    assert r.status_code == 429 and error_of(r) == "rate_limited"
    assert "rate-limit" in r.json()["type"]
    assert r.headers["Retry-After"]
    assert token.call_count == 2 and len(sf.calls) == 2, "refused before the token is read"
    assert cache.get(f"connections:calls:{w.person.pk}:salesforce") is not None


def test_a_refused_call_spends_no_budget(w):
    with mock.patch.object(call_service, "CALLS_PER_MINUTE", 1), FakeSF(), Salesforce():
        assert error_of(post(w, path="limits", query=None)) == "not_declared"
        assert post(w).status_code == 200


def test_response_over_5mb_refused(w):
    big = Answer(raw=b"x" * (call_service.MAX_RESPONSE_BYTES + 1))
    with FakeSF(), Salesforce(big):
        r = post(w)
    assert r.status_code == 502 and error_of(r) == "response_too_large"
    assert big.closed


def test_response_of_exactly_5mb_is_fine(w):
    raw = b'"' + b"x" * (call_service.MAX_RESPONSE_BYTES - 2) + b'"'
    with FakeSF(), Salesforce(Answer(raw=raw)):
        assert post(w).json()["status"] == 200


def test_request_over_1mb_refused_on_the_raw_body(w):
    # All of it whitespace: what counts is what arrived, not what it parses to.
    raw = json.dumps(body_of(w)) + " " * (call_service.MAX_REQUEST_BYTES + 1)
    with mock.patch.object(server_tokens, "access") as token, Salesforce() as sf:
        r = w.client.post(URL, raw, content_type=J)
    assert r.status_code == 413 and error_of(r) == "request_too_large"
    token.assert_not_called()
    assert sf.calls == []


def test_a_request_past_the_upload_limit_is_not_a_500(w):
    raw = json.dumps(body_of(w)) + " " * (3 * 1024 * 1024)
    with mock.patch.object(server_tokens, "access") as token:
        r = w.client.post(URL, raw, content_type=J)
    assert r.status_code == 413
    token.assert_not_called()


def test_no_redirect_followed(w):
    moved = Answer(status=302, raw=b"", headers={"Location": "https://evil.example/x"})
    with FakeSF(), Salesforce(moved) as sf:
        r = post(w)
    assert r.status_code == 502 and error_of(r) == "provider_unavailable"
    assert len(sf.calls) == 1 and sf.calls[0]["allow_redirects"] is False
    assert "evil" not in r.content.decode()


# ---- Salesforce's own trouble ---------------------------------------------------------------

def test_401_forces_one_refresh_and_retry(w):
    """A token Salesforce refuses is not asked for again from the cache or the stored envelope."""
    tokens = [_resp(body=_token_body(refresh_token=None)),
              _resp(body=_token_body(access_token="00D!access_2", refresh_token=None))]
    with FakeSF(token=tokens) as fake, Salesforce(Answer(status=401, body=[{"errorCode": "INVALID_SESSION_ID"}]),
                                                  Answer(body={"totalSize": 2})) as sf:
        r = post(w)
    assert r.status_code == 200 and r.json() == {"status": 200, "body": {"totalSize": 2}}
    assert len(fake.token_calls()) == 2
    assert [sf.bearer(0), sf.bearer(1)] == [f"Bearer {ACCESS}", "Bearer 00D!access_2"]
    # And the next call uses the new token without another refresh.
    with FakeSF() as fake2, Salesforce() as sf2:
        assert post(w).status_code == 200
    assert fake2.token_calls() == [] and sf2.bearer() == "Bearer 00D!access_2"


def test_a_second_401_is_session_ended(w):
    with FakeSF(), Salesforce(Answer(status=401, body=[{"errorCode": "INVALID_SESSION_ID"}])) as sf:
        r = post(w)
    assert r.status_code == 409 and error_of(r) == "session_ended"
    assert len(sf.calls) == 2, "one retry, not more"
    assert "connect" in r.json()["detail"].lower()


def test_a_write_is_retried_on_401(w):
    with FakeSF(), Salesforce(Answer(status=401, body=[]), Answer(status=204, raw=b"")) as sf:
        r = lead_write(w)
    assert r.json()["status"] == 204 and len(sf.calls) == 2


@pytest.mark.parametrize("failure", [requests.Timeout("slow"), requests.ConnectionError("down")],
                         ids=["timeout", "connection"])
def test_timeout_not_retried(w, failure):
    with FakeSF(), Salesforce(failure) as sf:
        r = post(w)
    assert r.status_code == 502 and error_of(r) == "provider_unavailable"
    assert len(sf.calls) == 1
    assert "gone through" not in r.json()["detail"] and "was made" not in r.json()["detail"]


def test_a_write_timeout_says_it_may_have_gone_through(w):
    with FakeSF(), Salesforce(requests.Timeout("slow")) as sf:
        r = lead_write(w)
    assert r.status_code == 502 and error_of(r) == "provider_unavailable"
    assert r.json()["detail"] == "Salesforce didn't answer. Check whether the change was made before trying again."
    assert len(sf.calls) == 1
    event = Event.objects.get(kind="connections.connection_call")
    assert event.payload["status"] is None and event.payload["outcome"] == "no_answer"


class SlowServer:
    """A real socket on localhost: headers at once, then `behave(connection)`."""

    def __init__(self, behave, *, close=False):
        self.behave, self.close = behave, close
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            conn.recv(65536)
            shut = b"Connection: close\r\n" if self.close else b""
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + shut
                         + b"Content-Length: 100000\r\n\r\n")
            try:
                self.behave(conn, self.done)
            except OSError:
                pass  # the route hung up, which is the point

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}/x"

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(5)
        self.listener.close()


def _drip(conn, done):
    while not done.wait(0.02):
        conn.sendall(b"x")


def _stall(conn, done):
    conn.sendall(b"x")
    done.wait(10)


@pytest.mark.parametrize("close", [False, True], ids=["keep_alive", "connection_close"])
@pytest.mark.parametrize("behave", [_drip, _stall], ids=["drip", "stall"])
def test_a_slow_answer_stops_at_the_wall_clock_limit_on_a_real_socket(w, behave, close):
    budget = 0.5
    with SlowServer(behave, close=close) as url, \
            mock.patch.object(call_service, "TOTAL_SECONDS", budget), \
            mock.patch.object(call_service.calls, "build_url", return_value=url), \
            mock.patch.object(server_tokens, "access", return_value=server_tokens.Access(ACCESS, INSTANCE, "d")):
        started = time.monotonic()
        r = post(w)
        elapsed = time.monotonic() - started
    assert r.status_code == 502 and error_of(r) == "provider_unavailable"
    assert elapsed < budget + 1.5, f"took {elapsed:.1f}s against a {budget}s limit"


def test_the_wall_clock_limit_is_35_seconds():
    assert call_service.TOTAL_SECONDS == 35


# ---- at most so many at once ----------------------------------------------------------------

def test_ten_in_flight_for_one_person_is_the_most(w):
    held = [call_service._in_flight(w.person.pk) for _ in range(call_service.MAX_IN_FLIGHT_PER_PERSON)]
    for h in held:
        h.__enter__()
    try:
        with mock.patch.object(server_tokens, "access") as token, Salesforce() as sf:
            r = post(w)
    finally:
        for h in held:
            h.__exit__(None, None, None)
    assert r.status_code == 429 and error_of(r) == "rate_limited" and r.json()["detail"].endswith(".")
    token.assert_not_called()
    assert sf.calls == []
    with FakeSF(), Salesforce():
        assert post(w).status_code == 200


def test_fifty_in_flight_in_a_process_is_the_most(w):
    held = [call_service._in_flight(f"someone-{i}") for i in range(call_service.MAX_IN_FLIGHT)]
    for h in held:
        h.__enter__()
    try:
        with mock.patch.object(server_tokens, "access") as token:
            r = post(w)
    finally:
        for h in held:
            h.__exit__(None, None, None)
    assert r.status_code == 429 and error_of(r) == "rate_limited"
    token.assert_not_called()


def test_the_caps_are_ten_and_fifty():
    assert (call_service.MAX_IN_FLIGHT_PER_PERSON, call_service.MAX_IN_FLIGHT) == (10, 50)


@pytest.mark.parametrize("failure", [requests.Timeout("slow"), server_tokens.Waiting(), RuntimeError("a bug")],
                         ids=["provider", "token", "unexpected"])
def test_a_failed_call_releases_its_place(w, failure):
    w.client.raise_request_exception = False
    token = mock.patch.object(server_tokens, "access", side_effect=failure) \
        if not isinstance(failure, requests.Timeout) else \
        mock.patch.object(server_tokens, "access", return_value=server_tokens.Access(ACCESS, INSTANCE, "d"))
    with token, Salesforce(failure):
        assert post(w).status_code >= 500
    assert call_service._flying_total() == 0


# ---- one forced refresh a minute ------------------------------------------------------------

def test_a_burst_of_401s_makes_one_forced_refresh(w):
    dead = Answer(status=401, body=[{"errorCode": "INVALID_SESSION_ID"}])
    with FakeSF() as fake, Salesforce(dead) as sf:
        replies = [post(w) for _ in range(5)]
    assert [error_of(r) for r in replies] == ["session_ended"] * 5
    assert len(fake.token_calls()) == 2, "the first token, and one forced refresh"
    assert len(sf.calls) == 6, "one retry in the whole burst; after that a 401 is the answer"


def test_a_token_another_request_refreshed_is_not_expired(w):
    """This request's token is refused, but another has already stored a new one: use that one."""
    tokens = [_resp(body=_token_body(refresh_token=None)),
              _resp(body=_token_body(access_token="00D!access_2", refresh_token=None))]

    def another_request_refreshes():
        Connection.objects.filter(pk=w.conn.pk).update(access_expires_at=timezone.now())
        server_tokens._forget_all()
        server_tokens.access_token(w.conn)
        return Answer(status=401, body=[])

    with FakeSF(token=tokens) as fake, Salesforce(another_request_refreshes, Answer(body={"ok": True})) as sf:
        r = post(w)
    assert r.json() == {"status": 200, "body": {"ok": True}}
    assert sf.bearer(1) == "Bearer 00D!access_2"
    assert len(fake.token_calls()) == 2, "no refresh of its own"
    w.conn.refresh_from_db()
    assert w.conn.access_expires_at > timezone.now(), "the new token was left alone"
    assert server_tokens._cached(w.conn) is not None


def test_a_refused_token_that_is_not_stored_spends_no_forced_refresh(w):
    with FakeSF():
        got = server_tokens.access(w.conn)
    assert server_tokens.forget(w.conn, "some-older-digest") is True
    w.conn.refresh_from_db()
    assert w.conn.access_expires_at > timezone.now()
    assert server_tokens.forget(w.conn, got.digest) is True, "the minute's refresh is still there"
    assert server_tokens.forget(w.conn, got.digest) is False, "and now it is spent"


# ---- last used ------------------------------------------------------------------------------

def test_a_call_marks_the_connection_used_at_most_once_a_minute(w):
    with FakeSF(), Salesforce():
        post(w)
        w.conn.refresh_from_db()
        first = w.conn.last_used_at
        assert first is not None
        post(w)
    w.conn.refresh_from_db()
    assert w.conn.last_used_at == first


def test_a_refused_call_does_not_mark_it_used(w):
    post(w, path="limits", query=None)
    w.conn.refresh_from_db()
    assert w.conn.last_used_at is None


# ---- answers that cannot be passed on -------------------------------------------------------

UNREADABLE = [b'{"n": ' + str(2 ** 70).encode() + b"}", b"[" * 400 + b"]" * 400, b"[" * 200000 + b"]" * 200000]


@pytest.mark.parametrize("raw", UNREADABLE, ids=["over_64_bits", "too_deep", "deeper_than_the_parser"])
def test_an_unreadable_answer_is_a_plain_502(w, raw):
    with FakeSF(), Salesforce(Answer(raw=raw)):
        r = post(w)
    assert r.status_code == 502 and error_of(r) == "response_unreadable"
    assert "gone through" not in r.json()["detail"]


def test_an_unreadable_answer_to_a_write_says_it_may_have_landed(w):
    with FakeSF(), Salesforce(Answer(raw=UNREADABLE[0])):
        r = lead_write(w)
    assert r.status_code == 502 and error_of(r) == "response_unreadable"
    assert "may have gone through" in r.json()["detail"]
    event = Event.objects.get(kind="connections.connection_call")
    assert event.payload["status"] == 200 and event.payload["outcome"] == "unreadable"


def test_a_too_large_answer_to_a_write_does_not_invite_a_repeat(w):
    with FakeSF(), Salesforce(Answer(raw=b"x" * (call_service.MAX_RESPONSE_BYTES + 1))):
        r = lead_write(w)
    assert r.status_code == 502 and error_of(r) == "response_too_large"
    assert r.json()["detail"] == "The change may have gone through, but the answer was too large to read."


def test_kms_unavailable_sentence(w):
    with mock.patch("apps.connections.server_tokens.server_key.derive",
                    side_effect=server_key.Unavailable("arn:aws:kms:secret-looking")), Salesforce() as sf:
        r = post(w)
    assert r.status_code == 503 and error_of(r) == "try_again"
    assert "arn:" not in r.content.decode() and r.json()["detail"].endswith(".")
    assert sf.calls == []


def test_a_lease_held_elsewhere_is_a_sentence(w):
    with mock.patch.object(server_tokens, "access", side_effect=server_tokens.Waiting()), Salesforce():
        r = post(w)
    assert r.status_code == 503 and error_of(r) == "waiting"
    assert "try again" in r.json()["detail"]


# ---- audit ----------------------------------------------------------------------------------

def test_a_write_is_an_event_with_who_what_and_where(w):
    with FakeSF(), Salesforce(Answer(status=204, raw=b"")):
        assert lead_write(w).status_code == 200
    event = Event.objects.get(kind="connections.connection_call")
    assert event.workspace_id == "w"
    p = event.payload
    assert p["by"] == w.person.email and p["connection"] == str(w.conn.pk)
    assert p["surface"] == f"view:{w.view.pk}" and p["revision"] == 1 and p["operation_id"] == "op-1"
    assert p["method"] == "PATCH" and p["path"] == f"sobjects/Lead/{LEAD}" and p["status"] == 204


def test_a_refused_write_is_still_an_event(w):
    with FakeSF(), Salesforce(Answer(status=403, body=[{"errorCode": "FORBIDDEN"}])):
        assert lead_write(w).json()["status"] == 403
    assert Event.objects.get(kind="connections.connection_call").payload["status"] == 403


def test_a_write_that_ends_in_session_ended_is_still_an_event(w):
    with FakeSF(), Salesforce(Answer(status=401, body=[])):
        assert error_of(lead_write(w)) == "session_ended"
    assert Event.objects.get(kind="connections.connection_call").payload["status"] == 401


def test_a_write_never_sent_is_no_event(w):
    Connection.objects.filter(pk=w.conn.pk).update(server_use=False)
    assert error_of(lead_write(w)) == "server_use_off"
    assert not Event.objects.filter(kind="connections.connection_call").exists()


def test_a_read_is_a_log_line_not_an_event(w, caplog):
    with caplog.at_level(logging.INFO, logger="apps.connections.services"), FakeSF(), Salesforce():
        assert post(w).status_code == 200
    assert not Event.objects.filter(kind="connections.connection_call").exists()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("connections.connection_read ")]
    assert len(lines) == 1
    assert f"surface=view:{w.view.pk}" in lines[0] and "path=query" in lines[0] and "status=200" in lines[0]
    assert "SELECT" not in lines[0]


def test_no_token_in_events_or_logs(w, caplog):
    """Salesforce echoing the token, in JSON and not; a failure; a write's Event; nowhere it appears."""
    query, secret_field = "SELECT Id FROM Lead WHERE Name='private-query'", "private-body"
    with caplog.at_level(logging.DEBUG), FakeSF():
        echoes = [Answer(status=500, raw=f"<html>bad session {ACCESS}</html>".encode()),
                  Answer(status=400, body=[{"message": f"bad {ACCESS}"}]),
                  requests.ConnectionError(f"failed with Authorization: Bearer {ACCESS}")]
        replies = []
        with Salesforce(*echoes):
            replies.append(post(w, query={"q": query}))
            replies.append(lead_write(w, body={"Name": secret_field}))
            replies.append(lead_write(w, body={"Name": secret_field}))
    assert replies[0].json()["text"] and replies[1].json()["status"] == 400
    assert replies[2].status_code == 502
    events = list(Event.objects.filter(kind="connections.connection_call"))
    assert len(events) == 2
    seen = (" ".join(r.getMessage() for r in caplog.records)
            + " ".join(json.dumps(e.payload) + e.summary for e in events)
            + " ".join(r.content.decode() for r in replies))
    assert ACCESS not in seen
    assert "Authorization" not in seen
    assert "private-query" not in seen.replace(replies[0].content.decode(), "")
    assert secret_field not in seen


# ---- no transaction -------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_the_provider_call_holds_no_transaction(key):
    world = TokensWorld()
    world.conn = world.shared(sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True),
                              canopy_web=server_key.thumbprint())
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Leads", "code": CODE, "connections": [DECL]})
    world.client = Client()
    world.client.force_login(world.person)
    with FakeSF(), Salesforce() as sf:
        assert post(world).status_code == 200
    assert sf.in_atomic == [False]
