"""A host site's surfaces (`apps/host_grants/surfaces.py`, docs/architecture/host-sites.md
§ Surfaces): one site, signed with one key, whose parts serve different people.

Connect Labs is the case: its pages are the site itself (staff, act-as-you grants, sensitive)
and `workers` is a surface of it (Connect Workers over PersonalID, not sensitive). What these
pin, each as a refusal where it can be one:

* a contact holds a conversation only on a surface the site declares not sensitive, through
  an entry that names it, and that conversation is not sensitive;
* a contact's subject-bound calls go to its surface's resource, never the site's own, so a
  contact never reaches the sensitive surface or its tools;
* a relay token never resolves to an account, on a surface as anywhere.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from canopy_sdk import contract
from canopy_sdk import site as wire
from canopy_sdk.host import verify_subject
from django.contrib.auth.models import User
from django.test import Client
from fastmcp import FastMCP

from apps.agents.published_profiles import parse, surface_of
from apps.canopy_sessions.models import Session
from apps.embedding import assertions
from apps.harness import sensitivity
from apps.harness.models import Turn
from apps.host_grants import site_needs, surfaces
from apps.tokens.models import AppCredential
from apps.workspaces.models import WorkspaceMembership
from tests.access_helpers import declare
from tests.fixtures import site_gateway as sg
from tests.fixtures.contact_websocket import _world

pytestmark = pytest.mark.django_db

WORKERS_RESOURCE = "https://host.test/workers/mcp/"
WORKERS = {"sensitive": False, "mcp_resource": WORKERS_RESOURCE, "person_scheme": "labs-worker"}


def _document(monkeypatch, surfaces_=None, name="connect-labs"):
    """The site's verified site.json, as `site_needs.needs_for` would return it."""
    doc = wire.SiteNeeds.from_claims(wire.site_document(
        name, sensitive_data={"requires": {"handles_sensitive": True, "runner_flag": "sensitive_data"}},
        surfaces={"workers": WORKERS} if surfaces_ is None else surfaces_))
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", doc))
    return doc


@pytest.fixture()
def no_document(monkeypatch):
    monkeypatch.setattr(site_needs, "needs_for",
                        lambda app, **kw: site_needs.Needs("x", None, reason="unreachable"))


# --- the entry names its surface ----------------------------------------------------------


def test_the_entry_names_a_surface_strictly_and_only_beside_a_site():
    caps = parse({"capabilities": {"c": {"sites": ["s"], "surface": "workers"}}})["capabilities"]["c"]
    assert caps["surface"] == "workers"
    assert "surface" not in parse({"capabilities": {"c": {"sites": ["s"]}}})["capabilities"]["c"]
    for bad in ({"sites": ["s"], "surface": "Workers"}, {"sites": ["s"], "surface": 3},
                {"surface": "workers"}):
        with pytest.raises(Exception):  # noqa: B017 - InterfaceError
            parse({"capabilities": {"c": bad}})


def test_an_admin_sets_and_clears_it_through_the_host_site_route():
    owner, ws, app, priv = _world()
    agent = app.allowed_agents.get().agent
    declare(agent, {"labs": {"sites": ["connect-labs"]}})
    c = Client()
    c.force_login(owner)
    r = c.put(f"/api/agents/{agent.slug}/host-sites/connect-labs",
              {"contact_scoped": True, "surface": "workers"}, content_type="application/json")
    assert r.status_code == 200, r.content
    row = next(x for x in r.json()["sites"] if x["site"] == "connect-labs")
    assert row["surface"] == "workers" and r.json()["new_site_defaults"]["surface"] == ""
    agent.refresh_from_db()
    assert surface_of(agent, "connect-labs") == "workers"
    r = c.put(f"/api/agents/{agent.slug}/host-sites/connect-labs", {"surface": ""},
              content_type="application/json")
    agent.refresh_from_db()
    assert r.status_code == 200 and surface_of(agent, "connect-labs") == ""


# --- a contact's conversation: only on a surface that is not sensitive --------------------


def _arrive(priv, *, sub="personalid:ana", requirements=None, channel="personalid", email=""):
    now = int(dt.datetime.now(dt.UTC).timestamp())
    claims = {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(), "iat": now,
              "exp": now + 60, "jti": str(uuid.uuid4()), "canopy_agent": "echo"}
    if channel:
        claims[contract.CHANNEL_CLAIM] = channel
    if requirements:
        claims[contract.RUNNER_REQUIREMENTS_CLAIM] = requirements
    if email:
        claims.update(email=email, email_verified=True)
    r = Client().post("/api/auth/contact-token",
                      {"assertion": jwt.encode(claims, priv, algorithm="EdDSA"), "agent_slug": "echo"},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


@pytest.fixture()
def site(monkeypatch):
    owner, ws, app, priv = _world()
    agent = app.allowed_agents.get().agent
    declare(agent, {"workers": {"sites": ["connect-labs"], "contact_scoped": True, "surface": "workers",
                                "tools": ["Read"]}})
    _document(monkeypatch)
    return {"owner": owner, "ws": ws, "app": app, "priv": priv, "agent": agent}


def _start(token, **body):
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}").post(
        "/api/contact/sessions", {"agent_slug": "echo", **body}, content_type="application/json")


