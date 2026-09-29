"""canopy's live probe of a Connected site's grant chain (`apps/tokens/live_probe.py`).

The probe must use the REAL redemption (`host_grants.redeem`) and the REAL
gateway client (`host_gateway`) — so every test here drives both against a real
host half: canopy-web as its own host (its real `/api/mcp/` behind the real DPoP
gate), and an external host built only from `canopy_sdk.host`. Then: where the
verdict is recorded, who hears about a failure, when it runs, and what real
traffic says beside it.
"""
from __future__ import annotations

from unittest import mock

import httpx2
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from canopy_sdk import contract
from canopy_sdk.host import (
    ClientKeyResolver, DPoPGate, GrantHandler, GrantRefused, HostConfig, ProbeHandler, ProbeIdentity,
    ResourceVerifier, authorization_server_metadata, delegated_principal,
)
from canopy_sdk.keys import generate_private_key, public_pem
from canopy_sdk.stores import MemoryCache, MemoryJtiStore, MemoryTokenStore
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.events.models import Event
from apps.harness.signals import sessions_reported
from apps.mcp import delegation
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.tokens import client_identity, host_gateway, live_probe, outbound, self_host
from apps.tokens.models import AppCredential, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "https://canopy.test"


def _create_probe_user():
    """What `tokens/0026_probe_user` does where a deployment names the user."""
    from importlib import import_module

    from django.apps import apps as registry

    import_module("apps.tokens.migrations.0026_probe_user").create(registry, None)


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    with override_settings(CANOPY_PUBLIC_BASE_URL=BASE, CANOPY_HOST_PROBE_USERNAME="canopy-probe"):
        _create_probe_user()
        yield
    cache.clear()


@pytest.fixture()
def owner():
    return User.objects.create_user("op", "op@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=w, role=WorkspaceMembership.OWNER)
    return w


def _in_lifespan(inner, fn):
    """Run sync `fn` while `inner`'s lifespan (its MCP session manager) is up."""
    async def main():
        async with inner.router.lifespan_context(inner):
            return await sync_to_async(fn, thread_sensitive=True)()
    return async_to_sync(main)()


# --- canopy-web as its own host (the `canopy-web` Connected site) -----------------------------


@pytest.fixture()
def self_site(owner, ws):
    app = AppCredential.create_credential(name="canopy-web", created_by=owner, workspace=ws)
    app.jwks_url = self_host.jwks_url()
    app.host_issuer = self_host.issuer()
    app.host_mcp_resource = self_host.resource()
    app.save()
    return app


@pytest.fixture()
def own_mcp(monkeypatch):
    """canopy's own MCP behind the real DPoP gate, as `config/asgi.py` mounts it."""
    inner = mcp.http_app(path="/", transport="streamable-http", stateless_http=True, json_response=True)
    app = Starlette(routes=[Mount("/api/mcp", app=delegation.gate(inner))])
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=app))
    return inner


def test_the_migration_created_a_probe_user_that_is_nobody():
    probe = User.objects.get(username="canopy-probe")
    assert probe.is_active and not probe.is_staff and not probe.is_superuser
    assert not probe.has_usable_password() and probe.email == ""
    assert not WorkspaceMembership.objects.filter(user=probe).exists()
    assert self_host.probe_user() == probe
    _create_probe_user()
    assert User.objects.filter(username="canopy-probe").count() == 1, "idempotent"


def test_the_migration_creates_nobody_where_the_probe_is_not_named():
    User.objects.filter(username="canopy-probe").delete()
    with override_settings(CANOPY_HOST_PROBE_USERNAME=""):
        _create_probe_user()
    assert not User.objects.filter(username="canopy-probe").exists()


def test_the_probe_user_never_owns_the_default_workspace():
    from apps.workspaces import services as wsvc

    Workspace.objects.filter(slug=wsvc.DEFAULT_WORKSPACE_SLUG).delete()
    assert User.objects.order_by("id").first().username == "canopy-probe"
    assert wsvc.ensure_default_workspace() is None, "the probe user alone is not a first user"
    real = User.objects.create_user("first", "first@dimagi.com", "pw")
    ws = wsvc.ensure_default_workspace()
    assert WorkspaceMembership.objects.get(workspace=ws, role=WorkspaceMembership.OWNER).user == real


