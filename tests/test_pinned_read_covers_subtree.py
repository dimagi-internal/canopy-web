"""A pinned READ covers the workspaces below the pin (owner decision, Jonathan, 2026-10-12).

"A pinned read of workspace W returns W plus every DESCENDANT of W that the
caller is a member of (direct or by inherited ownership). Separate roots never
join. WRITES pinned to W still land in exactly W. By-id unchanged."

Before this, `/api/w/dimagi/agents/` returned only dimagi's agents, so a fleet
survey pinned to its home (Ada's `list_agents(workspace="dimagi")`) missed every
agent living in `connect` (echo, hal, ace, jarvis) and clients fanned one read
out per workspace to put the organisation back together.
"""
from __future__ import annotations

import contextlib
import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent, AgentTask
from apps.mcp.server import mcp
from apps.shareouts.models import Shareout
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership as M

User = get_user_model()


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()


@pytest.fixture()
def world(db):
    """dimagi → connect (one organisation) and marshellis (another root, with a
    child of its own)."""
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj,
                                       parent=dimagi)
    family = Workspace.objects.create(slug="marshellis", display_name="Marshellis", created_by=jj)
    kids = Workspace.objects.create(slug="kids", display_name="Kids", created_by=jj, parent=family)

    both = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    for ws in (dimagi, connect, family, kids):
        wsvc.ensure_member(ws, both, M.EDITOR)
    dimagi_only = User.objects.create_user("d", "d@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, dimagi_only, M.EDITOR)
    org_owner = User.objects.create_user("o", "o@dimagi.com", "pw")
    wsvc.ensure_member(dimagi, org_owner, M.OWNER)  # owns connect by inheritance
    connect_only = User.objects.create_user("c", "c@dimagi.com", "pw")
    wsvc.ensure_member(connect, connect_only, M.EDITOR)

    hal = Agent.objects.create(slug="hal", name="Hal", workspace=dimagi)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=connect)
    Agent.objects.create(slug="nanny", name="Nanny", workspace=family)
    Agent.objects.create(slug="tutor", name="Tutor", workspace=kids)
    AgentTask.objects.create(agent=hal, ext_id="T1", title="dimagi task")
    AgentTask.objects.create(agent=echo, ext_id="T1", title="connect task")
    return {"both": both, "dimagi_only": dimagi_only, "org_owner": org_owner,
            "connect_only": connect_only}


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _agents(client, path) -> set[str]:
    resp = client.get(path)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return {a["slug"] for a in (body["items"] if isinstance(body, dict) else body)}


# -- reads ---------------------------------------------------------------------


def test_pinned_parent_returns_the_child_rows_for_a_member_of_both(world):
    c = _client(world["both"])
    assert _agents(c, "/api/w/dimagi/agents/") == {"hal", "echo"}
    tasks = c.get("/api/w/dimagi/tasks/").json()
    assert {(t["agent_slug"], t["workspace"]) for t in tasks} == {
        ("hal", "dimagi"), ("echo", "connect")}


def test_a_parent_member_not_in_the_child_gets_no_child_rows(world):
    c = _client(world["dimagi_only"])
    assert _agents(c, "/api/w/dimagi/agents/") == {"hal"}
    assert {t["agent_slug"] for t in c.get("/api/w/dimagi/tasks/").json()} == {"hal"}


def test_an_inherited_owner_of_the_child_gets_its_rows(world):
    assert _agents(_client(world["org_owner"]), "/api/w/dimagi/agents/") == {"hal", "echo"}


def test_a_pinned_child_never_reaches_up_to_its_parent(world):
    assert _agents(_client(world["both"]), "/api/w/connect/agents/") == {"echo"}
    assert _agents(_client(world["connect_only"]), "/api/w/connect/agents/") == {"echo"}


def test_a_pinned_read_never_crosses_roots(world):
    c = _client(world["both"])
    assert _agents(c, "/api/w/dimagi/agents/") == {"hal", "echo"}
    assert _agents(c, "/api/w/marshellis/agents/") == {"nanny", "tutor"}
    assert wsvc.read_scope(world["both"], "dimagi") == {"dimagi", "connect"}
    assert wsvc.read_scope(world["both"], "marshellis") == {"marshellis", "kids"}


