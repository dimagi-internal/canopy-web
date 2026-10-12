"""An unpinned LIST must not mix organisations (owner decision, 2026-10-11).

"Workspaces should need to be specified — workspace confusion shouldn't be
possible." A new root workspace (`marshellis`, a family) sits beside the
`dimagi` tree (dimagi → connect). A caller in both — ada@, Jonathan — used to
get the UNION of every membership from a flat `/api/…` list or an MCP tool
called without `workspace`, so family rows landed in Dimagi work.

The rule (`apps.workspaces.services.read_scope`):

* pinned (`/api/w/<ws>/…`, the MCP `workspace` arg) → that workspace and the
  caller's workspaces below it (2026-10-12; `tests/test_pinned_read_covers_subtree.py`);
* unpinned, every membership in ONE tree (dimagi + connect) → the union, as before;
* unpinned, memberships in more than one tree → 422 naming them.

By-id reads, `/api/me/`, the workspace switcher and runner paths keep the full
membership set: opening one object you belong to mixes nothing.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.mcp.server import mcp
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership as M

User = get_user_model()


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()


@pytest.fixture()
def world(db):
    """dimagi → connect (one organisation) and marshellis (another root)."""
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj,
                                       parent=dimagi)
    family = Workspace.objects.create(slug="marshellis", display_name="Marshellis", created_by=jj)
    ada = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    for ws in (dimagi, connect, family):
        wsvc.ensure_member(ws, ada, M.EDITOR)
    dimagi_only = User.objects.create_user("d", "d@dimagi.com", "pw")
    for ws in (dimagi, connect):
        wsvc.ensure_member(ws, dimagi_only, M.EDITOR)
    Agent.objects.create(slug="hal", name="Hal", workspace=dimagi)
    Agent.objects.create(slug="ace", name="Ace", workspace=connect)
    Agent.objects.create(slug="nanny", name="Nanny", workspace=family)
    return {"ada": ada, "dimagi_only": dimagi_only}


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _slugs(resp) -> set[str]:
    body = resp.json()
    return {a["slug"] for a in (body["items"] if isinstance(body, dict) else body)}


# -- the rule ------------------------------------------------------------------


def test_one_tree_still_unions_unpinned(world):
    resp = _client(world["dimagi_only"]).get("/api/agents/")
    assert resp.status_code == 200
    assert _slugs(resp) == {"hal", "ace"}


def test_two_trees_unpinned_list_is_refused_and_names_the_workspaces(world):
    resp = _client(world["ada"]).get("/api/agents/")
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("/workspace-required")
    for slug in ("dimagi", "connect", "marshellis"):
        assert slug in body["detail"]
    assert "/api/w/<workspace>/" in body["detail"]
    assert body["extras"]["trees"] == {"dimagi": ["connect", "dimagi"], "marshellis": ["marshellis"]}


def test_pinned_list_stays_in_its_tree(world):
    c = _client(world["ada"])
    assert _slugs(c.get("/api/w/dimagi/agents/")) == {"hal", "ace"}  # connect is below dimagi
    assert _slugs(c.get("/api/w/connect/agents/")) == {"ace"}
    assert _slugs(c.get("/api/w/marshellis/agents/")) == {"nanny"}


@pytest.mark.parametrize("path", [
    "/api/tasks/", "/api/projects/", "/api/harness/turns/", "/api/harness/turns/unclaimable",
    "/api/canopy-sessions/", "/api/timeline/", "/api/shareouts/", "/api/reviews/",
    "/api/threads/", "/api/huddles/", "/api/ddd/narratives/", "/api/events/",
])
def test_every_flat_list_takes_the_rule(world, path):
    assert _client(world["ada"]).get(path).status_code == 422, path
    assert _client(world["dimagi_only"]).get(path).status_code == 200, path


# -- what must NOT refuse --------------------------------------------------------


def test_by_id_reads_keep_every_membership(world):
    c = _client(world["ada"])
    assert c.get("/api/agents/nanny/").status_code == 200
    assert c.get("/api/agents/hal/").status_code == 200


def test_a_turn_list_narrowed_to_one_agent_needs_no_pin(world):
    """`?agent=` names one agent — one organisation — so a fleet survey asking
    per agent (Ada's `conduct`) is not refused."""
    from apps.harness.models import Turn

    Turn.objects.create(agent=Agent.objects.get(slug="nanny"), idempotency_key="n1", prompt="p")
    Turn.objects.create(agent=Agent.objects.get(slug="hal"), idempotency_key="h1", prompt="p")
    c = _client(world["ada"])
    rows = c.get("/api/harness/turns/?agent=nanny").json()
    assert [r["agent_slug"] for r in rows] == ["nanny"]
    assert c.get("/api/harness/turns/").status_code == 422


def test_me_and_the_workspace_switcher_are_unaffected(world):
    c = _client(world["ada"])
    assert c.get("/api/me/").status_code == 200
    rows = c.get("/api/workspaces/").json()
    assert {w["slug"]: w["root"] for w in rows} == {
        "dimagi": "dimagi", "connect": "dimagi", "marshellis": "marshellis"}


def test_tree_roots_and_read_scope(world):
    assert wsvc.tree_roots({"connect", "marshellis", "nope"}) == {
        "connect": "dimagi", "marshellis": "marshellis", "nope": "nope"}
    assert wsvc.read_scope(world["dimagi_only"]) == {"dimagi", "connect"}
    assert wsvc.read_scope(world["ada"], "marshellis") == {"marshellis"}
    with pytest.raises(wsvc.WorkspaceRequired):
        wsvc.read_scope(world["ada"])
    # A view spanning one org's workspaces (huddles, threads) stays in the pin's tree.
    assert wsvc.tree_scope(world["ada"], "connect") == {"dimagi", "connect"}
    assert wsvc.member_scope(world["ada"]) == {"dimagi", "connect", "marshellis"}


# -- MCP ---------------------------------------------------------------------------


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "auth_method": "pat"})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def test_mcp_service_scope_follows_the_rule(world):
    assert wsvc.workspace_slugs_for_user_id(world["dimagi_only"].pk) == {"dimagi", "connect"}
    assert wsvc.workspace_slugs_for_user_id(world["ada"].pk, "marshellis") == {"marshellis"}
    assert wsvc.workspace_slugs_for_user_id(world["dimagi_only"].pk, "marshellis") == set()
    with pytest.raises(wsvc.WorkspaceRequired):
        wsvc.workspace_slugs_for_user_id(world["ada"].pk)


def test_mcp_api_tool_without_workspace_is_refused_across_trees(world):
    with as_user(world["ada"]), pytest.raises(ToolError) as err:
        async_to_sync(mcp.call_tool)("list_agents", {})
    assert "marshellis" in str(err.value)
    with as_user(world["ada"]):
        res = async_to_sync(mcp.call_tool)("list_agents", {"workspace": "marshellis"}).structured_content
    rows = res.get("items") or res.get("result") or []
    assert {a["slug"] for a in rows} == {"nanny"}


def test_mcp_session_sweep_needs_a_workspace_across_trees(world):
    with as_user(world["ada"]), pytest.raises(ToolError) as err:
        async_to_sync(mcp.call_tool)("audit_session_noise", {})
    assert "workspace" in str(err.value)
    with as_user(world["ada"]):
        report = async_to_sync(mcp.call_tool)(
            "audit_session_noise", {"workspace": "marshellis"}).structured_content
    assert report["matched"] == 0
