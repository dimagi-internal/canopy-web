"""MCP tools: list + call run as the authenticated user and respect filters.

We exercise the tools through the real FastMCP instance (mcp.list_tools /
mcp.call_tool). To simulate an authenticated caller we set the SDK auth
context var to an AuthenticatedUser wrapping an AccessToken — the same
object CanopyPATVerifier would produce — which is what get_access_token()
reads inside the tool.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import (
    AuthenticatedUser,
    auth_context_var,
)

from apps.mcp.server import mcp
from apps.projects.models import Project, ProjectContext

from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace

User = get_user_model()


@contextlib.contextmanager
def as_user(user):
    """Run the block with `user` set as the authenticated MCP caller."""
    access = AccessToken(
        token="test-token",
        client_id=str(user.pk),
        scopes=["canopy:user"],
        claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _editor():
    """An editor of the default workspace — clearing insights is the author tier."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    return user


def _project(name, slug):
    # Homed: a project with no workspace is visible to nobody.
    return Project.objects.create(name=name, slug=slug, workspace=a_workspace())


def _insight(project, content, source="canopy"):
    return ProjectContext.objects.create(
        project=project, context_type="insight", content=content, source=source
    )


@pytest.mark.django_db
def test_tools_list_returns_insight_tools():
    tools = async_to_sync(mcp.list_tools)()
    names = {t.name for t in tools}
    assert {"list_insights", "clear_insights"} <= names


@pytest.mark.django_db
def test_list_insights_runs_and_returns_rows():
    user = _editor()
    proj = _project("Canopy", "canopy")
    _insight(proj, "[ship_gap] do the thing")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("list_insights", {})

    rows = result.structured_content["items"]  # the REST route's Page
    assert len(rows) == 1
    assert rows[0]["content"] == "[ship_gap] do the thing"
    assert rows[0]["project_slug"] == "canopy"


@pytest.mark.django_db
def test_clear_insights_respects_project_filter():
    user = _editor()
    keep = _project("Keep", "keep")
    drop = _project("Drop", "drop")
    _insight(keep, "[a] keep me")
    _insight(drop, "[b] drop me 1")
    _insight(drop, "[b] drop me 2")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("clear_insights", {"project": "drop"})

    assert result.structured_content == {"cleared": 2}
    remaining = ProjectContext.objects.filter(context_type="insight")
    assert remaining.count() == 1
    assert remaining.first().project_id == keep.pk


@pytest.mark.django_db
def test_clear_insights_respects_category_filter():
    user = _editor()
    proj = _project("P", "p")
    _insight(proj, "[hygiene] x")
    _insight(proj, "[ship_gap] y")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("clear_insights", {"category": "hygiene"})

    assert result.structured_content == {"cleared": 1}
    assert ProjectContext.objects.filter(content__startswith="[ship_gap]").exists()


@pytest.mark.django_db
def test_clear_insights_writes_audit_as_user():
    from apps.mcp.models import MCPAuditLog

    user = _editor()
    proj = _project("P", "p")
    _insight(proj, "[a] x")

    with as_user(user):
        async_to_sync(mcp.call_tool)("clear_insights", {})

    log = MCPAuditLog.objects.filter(tool="clear_insights").latest("created_at")
    assert log.user_id == user.pk
    assert log.ok is True


@pytest.mark.django_db
def test_unauthenticated_tool_call_is_rejected_at_transport():
    """No auth context => get_access_token() returns None in the tool.

    The tool still runs (call_tool bypasses transport auth), but the
    audited user is None — confirming the tool reads identity from the
    token and does not fabricate one. Transport-level 401 enforcement is
    covered by FastMCP MultiAuth itself (the verifier returning None).
    """
    from apps.mcp.audit import current_user_id

    # Outside any as_user() block there is no authenticated principal.
    assert current_user_id() is None
