"""Read mode and host sites over HTTP: the plain settings over `Agent.interface`.

 Driven through
the routes, as the form, the CLI and an MCP tool reach them.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import re

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.agents import profile_settings, published_profiles, roles, sensitive_data, turn_context
from apps.agents.answering_snapshot import probe_turn
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.api.api import api
from apps.events.models import SettingChange
from apps.mcp import api_tools
from apps.tokens.models import AppCredential, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "answering_interfaces.json"
PUBLISHED = dt.datetime(2026, 10, 23, 3, 10, 37, tzinfo=dt.UTC)


def _fixture():
    data = json.loads(FIXTURE.read_text())
    return {row["slug"]: row for row in [*data["agents"], *data["corner_cases"]]}


@pytest.fixture()
def w():
    user_model = get_user_model()
    owner = user_model.objects.create_user("own", "own@dimagi.com", "pw")
    admin = user_model.objects.create_user("adm", "adm@dimagi.com", "pw")
    member = user_model.objects.create_user("mem", "mem@dimagi.com", "pw")
    outsider = user_model.objects.create_user("out", "out@elsewhere.org", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.ADMIN)
    for person in (admin, member):
        WorkspaceMembership.objects.create(user=person, workspace=ws, role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner, email="hal@dimagi-ai.com")
    AgentAdmin.objects.create(agent=agent, user=admin)
    return {"ws": ws, "owner": owner, "admin": admin, "member": member, "outsider": outsider, "agent": agent}


def _as(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _put(client, path, body):
    return client.put(path, data=json.dumps(body), content_type="application/json")


def _shaped(w, slug="ace"):
    agent = w["agent"]
    agent.interface = copy.deepcopy(_fixture()[slug]["interface"])
    agent.interface_published_at = PUBLISHED
    agent.interface_published_by = w["owner"]
    agent.save()
    return agent


READ = "/api/agents/hal/read-mode"
SITES = "/api/agents/hal/host-sites"


# --- read mode --------------------------------------------------------------------------------

def test_read_mode_starts_on_the_default_and_customising_starts_from_it(w):
    client = _as(w["admin"])
    got = client.get(READ).json()
    assert got["source"] == "default"
    assert got["profile"]["tools"] == published_profiles.default_read_profile(w["agent"])["tools"]

    saved = _put(client, READ, {"description": "Ask Hal."}).json()
    assert saved["source"] == "custom" and saved["profile"]["description"] == "Ask Hal."
    assert saved["profile"]["tools"] == got["profile"]["tools"]    # only what was sent changed

    w["agent"].refresh_from_db()
    assert list(w["agent"].interface["capabilities"]) == ["ask"]
    # Read mode now stamps the override (`READ_MODE_STAMP`), as an owner's own read mode always has.
    reader = AgentGrant.objects.create(agent=w["agent"], kind=AgentGrant.PERSON, user=w["member"], role=roles.READ_ONLY)
    assert turn_context.for_asker(w["agent"], reader.user, verified=True).stamped == "ask"


def test_going_back_to_the_default_takes_both_names(w):
    agent = _shaped(w, "corner_both_names")
    assert _as(w["admin"]).delete(READ).json()["source"] == "default"
    agent.refresh_from_db()
    assert agent.interface["capabilities"] == {}


def test_an_entry_can_be_cleared_and_a_read_named_override_keeps_its_name(w):
    agent = _shaped(w, "corner_read_named")
    client = _as(w["admin"])
    assert _put(client, READ, {"entry": "/hal:ask"}).json()["profile"]["entry"] == "/hal:ask"
    assert _put(client, READ, {"entry": None}).json()["profile"]["entry"] is None
    agent.refresh_from_db()
    assert list(agent.interface["capabilities"]) == ["read"]


@pytest.mark.parametrize("field", ["sites", "ceiling", "own_login", "data", "pages", "key", "name"])
def test_read_mode_has_no_way_to_name_anything_else(w, field):
    assert _put(_as(w["admin"]), READ, {field: ["x"]}).status_code == 422
    w["agent"].refresh_from_db()
    assert not w["agent"].interface.get("capabilities")


# --- host sites --------------------------------------------------------------------------

def test_a_new_site_starts_with_the_saved_defaults_and_can_go_sensitive_with_nothing_else(w):
    AppCredential.create_credential(name="connect-labs", created_by=w["owner"], workspace=w["ws"])
    client = _as(w["admin"])
    listing = client.get(SITES).json()
    assert listing["sites"] == [] and listing["available"] == ["connect-labs"]

    row = _put(client, f"{SITES}/connect-labs", {}).json()["sites"][0]
    assert row["key"] == "connect_labs" and row["tools"] == listing["new_site_defaults"]["tools"]
    assert sensitive_data.structural_refusals("x", {"sites": ["s"], **listing["new_site_defaults"]}) == []

    row = _put(client, f"{SITES}/connect-labs", {"sensitive": True}).json()["sites"][0]
    assert row["sensitive"] is True
    w["agent"].refresh_from_db()
    # The defaults are stored, so a later change to them moves no agent.
    assert w["agent"].interface["capabilities"]["connect_labs"]["tools"] == row["tools"]
    assert sensitive_data.handles_sensitive(w["agent"], "connect_labs")


def test_a_row_says_what_the_document_says_for_ace(w):
    _shaped(w)
    rows = {r["site"]: r for r in _as(w["member"]).get(SITES).json()["sites"]}
    assert set(rows) == {"canopy-web", "connect-labs"}
    assert rows["connect-labs"]["key"] == "connect"
    assert rows["connect-labs"]["own_login"] == ["LABS_MCP_TOKEN"]
    assert rows["connect-labs"]["ceiling"] == ["mcp__*connect_labs__*"]
    assert rows["canopy-web"]["sensitive"] is False


@pytest.mark.parametrize("glob", ["*", "mcp__*connect_labs__*", "connect_labs__*", "mcp__*__*"])
def test_a_site_tool_limit_that_matches_every_tool_is_refused(w, glob):
    """`mcp__*connect_labs__*` read like a restriction and restricted nothing (ACE's labs row, until 2026-11-06)."""
    agent = _shaped(w)
    admin = _as(w["admin"])
    # The fixture stores a catch-all already; clear it so the one sent below is a change.
    assert _put(admin, f"{SITES}/connect-labs", {"ceiling": []}).status_code == 200
    agent.refresh_from_db()
    before = copy.deepcopy(agent.interface)
    res = _put(admin, f"{SITES}/connect-labs", {"ceiling": [glob]})
    assert res.status_code == 422 and "limits nothing" in res.content.decode()
    agent.refresh_from_db()
    assert agent.interface == before


def test_clearing_a_site_tool_limit_and_naming_tools_both_save(w):
    agent = _shaped(w)
    admin = _as(w["admin"])
    assert _put(admin, f"{SITES}/connect-labs", {"ceiling": []}).status_code == 200
    agent.refresh_from_db()
    assert agent.interface["capabilities"]["connect"]["ceiling"] == []
    assert _put(admin, f"{SITES}/connect-labs", {"ceiling": ["mcp__*connect_labs__marketplace_*"]}).status_code == 200


def test_saving_another_field_does_not_trip_over_a_limit_already_stored(w):
    """The guard is on a limit a save changes, so a row stored before it can still be edited."""
    agent = _shaped(w)
    assert agent.interface["capabilities"]["connect"]["ceiling"] == ["mcp__*connect_labs__*"]
    admin = _as(w["admin"])
    assert _put(admin, f"{SITES}/connect-labs", {"sensitive": False}).status_code == 200
    # The form sends the stored limit back unchanged; that is not a new limit.
    assert _put(admin, f"{SITES}/connect-labs", {"ceiling": ["mcp__*connect_labs__*"]}).status_code == 200


def test_editing_a_row_keeps_its_key_and_leaves_every_other_entry_as_saved(w):
    agent = _shaped(w)
    before = copy.deepcopy(agent.interface)
    row = _put(_as(w["admin"]), f"{SITES}/connect-labs", {"ceiling": ["mcp__*connect_labs__x"]}).json()["sites"]
    assert {r["key"] for r in row} == {"canopy", "connect"}
    agent.refresh_from_db()
    assert agent.interface["capabilities"]["connect"]["ceiling"] == ["mcp__*connect_labs__x"]
    assert agent.interface["capabilities"]["connect"]["own_login"] == {"connect-labs": ["LABS_MCP_TOKEN"]}
    for name in ("ask", "canopy"):
        assert agent.interface["capabilities"][name] == before["capabilities"][name]
    assert agent.interface["version"] == before["version"]


def test_a_login_on_a_row_is_stored_for_its_site_and_withheld_there(w):
    agent = w["agent"]
    _put(_as(w["admin"]), f"{SITES}/site-a", {"own_login": ["A_TOKEN"]})
    agent.refresh_from_db()
    turn = probe_turn(user=w["owner"], site="site-a")
    assert published_profiles.page_turn(turn, agent) == {"site": "site-a", "withheld": ["A_TOKEN"]}


@pytest.mark.parametrize("site,key", [
    ("connect-labs", "connect_labs"), ("Connect_Labs", "connect_labs"), ("9lives", "site_9lives"),
    ("read", "read_2"), ("ask", "ask_2"), ("session_review", "session_review_2"),
    ("Session-Review", "session_review_2"),
    ("x" * 100, "x" * 37),
])
def test_a_new_rows_key_is_one_the_parser_accepts_and_canopy_has_not_reserved(w, site, key):
    response = _put(_as(w["admin"]), f"{SITES}/{site}", {})
    assert response.status_code == 200, response.content
    assert response.json()["sites"][0]["key"] == key
    w["agent"].refresh_from_db()
    assert published_profiles.parse(w["agent"].interface)


def test_two_sites_that_clean_up_to_one_name_get_two_keys(w):
    client = _as(w["admin"])
    _put(client, f"{SITES}/connect-labs", {})
    rows = _put(client, f"{SITES}/connect_labs", {}).json()["sites"]
    assert sorted(r["key"] for r in rows) == ["connect_labs", "connect_labs_2"]


def test_session_review_stays_canopys_through_every_new_route(w):
    """No route publishes it: not by naming a site after it, nor by a field. A row that cleans up to its
    name gets another key, so canopy's profile stays canopy's and a person cannot open it with a line."""
    client = _as(w["admin"])
    canopy_review = published_profiles.profile(w["agent"], published_profiles.SESSION_REVIEW)
    for site in ("session_review", "Session-Review", "session-review"):
        assert _put(client, f"{SITES}/{site}", {"tools": ["Bash"]}).status_code == 200
    assert _put(client, f"{SITES}/x", {"key": "session_review"}).status_code == 422
    assert _put(client, f"{SITES}/x", {"name": "session_review"}).status_code == 422
    assert _put(client, READ, {"name": "session_review"}).status_code == 422

    w["agent"].refresh_from_db()
    assert published_profiles.SESSION_REVIEW not in w["agent"].interface["capabilities"]
    assert published_profiles.profile(w["agent"], published_profiles.SESSION_REVIEW) == canopy_review
    # A key like it is an ordinary host site: what it opens is that row, never the review.
    assert "Bash" not in canopy_review["tools"]
    # The parser refuses the name too, whatever asks it.
    with pytest.raises(published_profiles.InterfaceError):
        published_profiles.parse({"capabilities": {"session_review": {}}})


def test_a_sensitive_row_must_be_able_to_hold_it(w):
    response = _put(_as(w["admin"]), f"{SITES}/site-a", {"sensitive": True, "bash": ["ls"], "tools": ["Write"]})
    assert response.status_code == 422
    assert "may run no shell" in response.json()["detail"]
    assert "a site that handles sensitive data" in response.json()["detail"]
    assert "bash:" not in response.json()["detail"] and "capability" not in response.json()["detail"]
    w["agent"].refresh_from_db()
    assert not w["agent"].interface.get("capabilities")


@pytest.mark.parametrize("site", ["not a site!", "a.b"])
def test_a_site_name_is_the_parsers_call(w, site):
    assert _put(_as(w["admin"]), f"{SITES}/{site}", {}).status_code == 422


def test_removing_a_row_closes_it_for_turns_already_stamped_with_it(w):
    agent = _shaped(w)
    client = _as(w["admin"])
    rows = client.delete(f"{SITES}/connect-labs").json()["sites"]
    assert [r["key"] for r in rows] == ["canopy"]
    agent.refresh_from_db()
    assert published_profiles.profile(agent, "connect")["tools"] == []
    assert client.delete(f"{SITES}/connect-labs").status_code == 404


# --- the rules --------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["corner_two_sites", "corner_sensitive_ask"])
def test_a_document_these_settings_cannot_hold_is_refused_and_left_as_it_is(w, slug):
    agent = _shaped(w, slug)
    before = copy.deepcopy(agent.interface)
    client = _as(w["admin"])
    for response in (_put(client, READ, {"description": "x"}), client.delete(READ),
                     _put(client, f"{SITES}/site-a", {"description": "x"}), client.delete(f"{SITES}/site-a"),
                     _put(client, f"{SITES}/site-new", {"description": "x"})):
        assert response.status_code == 422, response.content
        assert "cannot hold" in response.json()["detail"]
    agent.refresh_from_db()
    assert agent.interface == before


def test_a_mixed_agent_fits_because_sensitive_is_per_site(w):
    agent = _shaped(w, "corner_mixed")
    response = _put(_as(w["admin"]), f"{SITES}/site-a", {"description": "plain"})
    assert response.status_code == 200
    agent.refresh_from_db()
    assert sensitive_data.handles_sensitive(agent, "secret") and not sensitive_data.handles_sensitive(agent, "plain")


def test_a_document_these_settings_cannot_hold_says_what_a_person_can_do(w):
    agent = _shaped(w, "corner_two_sites")
    stored = copy.deepcopy(agent.interface)
    response = _put(_as(w["admin"]), READ, {"description": "changed"})
    assert response.status_code == 422
    assert "Nothing was changed" in response.json()["detail"] and "maintainers" in response.json()["detail"]
    assert "interface" not in response.json()["detail"]
    agent.refresh_from_db()
    assert agent.interface == stored


# --- every fixture agent round-trips ----------------------------------------------------------

@pytest.mark.parametrize("slug", [s for s, row in _fixture().items()
                                  if not profile_settings.document_problems(
                                      row["interface"].get("capabilities") or {})])
def test_saving_each_setting_back_changes_no_answer(w, slug):
    agent = _shaped(w, slug)
    stored = copy.deepcopy(agent.interface)
    stamps = ["", "read", "ask", published_profiles.SESSION_REVIEW, *stored["capabilities"], "retired_probe"]
    profiles = {s: published_profiles.profile(agent, s) for s in stamps}
    client = _as(w["admin"])

    read_mode = client.get(READ).json()
    if read_mode["source"] == "custom":
        assert _put(client, READ, read_mode["profile"]).status_code == 200
    for row in client.get(SITES).json()["sites"]:
        body = {k: row[k] for k in row if k not in ("site", "key")}
        assert _put(client, f"{SITES}/{row['site']}", body).status_code == 200

    agent.refresh_from_db()
    assert published_profiles.parse(agent.interface) == published_profiles.parse(stored)
    assert {s: published_profiles.profile(agent, s) for s in stamps} == profiles


# --- what a save does around the document -----------------------------------------------------

def test_dry_run_checks_and_changes_nothing(w):
    agent = _shaped(w)
    before = copy.deepcopy(agent.interface)
    changes = SettingChange.objects.count()
    client = _as(w["admin"])

    draft = _put(client, f"{SITES}/new-site", {"dry_run": True}).json()
    assert "new-site" in {r["site"] for r in draft["sites"]}
    draft = _put(client, READ, {"description": "draft", "dry_run": True}).json()
    assert draft["profile"]["description"] == "draft"
    assert _put(client, f"{SITES}/connect-labs", {"sensitive": True, "dry_run": True}).status_code == 422

    agent.refresh_from_db()
    assert agent.interface == before and agent.interface_published_at == PUBLISHED
    assert SettingChange.objects.count() == changes


def test_each_write_is_a_setting_change_with_its_before_and_after(w):
    client = _as(w["admin"])
    _put(client, READ, {"description": "Ask Hal."})
    _put(client, f"{SITES}/site-a", {})
    client.delete(f"{SITES}/site-a")
    client.delete(READ)
    changes = [row.changes[0] for row in SettingChange.objects.order_by("id")]
    assert [c["scope"] for c in changes] == [
        "agent:hal/read-mode", "agent:hal/site/site-a", "agent:hal/site/site-a", "agent:hal/read-mode"]
    assert "ask" in changes[0]["after"] and "ask" not in changes[0]["before"]


def test_a_save_says_who_saved_it_as_every_save_has(w):
    agent = _shaped(w, "labsdata")
    _put(_as(w["admin"]), f"{SITES}/connect-labs", {"description": "changed"})
    agent.refresh_from_db()
    claim = sensitive_data.as_claim(agent, "data")
    assert claim["declared_by"] == w["admin"].email and claim["data"] == "sensitive"
    assert agent.interface_published_at > PUBLISHED


def test_two_edits_from_stale_copies_both_survive(w):
    stale_one = Agent.objects.get(pk=w["agent"].pk)
    stale_two = Agent.objects.get(pk=w["agent"].pk)
    profile_settings.set_site(stale_one, "site-a", {}, by=w["admin"])
    profile_settings.set_site(stale_two, "site-b", {}, by=w["admin"])
    w["agent"].refresh_from_db()
    assert {"site_a", "site_b"} <= set(w["agent"].interface["capabilities"])


def test_the_writer_holds_the_rows_lock(w):
    with CaptureQueriesContext(connection) as ctx:
        profile_settings.set_site(w["agent"], "site-a", {}, by=w["admin"])
    if connection.vendor == "postgresql":
        assert any("FOR UPDATE" in q["sql"] for q in ctx.captured_queries)
    w["agent"].refresh_from_db()
    assert "site_a" in w["agent"].interface["capabilities"]


# --- who, and where it shows ------------------------------------------------------------------

def test_any_member_reads_and_only_an_admin_writes(w):
    member, outsider, owner = _as(w["member"]), _as(w["outsider"]), _as(w["owner"])
    assert member.get(READ).status_code == 200 and member.get(SITES).status_code == 200
    assert _put(member, READ, {"description": "no"}).status_code == 403
    assert _put(member, f"{SITES}/x", {}).status_code == 403
    assert member.delete(READ).status_code == 403
    assert member.delete(f"{SITES}/x").status_code == 403
    assert outsider.get(READ).status_code == 404
    assert _put(owner, READ, {"description": "yes"}).status_code == 200


def test_a_token_can_change_it_too(w):
    raw, _ = PersonalToken.create_for_user(user=w["admin"], label="t")
    response = Client().put(f"{SITES}/site-a", data="{}", content_type="application/json",
                            HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 200


def test_the_agents_own_page_carries_both(w):
    _shaped(w)
    body = _as(w["member"]).get("/api/agents/hal/").json()
    assert body["read_mode"]["source"] == "custom"
    assert {r["key"] for r in body["host_sites"]} == {"canopy", "connect"}


RETIRED_OPERATIONS = ("get_interface", "publish_interface", "validate_interface", "reset_interface",
                      "set_capability_sensitive")
RETIRED_ROUTES = [("get", "interface"), ("put", "interface"), ("delete", "interface"),
                  ("post", "interface/validate"), ("put", "interface/capabilities/x/sensitive")]
#: What each old route used to take: had one survived, this would reset or rewrite the agent.
OLD_BODY = {"interface": {"capabilities": {}}, "source": "capabilities: {}\n", "handles_sensitive": True}


def _old_route_callers(w):
    raw, _ = PersonalToken.create_for_user(user=w["admin"], label="cli")
    browser = Client(enforce_csrf_checks=True)   # a stale browser tab holds no token for this
    browser.force_login(w["admin"])
    return {"owner": _as(w["owner"]), "admin": _as(w["admin"]), "member": _as(w["member"]),
            "admin's token": Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), "browser": browser}


@pytest.mark.parametrize("method, tail", RETIRED_ROUTES)
@pytest.mark.parametrize("prefix", ["/api/agents/hal", "/api/w/connect/agents/hal"])
def test_each_old_route_answers_410_and_saves_nothing(w, method, tail, prefix):
    agent = _shaped(w, "labsdata")
    stored, published_at = copy.deepcopy(agent.interface), agent.interface_published_at
    for who, client in _old_route_callers(w).items():
        kwargs = {} if method == "get" else {"data": json.dumps(OLD_BODY), "content_type": "application/json"}
        response = getattr(client, method)(f"{prefix}/{tail}", **kwargs)
        assert response.status_code == 410, (who, response.content)
        assert response["Content-Type"] == "application/problem+json"
        body = response.json()
        assert body["read_mode"] == "/api/agents/hal/read-mode"
        assert body["host_sites"] == "/api/agents/hal/host-sites"
        for word in ("read-mode", "host-sites", "dry_run", "sensitive"):
            assert word in body["detail"], word
        # The plugin's credential_guard ends a turn on words like these. A 410 is not a login failing.
        assert not re.search(r"credential|authenticat|unauthori|api.?key|access token|401", response.content.decode(), re.I)
    agent.refresh_from_db()
    assert agent.interface == stored and agent.interface_published_at == published_at
    assert not SettingChange.objects.exists()


def test_the_old_routes_are_in_neither_openapi_nor_the_tools_and_the_new_ones_are_published():
    schema = api.get_openapi_schema()
    assert not [path for path in schema["paths"] if "/interface" in path]
    operations = {op["operationId"]: op for methods in schema["paths"].values() for op in methods.values()}
    tools = {op["operationId"] for methods in api_tools.tool_spec(api_tools.api_schema())["paths"].values()
             for op in methods.values()}
    for name in RETIRED_OPERATIONS:
        assert not [found for found in (*operations, *tools) if found.endswith(name)], name
    for tail in ("get_read_mode", "put_read_mode", "reset_read_mode", "list_agent_host_sites", "put_agent_host_site",
                 "delete_agent_host_site"):
        assert any(name.endswith(tail) and not op.get("deprecated") for name, op in operations.items()), tail
