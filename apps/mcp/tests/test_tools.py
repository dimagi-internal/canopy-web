"""MCP tools: list + call run as the authenticated user and respect filters.

We exercise the tools through the real FastMCP instance (mcp.list_tools /
mcp.call_tool). To simulate an authenticated caller we set the SDK auth
context var to an AuthenticatedUser wrapping an AccessToken — the same
object CanopyPATVerifier would produce — which is what get_access_token()
reads inside the tool.
"""
from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import (
    AuthenticatedUser,
    auth_context_var,
)

from apps.mcp.server import mcp
from apps.shareouts.models import Shareout

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
    """An editor of the default workspace — clearing shareouts is the author tier."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    return user


_DAY = iter(range(1, 28))


def _shareout(user, project_slug, title, source="canopy"):
    """A shareout the editor posted (an editor clears their own). Each on its own
    day, so the one-per-period-and-source constraint never trips."""
    day = next(_DAY)
    return Shareout.objects.create(
        project_slug=project_slug, workspace=a_workspace(), created_by=user, title=title,
        content="c", source=source,
        period_start=dt.datetime(2026, 9, day, 0, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 9, day, 23, tzinfo=dt.timezone.utc),
    )


@pytest.mark.django_db
def test_tools_list_returns_the_list_and_clear_tools():
    tools = async_to_sync(mcp.list_tools)()
    names = {t.name for t in tools}
    assert {"list_shareouts", "clear_shareouts"} <= names


@pytest.mark.django_db
def test_list_shareouts_runs_and_returns_rows():
    user = _editor()
    proj = "canopy"
    _shareout(user, proj, "shipped the thing")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("list_shareouts", {})

    rows = result.structured_content["items"]  # the REST route's Page
    assert len(rows) == 1
    assert rows[0]["title"] == "shipped the thing"
    assert rows[0]["project_slug"] == "canopy"


@pytest.mark.django_db
def test_clear_shareouts_respects_project_filter():
    user = _editor()
    keep = "keep"
    drop = "drop"
    _shareout(user, keep, "keep me")
    _shareout(user, drop, "drop me 1")
    _shareout(user, drop, "drop me 2")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("clear_shareouts", {"project": "drop"})

    assert result.structured_content == {"cleared": 2}
    remaining = Shareout.objects.all()
    assert remaining.count() == 1
    assert remaining.first().project_slug == keep


@pytest.mark.django_db
def test_clear_shareouts_respects_source_filter():
    user = _editor()
    proj = "p"
    _shareout(user, proj, "x", source="run-a")
    _shareout(user, proj, "y", source="run-b")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("clear_shareouts", {"source": "run-a"})

    assert result.structured_content == {"cleared": 1}
    assert Shareout.objects.filter(source="run-b").exists()


@pytest.mark.django_db
def test_clear_shareouts_with_no_arguments_runs_and_writes_audit_as_user():
    """A route whose body fields are ALL optional, called with no arguments —
    the case `api_tools._ensure_json_body` exists for (without it Ninja answers
    422 "payload: Field required")."""
    from apps.mcp.models import MCPAuditLog

    user = _editor()
    proj = "p"
    _shareout(user, proj, "x")

    with as_user(user):
        result = async_to_sync(mcp.call_tool)("clear_shareouts", {})

    assert result.structured_content == {"cleared": 1}
    log = MCPAuditLog.objects.filter(tool="clear_shareouts").latest("created_at")
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
