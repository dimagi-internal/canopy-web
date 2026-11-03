"""What a view or app page declares it may ask a connection to do, and the check
that a call is inside it. Checked when saved and again at run time."""
# ruff: noqa: E501
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.connections import calls, declarations
from apps.dynamic_views.models import DynamicView
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services as app_services
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

J = "application/json"
LEAD = "00Q5g00000AbCdEFGH"
CODE = "function View({ canopy }) { return <div/>; }"

READ_QUERY = {"method": "GET", "path": "query"}
WRITE_LEAD = {"method": "PATCH", "path": "sobjects/Lead/*"}
DECL = {"provider": "salesforce", "as": "viewer", "reads": [READ_QUERY], "writes": [WRITE_LEAD]}


@pytest.fixture()
def world(db):
    ws = a_workspace("family")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    bot = a_member(ws, email="bailey@marshellis.com", role=M.VIEWER)
    Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=owner, user=bot)
    return dict(ws=ws, editor=editor)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _create(client, **body):
    body = {"workspace": "family", "title": "Leads", "code": CODE, **body}
    return client.post("/api/dynamic-views/", json.dumps(body), content_type=J)


def _m(method, path):
    return calls.match("salesforce", method, path)


# --- saving a view ---------------------------------------------------------------------


def test_valid_declaration_saved_and_snapshotted(world):
    c = _client(world["editor"])
    r = _create(c, connections=[DECL])
    assert r.status_code == 201, r.content
    assert r.json()["connections"] == [DECL]
    view = DynamicView.objects.get(pk=r.json()["id"])
    assert view.connections == [DECL]
    assert view.revisions.get(version=1).connections == [DECL]
    assert c.get(f"/api/dynamic-views/{view.pk}").json()["connections"] == [DECL]
    # A view that declares nothing declares nothing.
    assert _create(c, title="Plain").json()["connections"] == []


@pytest.mark.parametrize("declared, why", [
    ([{**DECL, "writes": [{"method": "POST", "path": "composite"}]}], "composite"),
    ([{**DECL, "reads": [{"method": "GET", "path": "../x"}]}], "path"),
    ([{**DECL, "reads": [{"method": "GET", "path": "sobjects/Lead/*/../*"}]}], "path"),
    ([{**DECL, "writes": [{"method": "PUT", "path": "sobjects/Lead/*"}]}], "method"),
    # A write of a refused object is refused at save, naming it.
    ([{**DECL, "writes": [{"method": "PATCH", "path": "sobjects/User/*"}]}], "sobjects/User/*"),
    ([{**DECL, "writes": [{"method": "POST", "path": "sobjects/ApexClass"}]}], "sobjects/ApexClass"),
    # A read declared with a write method.
    ([{**DECL, "reads": [{"method": "PATCH", "path": "sobjects/Lead/*"}]}], "read"),
    ([{**DECL, "reads": [{"method": "POST", "path": "sobjects/Lead"}]}], "read"),
    # A write that is really a read, and the other way round.
    ([{**DECL, "writes": [{"method": "GET", "path": "query"}]}], "write"),
    # No wildcard in the type position of a write, nor for a flow or a class.
    ([{**DECL, "writes": [{"method": "PATCH", "path": "sobjects/*/*"}]}], "exact"),
    ([{**DECL, "writes": [{"method": "POST", "path": "sobjects/*"}]}], "exact"),
    ([{**DECL, "writes": [{"method": "POST", "path": "actions/custom/flow/*"}]}], "exact"),
    ([{**DECL, "writes": [{"method": "POST", "path": "apexrest/*"}]}], "exact"),
    ([{**DECL, "provider": "google"}], "Google"),
    ([{**DECL, "provider": "nope"}], "cannot be called"),
    ([{**DECL, "as": "somebody"}], "viewer"),
    ([{**DECL, "extra": 1}], "extra"),
    ([{**DECL, "reads": [{"method": "GET", "path": "query", "x": 1}]}], "method and path"),
    ([{**DECL, "agent": "bailey"}], "agent"),
    ([DECL, DECL], "twice"),
    ("salesforce", "list"),
])
def test_declaration_outside_allowlist_refused(world, declared, why):
    r = _create(_client(world["editor"]), connections=declared)
    assert r.status_code == 422 and why in r.content.decode(), r.content