def test_canopy_web_probes_itself_through_the_real_redemption_and_gateway(self_site, own_mcp):
    probe = self_host.probe_user()
    report = _in_lifespan(own_mcp, lambda: live_probe.run(self_site))
    assert report.ok is True, [(s.name, s.status, s.detail) for s in report.steps]
    assert [s.name for s in report.steps] == list(live_probe.STEPS)

    # The host half issued a real token, for the probe user, bound to canopy's DPoP key...
    from canopy_sdk.django.models import DelegatedToken as IssuedByHost

    issued = IssuedByHost.objects.get()
    assert issued.subject == str(probe.pk) and issued.scope == "insights:read"
    assert issued.cnf_jkt == client_identity.dpop_jkt()
    # ...the tool ran AS the probe user, and the out-of-scope one never ran...
    assert MCPAuditLog.objects.filter(tool="list_insights", user=probe, ok=True).exists()
    assert not MCPAuditLog.objects.filter(tool="list_items").exists()
    # ...and the probe's grant did not outlive the probe.
    assert not HostGrant.objects.exists()
    # Probe traffic is not real traffic.
    assert not Event.objects.filter(source="tokens.host_grants").exists()


def test_the_probe_is_not_set_up_until_the_probe_user_is_named(self_site, own_mcp):
    with override_settings(CANOPY_HOST_PROBE_USERNAME=""):
        report = live_probe.run(self_site)
    assert report.ok is None and report.steps[0].status == "skip"
    assert "no probe identity" in report.steps[0].detail