def test_a_contact_holds_a_conversation_on_a_surface_the_site_declares_not_sensitive(site):
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 200, r.content
    session = Session.objects.get(pk=r.json()["id"])
    assert session.metadata[surfaces.SESSION_KEY] == "workers"
    assert not sensitivity.is_sensitive(session)


def test_an_arrival_requiring_sensitive_data_runners_is_refused_on_that_surface(site):
    """The staff pages' arrivals require sensitive-data runners: on the workers surface such an
    arrival is refused, never served with the requirement dropped."""
    r = _start(_arrive(site["priv"], requirements=["sensitive_data"])["token"])
    assert r.status_code == 422 and "surface_mismatch" in r.json()["detail"]
    assert not Session.objects.exists()


def test_a_surface_the_site_declares_sensitive_is_refused(site, monkeypatch):
    _document(monkeypatch, {"workers": {**WORKERS, "sensitive": True}})
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 422 and "surface_sensitive" in r.json()["detail"]
    assert not Session.objects.exists()


@pytest.mark.parametrize("declared", [{}, {"staff": {"sensitive": False}}])
def test_a_surface_the_site_does_not_declare_is_refused(site, monkeypatch, declared):
    _document(monkeypatch, declared)
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 422 and "surface_unknown" in r.json()["detail"]


def test_with_no_verifiable_site_json_a_surface_is_refused(site, no_document):
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 422 and "surface_unknown" in r.json()["detail"]


def test_a_contact_scoped_entry_naming_no_surface_on_a_surfaced_site_is_refused(site):
    declare(site["agent"], {"workers": {"sites": ["connect-labs"], "contact_scoped": True, "tools": ["Read"]}})
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 422 and "no_surface" in r.json()["detail"]


def test_the_site_itself_keeps_its_sensitivity(site):
    """On the site itself (no surface), a contact arriving under the site's sensitive-data
    requirement still cannot hold a conversation, as before surfaces."""
    declare(site["agent"], {})
    r = _start(_arrive(site["priv"], requirements=["sensitive_data"], channel="")["token"])
    assert r.status_code == 422 and not Session.objects.exists()


def test_an_agent_serving_the_sites_pages_is_never_offered_to_its_contacts(site):
    declare(site["agent"], {"labs": {"sites": ["connect-labs"], "surface": "workers", "tools": ["Read"]}})
    r = _start(_arrive(site["priv"])["token"])
    assert r.status_code == 404


def test_the_host_cannot_stamp_a_surface_itself(site):
    declare(site["agent"], {})  # on the site itself, no entry: offered to contacts as before
    r = _start(_arrive(site["priv"])["token"], metadata={surfaces.SESSION_KEY: "workers", "x_note": "hi"})
    assert r.status_code == 200, r.content
    session = Session.objects.get(pk=r.json()["id"])
    assert surfaces.SESSION_KEY not in session.metadata and session.metadata["x_note"] == "hi"


def test_a_relay_token_never_resolves_to_an_account(site):
    """A member of the workspace, vouched for by the site's verified email, still arrives over
    the relay as the CONTACT: a relay never holds a user's token."""
    member = User.objects.create_user("ana", "ana@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=site["ws"], role=WorkspaceMembership.EDITOR)
    minted = _arrive(site["priv"], email="ana@dimagi.com")
    assert minted["kind"] == "contact"
    r = _start(minted["token"])
    assert r.status_code == 200 and Session.objects.get(pk=r.json()["id"]).created_by is None


# --- a contact's subject-bound calls: to its surface, never the site's pages --------------


class WorkersHost(sg.HostMCP):
    """The site's workers surface: one tool, at its own resource, every request verified with
    the SDK against a config whose `resource` is the SURFACE's."""

    def __init__(self):
        super().__init__()
        from canopy_sdk.host import HostConfig
        from canopy_sdk.keys import generate_private_key

        config, self.keys, self.jtis = sg.host_side()
        self.config = HostConfig(signing_key=generate_private_key("EdDSA"), resource=WORKERS_RESOURCE,
                                 canopy_client_id=config.canopy_client_id)
        self.subjects: list[str] = []
        self.paths: list[str] = []
        server = FastMCP("workers")

        @server.tool
        def worker_profile() -> dict:
            self.calls.append("worker_profile")
            return {"stage": "deliver"}

        self.inner = server.http_app(path="/workers/mcp/", stateless_http=True, json_response=True)

    def _check(self, scope, headers) -> str:
        self.paths.append(scope["path"])
        try:
            subject = verify_subject(headers, config=self.config, client_keys=self.keys,
                                     replay_store=self.jtis, method=scope["method"])
        except Exception as exc:  # noqa: BLE001 - refused, and said why
            return f"refused {getattr(exc, 'code', exc)}"
        self.subjects.append(subject.sub)
        return ""


@pytest.fixture()
def host(monkeypatch):
    import httpx2

    from apps.host_grants import host_gateway

    h = WorkersHost()
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=h.app))
    return h


