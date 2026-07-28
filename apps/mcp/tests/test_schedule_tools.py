"""Schedule MCP tools — run as the authenticated user, audit, rate-limit."""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Turn
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(
        token="t", client_id=str(user.pk), scopes=["canopy:user"],
        claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email},
    )
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


@pytest.fixture()
def member():
    u = User.objects.create_user(username="jj", email="jj@dimagi.com")
    w = Workspace.objects.create(slug="dimagi", display_name="D", created_by=u, auto_join_domains=[])
    wsvc.ensure_member(w, u, WorkspaceMembership.OWNER)
    Agent.objects.create(slug="eva", name="Eva", workspace=w)
    return u


def _call(name, args):
    return async_to_sync(mcp.call_tool)(name, args)


def test_tools_are_registered():
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"list_schedules", "create_schedule", "update_schedule",
            "delete_schedule", "run_schedule_now", "preview_cron"} <= names


def test_create_then_list(member):
    with as_user(member):
        _call("create_schedule", {
            "agent_slug": "eva", "name": "Goal review", "prompt": "/eva:goal-review",
            "cron": "0 9 1 * *", "timezone": "America/New_York",
        })
        result = _call("list_schedules", {"agent_slug": "eva"})
    rows = result.structured_content["result"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Goal review"


def test_create_audits_success(member):
    with as_user(member):
        _call("create_schedule", {
            "agent_slug": "eva", "name": "R", "prompt": "p", "cron": "0 9 * * 5",
        })
    row = MCPAuditLog.objects.filter(tool="create_schedule").latest("id")
    assert row.ok is True


def test_run_now_audit_carries_schedule_name(member):
    with as_user(member):
        _call("create_schedule", {"agent_slug": "eva", "name": "Weekly", "prompt": "p", "cron": "0 9 * * 5"})
        sid = AgentSchedule.objects.get().id
        _call("run_schedule_now", {"agent_slug": "eva", "schedule_id": sid})
    row = MCPAuditLog.objects.filter(tool="run_schedule_now").latest("id")
    assert "Weekly" in row.args_summary
    # Run-now is scheduler work fired off-cycle, not a separate source.
    assert Turn.objects.filter(origin=Turn.ORIGIN_CANOPY_SCHEDULER).count() == 1


def test_non_member_gets_error_not_leak(member):
    outsider = User.objects.create_user(username="m", email="m@evil.com")
    with as_user(outsider):
        # ScheduleNotFound(agent_slug) surfaces through FastMCP as a ToolError
        # whose message carries the agent slug the service raised it with.
        with pytest.raises(ToolError, match="eva"):
            _call("list_schedules", {"agent_slug": "eva"})


def test_delete_supersedes_then_removes(member):
    with as_user(member):
        _call("create_schedule", {"agent_slug": "eva", "name": "D", "prompt": "p", "cron": "0 9 * * 5"})
        sid = AgentSchedule.objects.get().id
        _call("delete_schedule", {"agent_slug": "eva", "schedule_id": sid})
    assert not AgentSchedule.objects.filter(pk=sid).exists()


@override_settings(MCP_WRITE_LIMIT=0, MCP_WRITE_WINDOW_SECONDS=60)
def test_rate_limited_write_is_audited(member):
    # MCP_WRITE_LIMIT=0 forces check_write_limit to reject the very first
    # write. run_schedule_now is the highest-stakes write tool (it spawns a
    # real agent turn / burns tokens), so a rate-limited call here is the
    # abuse signature (an AI looping on it) we most need a trail for.
    cache.clear()
    with as_user(member):
        with pytest.raises(ToolError, match="rate limit"):
            _call("run_schedule_now", {"agent_slug": "eva", "schedule_id": 1})
    row = MCPAuditLog.objects.filter(tool="run_schedule_now").latest("id")
    assert row.ok is False
    assert "rate limit" in row.error.lower()