def test_the_self_probe_endpoint_is_public_404s_off_and_refuses_other_clients(self_site):
    c = Client()
    with override_settings(REQUIRE_AUTH=True):
        r = c.post("/oauth/probe", {"client_id": "https://evil.test/oauth/client.json"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client", "public, and self-enforcing"
    with override_settings(CANOPY_HOST_PROBE_USERNAME=""):
        assert c.post("/oauth/probe", {}).status_code == 404
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta[contract.PROBE_ENDPOINT_METADATA_FIELD] == f"{BASE}/oauth/probe"


def test_the_self_probe_endpoint_never_issues_for_another_principal(self_site):
    creds = client_identity.credentials()
    form = {"client_id": creds.client_id, "client_assertion_type": contract.CLIENT_ASSERTION_TYPE,
            "client_assertion": creds.client_assertion(BASE), "subject": str(User.objects.first().pk)}
    r = Client().post("/oauth/probe", form,
                      headers={"DPoP": creds.dpop_proof("POST", self_host.probe_endpoint())})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


# --- an external host built only from the SDK ---------------------------------------------------

ISSUER = "https://host.test"
RESOURCE = "https://host.test/mcp/"
TOKEN_ENDPOINT = "https://host.test/o/token/"
PROBE_ENDPOINT = "https://host.test/canopy/probe/"
PROBE_SUBJECT = "probe-7"


class Host:
    """A host with a probe identity. `scope_leak=True` makes its MCP run a tool
    outside the token's scope — the failure (b) exists to catch."""

    def __init__(self, *, probe=True, scope_leak=False):
        self.key = generate_private_key("EdDSA")
        self.config = HostConfig(
            signing_key=self.key, canopy_base_url=BASE, app_name="connect-labs",
            issuer=ISSUER, resource=RESOURCE, token_endpoint=TOKEN_ENDPOINT,
            canopy_client_id=client_identity.client_id(),
            scope_tools={"marketplace:read": {"marketplace_rounds_list"}, "admin:write": {"org_delete"}},
            probe=ProbeIdentity(endpoint=PROBE_ENDPOINT, subject=PROBE_SUBJECT, scope="marketplace:read",
                                tool="marketplace_rounds_list", arguments={"limit": 1},
                                denied_tool="org_delete") if probe else None)
        self.tokens = MemoryTokenStore()
        jtis = MemoryJtiStore()
        keys = ClientKeyResolver(fetch_json=self._fetch_canopy, cache=MemoryCache())
        active = {PROBE_SUBJECT}.__contains__
        self.grants = GrantHandler(self.config, jti_store=jtis, token_store=self.tokens, client_keys=keys,
                                   subject_active=active)
        self.prober = ProbeHandler(self.config, jti_store=jtis, client_keys=keys, subject_active=active)
        self.verifier = ResourceVerifier(self.config, token_store=self.tokens, replay_store=MemoryJtiStore())
        self.ran: list[tuple[str, str]] = []

        server = FastMCP("host")
        host = self

        class Scope(Middleware):
            async def on_list_tools(self, context, call_next):
                tools = await call_next(context)
                principal = delegated_principal.get()
                return [t for t in tools if principal is not None and principal.allows(t.name)]

            async def on_call_tool(self, context, call_next):
                principal = delegated_principal.get()
                if not scope_leak and (principal is None or not principal.allows(context.message.name)):
                    from fastmcp.exceptions import ToolError

                    raise ToolError("not within this grant")
                return await call_next(context)

        server.add_middleware(Scope())

        @server.tool
        def marketplace_rounds_list(limit: int = 10) -> list:
            principal = delegated_principal.get()
            host.ran.append(("marketplace_rounds_list", principal.subject if principal else ""))
            return []

        @server.tool
        def org_delete() -> str:
            host.ran.append(("org_delete", ""))
            return "deleted"

        self.inner = server.http_app(path="/mcp/", stateless_http=True, json_response=True)

    def _fetch_canopy(self, url):
        assert url in (client_identity.client_id(), client_identity.jwks_uri())
        from urllib.parse import urlsplit

        return Client().get(urlsplit(url).path).json()

    def get_json(self, url, *, what="URL"):
        assert url == contract.metadata_url(ISSUER), url
        return authorization_server_metadata(self.config)

    def post_form(self, url, data, *, headers, what="URL"):
        handler = {TOKEN_ENDPOINT: self.grants.handle, PROBE_ENDPOINT: self.prober.handle}[url]
        try:
            return 200, handler(dict(data), headers.get(contract.DPOP_HEADER)).body(), {}
        except GrantRefused as refused:
            return refused.status, refused.body(), refused.headers()


def _host(monkeypatch, **kwargs):
    h = Host(**kwargs)
    monkeypatch.setattr(outbound, "get_json", h.get_json)
    monkeypatch.setattr(outbound, "post_form", h.post_form)
    monkeypatch.setattr(outbound, "check_url", lambda url, what="URL": url)
    monkeypatch.setattr(host_gateway, "_transport_override",
                        httpx2.ASGITransport(app=DPoPGate(h.inner, h.verifier, require_principal=True)))
    return h


@pytest.fixture()
def site(owner, ws):
    return _site(owner, ws)


def _site(owner, ws, host=None):
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.host_issuer = ISSUER
    app.host_mcp_resource = RESOURCE
    app.save()
    return app


def _register(app, host):
    app.public_keys = [public_pem(host.key)]
    app.save()


def test_an_sdk_host_passes_the_whole_live_chain(site, monkeypatch):
    host = _host(monkeypatch)
    _register(site, host)
    report = _in_lifespan(host.inner, lambda: live_probe.run(site))
    assert report.ok is True, [(s.name, s.status, s.detail) for s in report.steps]
    assert host.ran == [("marketplace_rounds_list", PROBE_SUBJECT)], "only the probe tool ran, as the probe user"
    [issued] = host.tokens._tokens.values()
    assert issued.subject == PROBE_SUBJECT and issued.scopes == ("marketplace:read",)
    assert not HostGrant.objects.exists()


def test_a_host_that_runs_an_out_of_scope_tool_fails_b(site, monkeypatch):
    host = _host(monkeypatch, scope_leak=True)
    _register(site, host)
    report = _in_lifespan(host.inner, lambda: live_probe.run(site))
    assert report.ok is False and report.failing.name == "out_of_scope_refused"
    assert "RAN" in report.failing.detail


def test_a_grant_signed_by_a_key_canopy_did_not_register_fails_at_redemption(site, monkeypatch):
    host = _host(monkeypatch)
    site.public_keys = [public_pem(generate_private_key("EdDSA"))]
    site.save()
    report = live_probe.run(site)
    assert report.ok is False and report.failing.name == "live_grant_redeemed"
    assert "bad_signature" in report.failing.detail
    assert not host.tokens._tokens, "canopy checked first and spent nothing"


def test_a_host_without_a_probe_is_not_set_up_rather_than_failing(site, monkeypatch):
    host = _host(monkeypatch, probe=False)
    _register(site, host)
    report = live_probe.run(site)
    assert report.ok is None and report.skipped.name == "probe_issued"


# --- recording, events, notification -------------------------------------------------------------


def _report(ok: bool | None, step="probe_tool_succeeds", detail="host_unreachable: down"):
    r = live_probe.ProbeReport()
    r.add("probe_issued", "pass")
    if ok is True:
        r.add(step, "pass")
    elif ok is False:
        r.add(step, "fail", detail)
    else:
        r.steps = [live_probe.Step("probe_issued", "skip", "no probe identity")]
    return r


def test_a_failure_is_recorded_evented_and_pushed_once_then_recovery_too(site, owner):
    with mock.patch("apps.push.services.send_to_user", return_value=1) as push:
        live_probe.record(site, _report(False))
        site.refresh_from_db()
        assert site.last_probe_ok is False and site.last_probe_step == "probe_tool_succeeds"
        assert "host_unreachable" in site.last_probe_reason and site.last_probe_at
        assert push.call_count == 1 and push.call_args.args[0] == owner
        assert push.call_args.args[3] == "/w/connect/settings/connected-apps"

        # Still failing: the Event coalesces, nobody is pushed again.
        live_probe.record(site, _report(False))
        failed = Event.objects.get(kind="host_probe.failed")
        assert failed.count == 2 and failed.level == "error" and failed.workspace_id == "connect"
        assert push.call_count == 1

        live_probe.record(site, _report(True))
        site.refresh_from_db()
        assert site.last_probe_ok is True and site.last_probe_step == "" and site.last_probe_reason == ""
        assert Event.objects.filter(kind="host_probe.recovered").count() == 1
        assert push.call_count == 2 and "working again" in push.call_args.args[1]


def test_not_set_up_is_neither_a_failure_nor_a_recovery(site):
    with mock.patch("apps.push.services.send_to_user", return_value=1) as push:
        live_probe.record(site, _report(None))
    site.refresh_from_db()
    assert site.last_probe_ok is None and site.last_probe_step == "probe_issued"
    assert not Event.objects.filter(source=live_probe.EVENT_SOURCE).exists() and not push.called


def test_only_workspace_owners_are_pushed(site, ws):
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    with mock.patch("apps.push.services.send_to_user", return_value=1) as push:
        live_probe.record(site, _report(False))
    assert [c.args[0].username for c in push.call_args_list] == ["op"]


# --- on demand only -----------------------------------------------------------------------------------


def test_nothing_probes_on_a_clock():
    # It swept every 30 min per site off runner reports until 2026-09-29; it is
    # on demand now (Test connection). Every probe mints a real grant and calls
    # a real host, so nothing may start one that a person did not ask for.
    with mock.patch.object(live_probe, "probe_and_record") as probe:
        sessions_reported.send(sender=None, runner=mock.Mock(paired_by_id=None))
    assert not probe.called
    assert not hasattr(live_probe, "sweep")


def test_a_probe_already_running_for_a_site_is_not_started_twice(site):
    cache.add(f"tokens:live_probe:{site.pk}", 1, 60)
    assert live_probe.probe_and_record(site) is None


# --- real traffic -----------------------------------------------------------------------------------


def test_traffic_health_reads_what_canopy_already_records(site, ws, owner):
    from apps.events import services as events

    empty = live_probe.traffic_health(site)
    assert empty.last_redeemed_at is None and empty.last_site_call_at is None and empty.refusals_24h == 0

    events.record([{"source": "tokens.host_grants", "kind": "host_grant.redeemed", "key": "",
                    "payload": {"site": site.name}},
                   {"source": "tokens.host_grants", "kind": "host_grant.refused", "key": f"{site.pk}:x",
                    "payload": {"site": site.name}},
                   # another site's refusal is not this one's
                   {"source": "tokens.host_grants", "kind": "host_grant.refused", "key": "9:x",
                    "payload": {"site": "other"}}], workspace=ws)
    MCPAuditLog.objects.create(user=owner, tool="site_call", ok=True,
                               args_summary=f"turn=t site={site.name} app={site.pk} tool=x is_error=False")
    MCPAuditLog.objects.create(user=owner, tool="site_call", ok=False, error="not_allowed",
                               args_summary=f"turn=t site={site.name} app={site.pk} tool=y")
    MCPAuditLog.objects.create(user=owner, tool="site_call", ok=True,
                               args_summary=f"turn=t site={site.name} app={site.pk + 1000} tool=x "
                                            "is_error=False")
    health = live_probe.traffic_health(site)
    assert health.last_redeemed_at is not None and health.last_site_call_at is not None
    assert health.refusals_24h == 2


# --- the API ------------------------------------------------------------------------------------------


def test_test_connection_runs_the_live_probe_and_the_table_shows_it(site, owner, monkeypatch):
    host = _host(monkeypatch)
    _register(site, host)
    c = Client()
    c.force_login(owner)
    monkeypatch.setattr("apps.tokens.connection_test.run", lambda app: [])
    r = _in_lifespan(host.inner, lambda: c.post(f"/api/workspaces/connect/connected-apps/{site.pk}/test"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["live_probe_ok"] is True and body["ok"] is True
    assert [s["name"] for s in body["live_probe"]] == list(live_probe.STEPS)

    [row] = c.get("/api/workspaces/connect/connected-apps").json()
    assert row["live_probe"]["ok"] is True and row["live_probe"]["at"]
    assert set(row["traffic"]) == {"last_redeemed_at", "last_site_call_at", "refusals_24h"}


def test_a_failing_live_probe_fails_test_connection(site, owner, monkeypatch):
    host = _host(monkeypatch, scope_leak=True)
    _register(site, host)
    c = Client()
    c.force_login(owner)
    monkeypatch.setattr("apps.tokens.connection_test.run", lambda app: [])
    body = _in_lifespan(host.inner, lambda: c.post(
        f"/api/workspaces/connect/connected-apps/{site.pk}/test")).json()
    assert body["ok"] is False and body["live_probe_ok"] is False
    row = c.get("/api/workspaces/connect/connected-apps").json()[0]
    assert row["live_probe"]["step"] == "out_of_scope_refused"
    assert row["live_probe"]["step_label"] == live_probe.STEPS["out_of_scope_refused"]
