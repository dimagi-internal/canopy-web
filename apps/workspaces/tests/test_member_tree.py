"""The member tree: everyone across a workspace and the ones below it, and the
fix-it add that puts someone already in the tree into another workspace of it.

The gap it exists to show: an editor of the org is NOT a member of its
divisions (only owners and admins inherit down), so an agent in a division
treats them as an outsider.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.workspaces import system_accounts
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _ws(slug, parent, by):
    return Workspace.objects.create(slug=slug, display_name=slug.title(), parent=parent, created_by=by)


def _join(ws, user, role):
    return WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


@pytest.fixture
def tree():
    """org ─┬─ connect ── field
            └─ labs
    owner owns org; editor edits org only; admin administers connect;
    labsie is a viewer of labs only."""
    owner, editor, admin, labsie = (_user(f"{n}@dimagi.com") for n in ("owner", "editor", "admin", "labsie"))
    org = _ws("org", None, owner)
    connect = _ws("connect", org, owner)
    field = _ws("field", connect, owner)
    labs = _ws("labs", org, owner)
    _join(org, owner, "owner")
    _join(org, editor, "editor")
    _join(connect, admin, "admin")
    _join(labs, labsie, "viewer")
    return {"owner": owner, "editor": editor, "admin": admin, "labsie": labsie,
            "org": org, "connect": connect, "field": field, "labs": labs}


def _add(c, slug, user_id, role):
    return c.post(f"/api/workspaces/{slug}/members/", data=json.dumps({"user_id": user_id, "role": role}),
                  content_type="application/json")


def _tree(c, slug):
    r = c.get(f"/api/workspaces/{slug}/member-tree")
    assert r.status_code == 200, r.content
    return r.json()


def _cells(body, email):
    return next(p for p in body["people"] if p["email"] == email)["cells"]


def test_the_tree_shows_direct_and_inherited_roles_in_tree_order(tree):
    body = _tree(_client(tree["owner"]), "org")
    assert [(w["slug"], w["depth"]) for w in body["workspaces"]] == [
        ("org", 0), ("connect", 1), ("field", 2), ("labs", 1)]
    assert all(w["can_manage"] for w in body["workspaces"])

    owner = _cells(body, "owner@dimagi.com")
    assert owner["org"] == {"role": "owner", "inherited": False, "inherited_from": None}
    assert owner["field"] == {"role": "owner", "inherited": True, "inherited_from": "org"}

    # The gap: an editor of the org is in the org only.
    assert set(_cells(body, "editor@dimagi.com")) == {"org"}

    admin = _cells(body, "admin@dimagi.com")
    assert admin["connect"]["inherited"] is False
    assert admin["field"] == {"role": "admin", "inherited": True, "inherited_from": "connect"}
    assert "org" not in admin and "labs" not in admin


def test_the_tree_hides_workspaces_the_caller_is_not_in(tree):
    body = _tree(_client(tree["admin"]), "connect")
    assert [w["slug"] for w in body["workspaces"]] == ["connect", "field"]
    # The org owner is in connect by inheritance, so is listed; labs' viewer is not.
    emails = {p["email"] for p in body["people"]}
    assert "owner@dimagi.com" in emails and "labsie@dimagi.com" not in emails
    # A workspace the caller is not in is 404, like every member read.
    assert _client(tree["admin"]).get("/api/workspaces/org/member-tree").status_code == 404


def test_a_viewer_sees_the_tree_but_may_manage_nothing(tree):
    body = _tree(_client(tree["labsie"]), "labs")
    assert [(w["slug"], w["can_manage"], w["my_role"]) for w in body["workspaces"]] == [("labs", False, "viewer")]


def test_add_puts_someone_from_the_tree_into_another_workspace(tree):
    r = _add(_client(tree["owner"]), "connect", tree["editor"].pk, "editor")
    assert r.status_code == 201, r.content
    assert r.json()["role"] == "editor" and r.json()["inherited"] is False
    assert WorkspaceMembership.objects.get(workspace=tree["connect"], user=tree["editor"]).role == "editor"
    assert _cells(_tree(_client(tree["owner"]), "org"), "editor@dimagi.com")["connect"]["role"] == "editor"


def test_add_refuses_someone_outside_the_tree(tree):
    stranger = _user("stranger@elsewhere.example")
    _join(_ws("other", None, stranger), stranger, "owner")
    nobody = _user("nobody@dimagi.com")
    c = _client(tree["owner"])
    assert _add(c, "connect", stranger.pk, "viewer").status_code == 404
    assert _add(c, "connect", nobody.pk, "viewer").status_code == 404
    assert _add(c, "connect", 999999, "viewer").status_code == 404
    assert not WorkspaceMembership.objects.filter(workspace=tree["connect"], user__in=[stranger, nobody]).exists()


def test_add_refuses_someone_the_caller_cannot_see(tree):
    # labsie is in the tree, but only in labs, which connect's admin is not in.
    assert _add(_client(tree["admin"]), "connect", tree["labsie"].pk, "viewer").status_code == 404


def test_add_refuses_an_existing_member(tree):
    _join(tree["connect"], tree["labsie"], "viewer")
    assert _add(_client(tree["owner"]), "connect", tree["labsie"].pk, "editor").status_code == 409
    assert WorkspaceMembership.objects.get(workspace=tree["connect"], user=tree["labsie"]).role == "viewer"


def test_an_admin_cannot_grant_admin(tree):
    # The editor is visible to connect's admin once both are in connect's tree view.
    _join(tree["field"], tree["editor"], "viewer")
    c = _client(tree["admin"])
    assert _add(c, "connect", tree["editor"].pk, "admin").status_code == 403
    assert _add(c, "connect", tree["editor"].pk, "editor").status_code == 201


def test_a_non_manager_cannot_add(tree):
    r = _add(_client(tree["editor"]), "org", tree["labsie"].pk, "viewer")
    assert r.status_code == 403
    # Not in connect at all: 404 before any role is probed.
    assert _add(_client(tree["labsie"]), "connect", tree["editor"].pk, "viewer").status_code == 404


def test_a_system_account_is_added_only_as_editor_or_viewer(tree):
    acct = system_accounts.create(tree["org"], name="alarms", by=tree["owner"])
    c = _client(tree["owner"])
    assert _add(c, "connect", acct.user_id, "admin").status_code == 422
    assert _add(c, "connect", acct.user_id, "viewer").status_code == 201
    body = _tree(c, "org")
    assert next(p for p in body["people"] if p["user_id"] == acct.user_id)["system"] is True