def test_by_id_covers_what_the_pinned_list_shows(world):
    """2026-10-17: a by-slug gate spans what the pinned LIST spans, so a row the
    dimagi list shows (echo, in connect) opens under the dimagi pin too."""
    c = _client(world["both"])
    assert c.get("/api/w/connect/agents/echo/").status_code == 200
    assert c.get("/api/w/dimagi/agents/echo/").status_code == 200
    assert wsvc.member_scope(world["both"], "dimagi") == {"dimagi", "connect"}
    # An owner of the parent is in the child by inheritance, as for lists.
    assert _client(world["org_owner"]).get("/api/w/dimagi/agents/echo/").status_code == 200


def test_by_id_never_reaches_past_the_callers_memberships(world):
    # In dimagi only: connect's agent stays a 404 under the dimagi pin.
    assert _client(world["dimagi_only"]).get("/api/w/dimagi/agents/echo/").status_code == 404
    assert wsvc.member_scope(world["dimagi_only"], "dimagi") == {"dimagi"}
    # A pinned child never reaches up to its parent.
    assert _client(world["both"]).get("/api/w/connect/agents/hal/").status_code == 404
    assert wsvc.member_scope(world["both"], "connect") == {"connect"}
    # Never across roots.
    assert _client(world["both"]).get("/api/w/dimagi/agents/nanny/").status_code == 404


# -- writes --------------------------------------------------------------------


def _shareout(ws, user, title):
    return Shareout.objects.create(
        workspace_id=ws, created_by=user, title=title, content="c", source="canopy:shareout",
        period_start="2026-10-01T00:00:00Z", period_end="2026-10-02T00:00:00Z")


def test_a_bulk_write_pinned_to_the_parent_stays_exactly_there(world):
    """"Clear every shareout" pinned to dimagi must not wipe connect's, though the
    pinned READ of the same feed shows both."""
    user = world["both"]
    _shareout("dimagi", user, "d")
    _shareout("connect", user, "c")
    c = _client(user)
    feed = c.get("/api/w/dimagi/shareouts/").json()
    assert {s["title"] for s in feed["items"]} == {"d", "c"}
    res = c.post("/api/w/dimagi/shareouts/clear/", data="{}", content_type="application/json")
    assert res.json() == {"cleared": 1}
    assert list(Shareout.objects.values_list("title", flat=True)) == ["c"]


def test_the_write_scope_is_exactly_the_pin(world):
    assert wsvc.write_scope(world["both"], "dimagi") == {"dimagi"}
    assert wsvc.workspace_slugs_for_user_id(world["both"].pk, "dimagi", write=True) == {"dimagi"}
    assert wsvc.workspace_slugs_for_user_id(world["both"].pk, "dimagi") == {"dimagi", "connect"}
    assert wsvc.workspace_slugs_for_user_id(world["dimagi_only"].pk, "connect") == set()


def test_a_create_pinned_to_the_parent_lands_in_exactly_it(world):
    body = {"shareouts": [{"period_start": "2026-10-01T00:00:00Z",
                           "period_end": "2026-10-02T00:00:00Z",
                           "title": "t", "content": "c", "source": "canopy:shareout"}]}
    res = _client(world["both"]).post("/api/w/dimagi/shareouts/", data=json.dumps(body),
                                      content_type="application/json")
    assert res.status_code == 201
    assert list(Shareout.objects.values_list("workspace_id", flat=True)) == ["dimagi"]


# -- what clients read ---------------------------------------------------------


def test_the_refusal_names_one_pin_per_organisation(world):
    body = _client(world["both"]).get("/api/agents/").json()
    assert body["extras"]["pins"] == ["dimagi", "marshellis"]
    assert wsvc.covering_pins({"connect", "kids"}) == ["connect", "kids"]


def test_workspaces_advertise_subtree_reads(world):
    rows = _client(world["both"]).get("/api/workspaces/").json()
    assert rows and all(r["pinned_read_covers_descendants"] is True for r in rows)


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "auth_method": "pat"})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def test_mcp_list_named_by_the_parent_covers_the_child(world):
    with as_user(world["both"]):
        res = async_to_sync(mcp.call_tool)("list_agents", {"workspace": "dimagi"}).structured_content
    rows = res.get("items") or res.get("result") or []
    assert {a["slug"] for a in rows} == {"hal", "echo"}
