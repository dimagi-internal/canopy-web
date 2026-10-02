"""The MCP audit log has a reader: workspace admins read their workspace's tool
calls, and everyone reads their own. It had none — rows were written on every
MCP call and only aggregates ever left the table."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.mcp.audit import _write_audit_sync
from apps.mcp.models import MCPAuditLog
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def logs():
    ws, other = a_workspace("aud-ws"), a_workspace("aud-other")
    people = {
        "admin": a_member(ws, email="aud-admin@dimagi.com", role=M.ADMIN),
        "editor": a_member(ws, email="aud-editor@dimagi.com", role=M.EDITOR),
        "stranger": a_member(other, email="aud-stranger@dimagi.com", role=M.OWNER),
    }
    _write_audit_sync(user_id=people["editor"].pk, tool="list_agents", args_summary="GET",
                      ok=True, error="", workspace="aud-ws")
    _write_audit_sync(user_id=people["stranger"].pk, tool="delete_agent", args_summary="DELETE",
                      ok=False, error="403", workspace="aud-other")
    return people


def _tools(user, query=""):
    c = Client()
    c.force_login(user)
    return [r["tool"] for r in c.get(f"/api/events/mcp-calls{query}").json()["items"]]


def test_an_admin_reads_the_workspaces_calls_and_nothing_else(logs):
    assert _tools(logs["admin"]) == ["list_agents"]


def test_a_non_admin_reads_only_their_own(logs):
    assert _tools(logs["editor"]) == ["list_agents"]
    _write_audit_sync(user_id=logs["admin"].pk, tool="admins_own", args_summary="",
                      ok=True, error="", workspace="aud-ws")
    assert _tools(logs["editor"]) == ["list_agents"]


def test_failed_filter(logs):
    assert _tools(logs["stranger"], "?failed=true") == ["delete_agent"]


def test_a_flat_call_is_filed_under_the_callers_sole_workspace(logs):
    _write_audit_sync(user_id=logs["editor"].pk, tool="flat", args_summary="", ok=True,
                      error="", workspace=None)
    assert MCPAuditLog.objects.get(tool="flat").workspace_slug == "aud-ws"