ENTRY = {"connect": {"sites": ["connect-labs"], "contact_scoped": True, "surface": "workers",
                     "tools": ["mcp__*canopy-web__who_is_asking"]}}


@pytest.fixture()
def w(monkeypatch):
    """The world's conversation as one of the site's own contacts, held on its workers surface."""
    w = sg.world()
    declare(w["agent"], ENTRY)
    _document(monkeypatch)
    session = w["session"]
    session.created_by, session.contact = None, w["contact"]
    session.metadata = {**session.metadata, surfaces.SESSION_KEY: "workers"}
    session.save()
    Turn.objects.filter(pk=w["turn"].pk).update(initiator_user=None, initiator_contact=w["contact"],
                                                initiator_kind="contact")
    return w


def _as_contact(w):
    return sg.as_token(sg.caller_tokens.mint(Turn.objects.get(pk=w["turn"].pk)))


def _refused(w, host, match):
    with _as_contact(w), pytest.raises(Exception, match=match):
        sg.call(host, "site_call", {"tool": "worker_profile"})
    assert host.calls == [] and host.subjects == [], "a refused call must not reach the host by any route"


def test_a_contacts_call_goes_to_its_surfaces_resource_bound_to_that_contact(w, host):
    with _as_contact(w):
        out = sg.call(host, "site_call", {"tool": "worker_profile", "arguments": {}}).structured_content
    assert out["is_error"] is False and host.calls == ["worker_profile"]
    assert set(host.subjects) == {"u-42"} and set(host.paths) == {"/workers/mcp/"}
    assert w["app"].host_mcp_resource == sg.RESOURCE, "the site's own resource is never the target"


def test_a_surfaces_tools_stay_out_of_the_sites_index(w, host):
    with _as_contact(w):
        sg.call(host, "site_tools", {})
    w["app"].refresh_from_db()
    assert not (w["app"].mcp_apps_index or {}).get("tools")


def test_a_contact_scoped_entry_with_no_surface_never_reaches_the_sites_own_tools(w, host):
    declare(w["agent"], {"connect": {**ENTRY["connect"], "surface": ""}})
    Session.objects.filter(pk=w["session"].pk).update(metadata={"embed_app": "connect-labs"})
    _refused(w, host, "surface of its own")


def test_a_surface_now_declared_sensitive_is_refused(w, host, monkeypatch):
    _document(monkeypatch, {"workers": {**WORKERS, "sensitive": True}})
    _refused(w, host, "sensitive data")


def test_a_surface_no_longer_declared_is_refused(w, host, monkeypatch):
    _document(monkeypatch, {})
    _refused(w, host, "declares no surface")


def test_no_verifiable_site_json_is_refused(w, host, no_document):
    _refused(w, host, "cannot read")


def test_a_surface_resource_off_the_sites_registered_origin_is_refused(w, host, monkeypatch):
    _document(monkeypatch, {"workers": {**WORKERS, "mcp_resource": "https://elsewhere.test/workers/mcp/"}})
    _refused(w, host, "off the origin")


def test_a_surface_with_no_resource_is_refused(w, host, monkeypatch):
    _document(monkeypatch, {"workers": {"sensitive": False}})
    _refused(w, host, "names no MCP resource")


def test_an_entry_moved_to_another_surface_is_refused_in_an_older_conversation(w, host, monkeypatch):
    _document(monkeypatch, {"workers": WORKERS, "coaching": WORKERS})
    declare(w["agent"], {"connect": {**ENTRY["connect"], "surface": "coaching"}})
    _refused(w, host, "held on")


def test_a_site_with_no_registered_resource_is_refused(w, host):
    AppCredential.objects.filter(pk=w["app"].pk).update(host_mcp_resource="")
    _refused(w, host, "does not let me reach its tools")


# --- the site as the system of record: a surface's person scheme -------------------------


def test_a_record_uri_may_be_under_a_surfaces_person_scheme(site):
    app = site["app"]
    assert surfaces.person_scheme_for(app, "labs-worker://ana") == "labs-worker"
    # Nothing at the site level and nothing that matches: the surface's, so the refusal names it.
    assert surfaces.person_scheme_for(app, "other://ana") == "labs-worker"