def test_shared_needs_agent(world):
    c = _client(world["editor"])
    shared = {**DECL, "as": "shared"}
    r = _create(c, connections=[shared])
    assert r.status_code == 422 and "agent" in r.content.decode()
    r = _create(c, connections=[{**shared, "agent": "nobody"}])
    assert r.status_code == 422 and "no agent 'nobody'" in r.content.decode()
    r = _create(c, connections=[{**shared, "agent": "../x"}])
    assert r.status_code == 422 and "agent" in r.content.decode()
    ok = _create(c, connections=[{**shared, "agent": "bailey"}])
    assert ok.status_code == 201, ok.content
    assert ok.json()["connections"][0]["agent"] == "bailey"


def test_limits():
    five = [{"provider": "salesforce", "as": "shared", "agent": f"a{i}", "reads": [], "writes": []} for i in range(5)]
    assert len(declarations.validate(five)) == 5
    with pytest.raises(ValueError, match="at most 5"):
        declarations.validate([*five, {**DECL}])
    lines = [{"method": "GET", "path": f"sobjects/T{i}"} for i in range(50)]
    assert declarations.validate([{**DECL, "reads": lines, "writes": []}])
    with pytest.raises(ValueError, match="at most 50"):
        declarations.validate([{**DECL, "reads": lines, "writes": [WRITE_LEAD]}])
    with pytest.raises(ValueError, match="twice"):
        declarations.validate([{**DECL, "reads": [READ_QUERY, READ_QUERY]}])


def test_revision_keeps_old_declaration(world):
    c = _client(world["editor"])
    view_id = _create(c, connections=[DECL]).json()["id"]
    narrower = {**DECL, "writes": []}
    r = c.patch(f"/api/dynamic-views/{view_id}", json.dumps({"connections": [narrower]}), content_type=J)
    assert r.status_code == 200 and r.json()["connections"] == [narrower]
    revs = {x["version"]: x for x in c.get(f"/api/dynamic-views/{view_id}/revisions/").json()}
    assert revs[1]["connections"] == [DECL] and revs[2]["connections"] == [narrower]


def test_rollback_restores_declaration(world):
    c = _client(world["editor"])
    view_id = _create(c, connections=[DECL]).json()["id"]
    c.patch(f"/api/dynamic-views/{view_id}", json.dumps({"connections": []}), content_type=J)
    r = c.post(f"/api/dynamic-views/{view_id}/rollback", json.dumps({"version": 1}), content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["version"], r.json()["connections"]) == (3, [DECL])


# --- an app's manifest -----------------------------------------------------------------


def test_app_manifest_connections_validated():
    raw = app_services._read_bundled("hiring")
    assert mf.validate(raw)["connections"] == []
    raw["connections"] = [DECL]
    assert mf.validate(raw)["connections"] == [DECL]
    # The agent's slug is checked for shape only: there is no workspace to resolve it in.
    raw["connections"] = [{**DECL, "as": "shared", "agent": "bailey"}]
    assert mf.validate(raw)["connections"][0]["agent"] == "bailey"
    raw["connections"] = [{**DECL, "as": "shared", "agent": "../x"}]
    with pytest.raises(ValueError, match="connections"):
        mf.validate(raw)
    raw["connections"] = [{**DECL, "writes": [{"method": "PATCH", "path": "sobjects/User/*"}]}]
    with pytest.raises(ValueError, match="connections.*sobjects/User/"):
        mf.validate(raw)


# --- the run-time check ----------------------------------------------------------------


