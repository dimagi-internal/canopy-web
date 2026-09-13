"""A page's actions, as MCP tools.

The point of going through MCP rather than a bespoke endpoint: a host declares
its actions once in JS, and any MCP-speaking agent gets them as ordinary tools
with schemas. These tests pin the translation, and the two places it could go
quietly wrong — a host shadowing a canopy tool, and a user with two tabs open.
"""

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions.models import Session
from apps.mcp import page_tools
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

DISMISS = {
    "name": "dismissInsights",
    "description": "Dismiss insights from the list the user is viewing",
    "inputSchema": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["ids"],
    },
}


def _user(name="jj"):
    user = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    ws, _ = Workspace.objects.get_or_create(
        slug="w1", defaults={"display_name": "W1", "created_by": user}
    )
    WorkspaceMembership.objects.get_or_create(
        user=user, workspace=ws, defaults={"role": WorkspaceMembership.OWNER}
    )
    return user, ws


def _session(user, ws, actions, title="chat"):
    return Session.objects.create(
        workspace=ws, created_by=user, title=title, page_actions_available=actions
    )


def test_a_declared_action_becomes_an_mcp_tool_with_its_schema():
    user, ws = _user()
    session = _session(user, ws, [DISMISS])

    specs = page_tools.page_tool_specs(user)
    assert [n for n, _s, _sp in specs] == ["page_dismissInsights"]

    tool = page_tools.to_mcp_tool(*specs[0])
    # The schema is the whole reason a declaration carries one: without it the
    # agent knows the tool exists but not that it takes `ids`.
    assert tool.inputSchema["required"] == ["ids"]
    assert "dismissInsights" in tool.name
    # The description says where it runs and how it fails, because "the tab is
    # closed" is the outcome an agent most needs to be able to act on.
    assert str(session.id) in tool.description
    assert "never queued" in tool.description


def test_tools_are_namespaced_so_a_page_cannot_shadow_a_canopy_tool():
    """A page declaring `clear_insights` must not intercept calls meant for
    canopy's real one."""
    user, ws = _user()
    _session(user, ws, [{"name": "clear_insights"}])
    assert [n for n, _s, _sp in page_tools.page_tool_specs(user)] == ["page_clear_insights"]


def test_a_session_with_no_declaration_contributes_nothing():
    """A session can be live with no page attached — the agent working alone."""
    user, ws = _user()
    _session(user, ws, [])
    assert page_tools.page_tool_specs(user) == []


def test_a_user_with_no_pages_gets_no_tools():
    user, _ws = _user()
    assert page_tools.page_tool_specs(user) == []


def test_two_tabs_declaring_the_same_action_expose_one_tool_newest_wins():
    """Two tools with one name is unusable. The newest attachment wins, and the
    chosen session is named in the description so a wrong guess is visible."""
    user, ws = _user()
    _session(user, ws, [DISMISS], title="older")
    newer = _session(user, ws, [DISMISS], title="newer")

    specs = page_tools.page_tool_specs(user)

    assert len(specs) == 1
    _name, session, _spec = specs[0]
    assert session.id == newer.id


def test_two_tabs_with_different_actions_expose_both():
    user, ws = _user()
    _session(user, ws, [DISMISS])
    _session(user, ws, [{"name": "recordCount"}])
    names = sorted(n for n, _s, _sp in page_tools.page_tool_specs(user))
    assert names == ["page_dismissInsights", "page_recordCount"]


def test_another_users_page_is_not_exposed():
    """The tool list is per caller — a page belongs to whoever has it open."""
    mine, ws = _user("jj")
    theirs, _ = _user("other")
    _session(theirs, ws, [DISMISS])
    assert page_tools.page_tool_specs(mine) == []


def test_a_declaration_with_no_name_is_ignored():
    user, ws = _user()
    _session(user, ws, [{"description": "nameless"}, DISMISS])
    assert [n for n, _s, _sp in page_tools.page_tool_specs(user)] == ["page_dismissInsights"]


def test_a_host_using_parameters_instead_of_inputschema_still_works():
    """`parameters` is OpenAI's function-calling spelling and `inputSchema` is
    MCP's. A host that writes either should not silently get a schemaless tool."""
    user, ws = _user()
    _session(user, ws, [{"name": "act", "parameters": {"type": "object", "required": ["x"]}}])
    tool = page_tools.to_mcp_tool(*page_tools.page_tool_specs(user)[0])
    assert tool.inputSchema["required"] == ["x"]


def test_a_schemaless_declaration_still_produces_a_valid_tool():
    """MCP requires an inputSchema. A host that omits one gets an empty object
    rather than a tool the client rejects outright."""
    user, ws = _user()
    _session(user, ws, [{"name": "ping"}])
    tool = page_tools.to_mcp_tool(*page_tools.page_tool_specs(user)[0])
    assert tool.inputSchema == {"type": "object", "properties": {}}
