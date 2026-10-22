"""/ide close review, through canopy's own MCP as a host-grant visitor.

Spec: docs/superpowers/specs/2026-10-16-ide-close-review-design.md. The same
in-process round trip `test_self_host.py` drives — the widget mints on /ide,
canopy grants itself `sessions:close`, and the calls go through the real DPoP
gate into canopy's own MCP as the visitor.
"""
from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from apps.canopy_sessions.models import Session
from apps.common.encryption import decrypt_secret
from apps.mcp.server import mcp
from apps.mcp.tools.close_review import VIEW_URI
from apps.mcp_apps_host import mcp_apps
from apps.tokens import host_gateway
from apps.tokens.models import HostGrant
from tests.fixtures.self_host import _ctx, _in_lifespan, _mint

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("self_host_base")]


@pytest.fixture()
def world(self_host_world):
    return self_host_world


@pytest.fixture()
def granted(world, gateway_to_self):  # noqa: F811 - fixtures by name
    inner, _app = gateway_to_self
    r = _mint(world["visitor"], "ide")
    assert r.status_code == 200 and r.json()["host_grant"] is True, r.content
    grant = HostGrant.objects.get(app=world["app"])
    assert grant.scope == "sessions:close"
    done = Session.objects.create(workspace=world["ws"], created_by=world["visitor"],
                                  title="Ship the thing")
    hidden = Session.objects.create(workspace=world["ws"], created_by=world["owner"],
                                    title="Someone else's")
    return {**world, "inner": inner, "ctx": _ctx(decrypt_secret(grant.access_token_enc)),
            "done": done, "hidden": hidden}


def _call(g, tool, args):
    return _in_lifespan(g["inner"], lambda: host_gateway.call_tool(g["ctx"], tool, args))


def _view_call(g, tool, args):
    """The View's path: listed as the viewer, the tool must be app-visible."""
    def check(t):
        assert t is not None and mcp_apps.app_visible(t.meta or {}), tool
    return _in_lifespan(g["inner"], lambda: host_gateway.view_call(g["ctx"], tool, args, check=check))


def test_the_model_sees_the_proposal_tool_and_never_the_close(granted):
    listed = _in_lifespan(granted["inner"], lambda: host_gateway.list_tools(granted["ctx"]))
    assert [t["name"] for t in listed] == ["propose_session_closes"]
    with pytest.raises(host_gateway.GatewayRefusal, match="only for the page's own view"):
        _call(granted, "close_proposed_sessions", {"session_ids": [str(granted["done"].pk)]})
    granted["done"].refresh_from_db()
    assert granted["done"].status != Session.ARCHIVED


def test_minting_on_ide_puts_the_view_in_the_site_index(granted):
    """An owner's turn calls on the agent's own login and never lists through the
    gateway, so the grant itself must make the View known."""
    app = granted["app"]
    app.refresh_from_db()
    assert mcp_apps.indexed_tool(app, "propose_session_closes")["resource_uri"] == VIEW_URI
    assert mcp_apps.indexed_tool(app, "close_proposed_sessions")["resource_uri"] == VIEW_URI


def test_a_proposal_comes_back_as_canopys_rows_read_as_the_visitor(granted):
    out = _call(granted, "propose_session_closes", {"proposals": [
        {"session_id": str(granted["done"].pk), "quote": "All done, safe to close.", "why": "It says so."},
        {"session_id": str(granted["hidden"].pk), "quote": "x", "why": "y"},
        {"session_id": "not-a-uuid"},
    ]})
    assert out["is_error"] is False, out
    body = out["structured"]
    assert body["proposed"] == 2
    [row] = body["rows"]
    assert row["id"] == str(granted["done"].pk) and row["title"] == "Ship the thing"
    assert row["quote"] == "All done, safe to close." and row["why"] == "It says so."
    assert row["blocked"] == "" and row["running"] is False
    assert "Someone else" not in str(body), "a session the visitor cannot read is never revealed"


def test_the_view_is_readable_with_the_grant_and_nothing_else_is(granted):
    contents = _in_lifespan(granted["inner"], lambda: host_gateway.view_read(granted["ctx"], VIEW_URI))
    assert any("close_proposed_sessions" in (c.get("text") or "") for c in contents)
    with pytest.raises(host_gateway.GatewayRefusal):
        _in_lifespan(granted["inner"], lambda: host_gateway.view_read(granted["ctx"], "ui://canopy/other"))


def test_close_closes_what_the_visitor_may_and_reports_the_rest(granted):
    done, hidden = granted["done"], granted["hidden"]
    out = _view_call(granted, "close_proposed_sessions",
                     {"session_ids": [str(done.pk), str(hidden.pk)]})
    assert not out.get("isError"), out
    results = {r["id"]: r["outcome"] for r in out["structuredContent"]["results"]}
    assert results == {str(done.pk): "closed", str(hidden.pk): "not_found"}
    done.refresh_from_db()
    hidden.refresh_from_db()
    assert done.status == Session.ARCHIVED
    assert hidden.status != Session.ARCHIVED


def test_close_refuses_every_credential_but_a_persons_grant(world):  # noqa: F811
    """An agent's own login lists every canopy tool, app-only or not; the close
    must still come only from the review's button."""
    session = Session.objects.create(workspace=world["ws"], created_by=world["visitor"], title="t")
    with pytest.raises(ToolError, match="only the close review"):
        _in_lifespan(mcp.http_app(path="/", transport="streamable-http", stateless_http=True),
                     lambda: mcp.call_tool("close_proposed_sessions", {"session_ids": [str(session.pk)]}))
    session.refresh_from_db()
    assert session.status != Session.ARCHIVED