def test_allows_read_and_write_pattern():
    declared = declarations.validate([DECL, {"provider": "salesforce", "as": "shared", "agent": "bailey",
                                             "reads": [{"method": "GET", "path": "sobjects/Account/*"}],
                                             "writes": []}])
    assert declarations.allows(declared, "salesforce", "viewer", None, _m("GET", "query"), "GET")
    assert declarations.allows(declared, "salesforce", "viewer", None, _m("PATCH", f"sobjects/Lead/{LEAD}"), "PATCH")
    # Method spelling does not matter; the canonical rule's method does.
    assert declarations.allows(declared, "salesforce", "viewer", None, _m("patch", f"sobjects/Lead/{LEAD}"), "patch")
    # One declared entry per (provider, as, agent): shared/bailey has its own lines.
    assert declarations.allows(declared, "salesforce", "shared", "bailey", _m("GET", f"sobjects/Account/{LEAD}"), "GET")
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("GET", f"sobjects/Account/{LEAD}"), "GET")
    assert not declarations.allows(declared, "salesforce", "shared", "other", _m("GET", f"sobjects/Account/{LEAD}"), "GET")


def test_allows_refuses_undeclared_write():
    declared = declarations.validate([{**DECL, "writes": [WRITE_LEAD]}])
    # Another object, another verb, a read-only declaration of the same path.
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("PATCH", f"sobjects/Contact/{LEAD}"), "PATCH")
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("DELETE", f"sobjects/Lead/{LEAD}"), "DELETE")
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("POST", "sobjects/Lead"), "POST")
    # A write is never satisfied by a declared read, even of the same path.
    reads_only = declarations.validate([{**DECL, "reads": [{"method": "GET", "path": "sobjects/Lead/*"}], "writes": []}])
    assert not declarations.allows(reads_only, "salesforce", "viewer", None, _m("PATCH", f"sobjects/Lead/{LEAD}"), "PATCH")
    # A different depth does not match a one-segment `*`.
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("PATCH", f"sobjects/Lead/{LEAD}"), "GET")
    # Nothing declared, nothing allowed; junk in place of a match is refused.
    assert not declarations.allows([], "salesforce", "viewer", None, _m("GET", "query"), "GET")
    assert not declarations.allows(declared, "salesforce", "viewer", None, "sobjects/Lead/x", "PATCH")  # type: ignore[arg-type]


def test_a_star_matches_exactly_one_segment_for_reads():
    declared = declarations.validate([{**DECL, "writes": [], "reads": [
        {"method": "GET", "path": "sobjects/*/describe"}, {"method": "GET", "path": "analytics/reports/*/describe"}]}])
    assert declarations.allows(declared, "salesforce", "viewer", None, _m("GET", "sobjects/Lead/describe"), "GET")
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("GET", f"sobjects/Lead/{LEAD}"), "GET")
    assert declarations.allows(declared, "salesforce", "viewer", None,
                               _m("GET", f"analytics/reports/{LEAD}/describe"), "GET")


def test_running_a_report_is_a_declarable_read():
    declared = declarations.validate([{**DECL, "writes": [], "reads": [
        {"method": "POST", "path": "analytics/reports/*"}, {"method": "POST", "path": "analytics/reports/*/instances"}]}])
    assert declarations.allows(declared, "salesforce", "viewer", None, _m("POST", f"analytics/reports/{LEAD}"), "POST")
    # Creating a report is a write, and is not covered.
    assert not declarations.allows(declared, "salesforce", "viewer", None, _m("POST", "analytics/reports"), "POST")


@pytest.mark.parametrize("slug", ["ada\n", "ada ", "\nada", "a/b", ""])
def test_an_agent_slug_must_be_exactly_a_slug(slug):
    with pytest.raises(ValueError, match="agent"):
        declarations.validate([{**DECL, "as": "shared", "agent": slug}])
    assert declarations.validate([{**DECL, "as": "shared", "agent": "ada"}])
