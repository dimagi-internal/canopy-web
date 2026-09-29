"""Hierarchical workspaces: an org owner owns every division; nothing else flows down.

The narrowness is the point. The org workspace (`dimagi`) is self-join for the
whole email domain, so if a parent's EDITORS inherited into children, every
employee would see every division's agents — the isolation a division
workspace exists to provide would be gone.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from apps.workspaces import services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
OWNER, EDITOR, VIEWER = (
    WorkspaceMembership.OWNER, WorkspaceMembership.EDITOR, WorkspaceMembership.VIEWER,
)


def _user(email):
    return User.objects.create(username=email, email=email)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _json(c, method, url, data=None):
    return getattr(c, method)(url, data=json.dumps(data or {}), content_type="application/json")


@pytest.fixture
def tree():
    """dimagi (org) -> strategy (division) -> strat-team (team); plus an unrelated root."""
    org_owner = _user("ceo@dimagi.com")
    org_editor = _user("staff@dimagi.com")
    div_owner = _user("cos@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=org_owner)
    strategy = Workspace.objects.create(
        slug="strategy", display_name="Strategy", created_by=org_owner, parent=dimagi,
    )
    team = Workspace.objects.create(
        slug="strat-team", display_name="Team", created_by=org_owner, parent=strategy,
    )
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=org_owner)
    WorkspaceMembership.objects.create(workspace=dimagi, user=org_owner, role=OWNER)
    WorkspaceMembership.objects.create(workspace=dimagi, user=org_editor, role=EDITOR)
    WorkspaceMembership.objects.create(workspace=strategy, user=div_owner, role=OWNER)
    return {
        "org_owner": org_owner, "org_editor": org_editor, "div_owner": div_owner,
        "dimagi": dimagi, "strategy": strategy, "team": team, "other": other,
    }


def test_org_owner_owns_every_descendant(tree):
    u = tree["org_owner"]
    assert services.member_role(u, "strategy") == OWNER
    assert services.member_role(u, "strat-team") == OWNER
    assert services.is_member(u, "strat-team")
    assert services.user_workspace_slugs(u) == {"dimagi", "strategy", "strat-team"}


def test_parent_editor_inherits_nothing(tree):
    u = tree["org_editor"]
    assert services.member_role(u, "dimagi") == EDITOR
    assert services.member_role(u, "strategy") is None
    assert not services.is_member(u, "strat-team")
    assert services.user_workspace_slugs(u) == {"dimagi"}


def test_division_owner_does_not_see_up_or_sideways(tree):
    u = tree["div_owner"]
    assert services.member_role(u, "strat-team") == OWNER  # down: yes
    assert services.member_role(u, "dimagi") is None       # up: no
    assert services.member_role(u, "other") is None        # sideways: no


def test_inherited_owner_outranks_weaker_direct_row(tree):
    WorkspaceMembership.objects.create(workspace=tree["strategy"], user=tree["org_owner"], role=VIEWER)
    m = services.membership(tree["org_owner"], "strategy")
    assert m.role == OWNER and m.inherited is True


def test_direct_owner_row_is_returned_as_is(tree):
    m = services.membership(tree["div_owner"], "strategy")
    assert m.pk is not None and not getattr(m, "inherited", False)


def test_cycle_is_refused(tree):
    dimagi = tree["dimagi"]
    dimagi.parent = tree["team"]
    with pytest.raises(ValidationError):
        dimagi.save()
    s = tree["strategy"]
    s.parent_id = "strategy"
    with pytest.raises(ValidationError):
        s.save()


def test_list_includes_inherited_and_marks_it(tree):
    r = _client(tree["org_owner"]).get("/api/workspaces/")
    assert r.status_code == 200
    rows = {w["slug"]: w for w in r.json()}
    assert set(rows) == {"dimagi", "strategy", "strat-team"}
    assert rows["strategy"]["parent"] == "dimagi"
    assert rows["strategy"]["inherited"] is True and rows["strategy"]["role"] == OWNER
    assert rows["dimagi"]["inherited"] is False


def test_pinned_tenant_url_admits_inherited_owner_only(tree):
    assert _client(tree["org_owner"]).get("/api/w/strategy/agents/").status_code == 200
    assert _client(tree["org_editor"]).get("/api/w/strategy/agents/").status_code == 404


def test_create_child_requires_owning_parent(tree):
    r = _json(_client(tree["org_owner"]), "post", "/api/workspaces/",
              {"slug": "ops", "display_name": "Operations", "parent": "dimagi"})
    assert r.status_code == 201, r.content
    assert r.json()["parent"] == "dimagi"
    assert Workspace.objects.get(slug="ops").parent_id == "dimagi"
    # An editor of the parent may not nest under it.
    r = _json(_client(tree["org_editor"]), "post", "/api/workspaces/",
              {"slug": "sneaky", "display_name": "Sneaky", "parent": "dimagi"})
    assert r.status_code == 403
    assert not Workspace.objects.filter(slug="sneaky").exists()


def test_inherited_owner_can_administer_members(tree):
    c = _client(tree["org_owner"])
    r = _json(c, "post", "/api/workspaces/strategy/invites/", {"email": "new@dimagi.com", "role": "owner"})
    assert r.status_code == 201, r.content


def test_move_parent_requires_owner_of_both_ends(tree):
    # div_owner owns `strategy` but not `other`: cannot move it under `other`.
    r = _json(_client(tree["div_owner"]), "put", "/api/workspaces/strategy/parent", {"parent": "other"})
    assert r.status_code == 404  # not a member of `other` — no existence leak
    # org_owner: add as owner of `other`, then the move works, and a cycle is 422.
    WorkspaceMembership.objects.create(workspace=tree["other"], user=tree["org_owner"], role=OWNER)
    c = _client(tree["org_owner"])
    r = _json(c, "put", "/api/workspaces/other/parent", {"parent": "dimagi"})
    assert r.status_code == 200 and r.json()["parent"] == "dimagi"
    r = _json(c, "put", "/api/workspaces/dimagi/parent", {"parent": "strat-team"})
    assert r.status_code == 422


def test_delete_refuses_parent_with_children(tree):
    r = _client(tree["org_owner"]).delete("/api/workspaces/dimagi/")
    assert r.status_code == 409
    assert "strategy" in r.json()["detail"]


# --- The agent list, across the tree -----------------------------------------
#
# The tests above asserted that the tree grants the right ACCESS, and every one
# passed while the Agents page broke on labs. Two gaps: nothing asserted the
# ORDER of `GET /api/workspaces/` (the client's default workspace is its first
# entry), and nothing looked at which AGENTS a tenant URL returns — only its
# status code. The fixture also built the parent before its children, which is
# the one shape that hides the ordering bug: on labs the divisions were created
# AFTER the memberships that already existed.


def _agent(slug, ws, owner):
    from apps.agents.models import Agent

    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)


def _agent_slugs(c, url):
    r = c.get(url)
    assert r.status_code == 200, r.content
    return sorted(a["slug"] for a in r.json()["items"])


@pytest.fixture
def labs_shape():
    """What labs actually looked like: an owner of `dimagi` (org) and `connect`
    (the division with the agents), and then — created LATER — new empty
    divisions under `dimagi` that the owner holds only by inheritance."""
    owner = _user("jj@dimagi.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=owner, parent=dimagi,
    )
    WorkspaceMembership.objects.create(workspace=dimagi, user=owner, role=OWNER)
    WorkspaceMembership.objects.create(workspace=connect, user=owner, role=OWNER)
    strategy = Workspace.objects.create(
        slug="strategy", display_name="Strategy", created_by=owner, parent=dimagi,
    )
    ops = Workspace.objects.create(
        slug="operations", display_name="Operations", created_by=owner, parent=dimagi,
    )
    for slug in ("ace", "hal"):
        _agent(slug, connect, owner)
    _agent("eva", dimagi, owner)
    _agent("fizzy", strategy, owner)
    return {"owner": owner, "dimagi": dimagi, "connect": connect, "strategy": strategy, "ops": ops}


def test_default_workspace_is_a_direct_membership_not_a_newer_inherited_one(labs_shape):
    """The regression: `/` and `/agents` land on the FIRST workspace listed.
    Sorting direct and inherited rows together by `created_at` made that an
    empty division created minutes ago instead of `connect`."""
    rows = _client(labs_shape["owner"]).get("/api/workspaces/").json()
    assert [w["slug"] for w in rows] == ["connect", "dimagi", "operations", "strategy"]
    assert [w["inherited"] for w in rows] == [False, False, True, True]


def test_each_tenant_url_lists_exactly_its_own_agents(labs_shape):
    c = _client(labs_shape["owner"])
    assert _agent_slugs(c, "/api/w/connect/agents/") == ["ace", "hal"]
    assert _agent_slugs(c, "/api/w/dimagi/agents/") == ["eva"]  # a parent does not absorb its children
    assert _agent_slugs(c, "/api/w/strategy/agents/") == ["fizzy"]  # inherited ownership lists them
    assert _agent_slugs(c, "/api/w/operations/agents/") == []


def test_flat_agent_list_spans_owned_descendants(labs_shape):
    c = _client(labs_shape["owner"])
    assert _agent_slugs(c, "/api/agents/") == ["ace", "eva", "fizzy", "hal"]
    # …and every agent the list shows must also open, or the list is a lie.
    for slug in ("ace", "eva", "fizzy", "hal"):
        assert c.get(f"/api/agents/{slug}/").status_code == 200


def test_org_editor_sees_org_agents_but_no_division_agents(labs_shape):
    staff = _user("staff@dimagi.com")
    WorkspaceMembership.objects.create(workspace=labs_shape["dimagi"], user=staff, role=EDITOR)
    c = _client(staff)
    assert [w["slug"] for w in c.get("/api/workspaces/").json()] == ["dimagi"]
    assert _agent_slugs(c, "/api/agents/") == ["eva"]
    assert c.get("/api/w/connect/agents/").status_code == 404
    assert c.get("/api/agents/ace/").status_code == 404


def test_division_owner_sees_only_their_division(labs_shape):
    lead = _user("lead@dimagi.com")
    WorkspaceMembership.objects.create(workspace=labs_shape["strategy"], user=lead, role=OWNER)
    c = _client(lead)
    assert [w["slug"] for w in c.get("/api/workspaces/").json()] == ["strategy"]
    assert _agent_slugs(c, "/api/agents/") == ["fizzy"]
    assert c.get("/api/w/dimagi/agents/").status_code == 404
