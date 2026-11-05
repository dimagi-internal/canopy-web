"""The roles of a workspace over HTTP: who may define them, what they may say, and what stays safe.

`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents import access, interface, roles, turn_context
from apps.agents.models import Agent, AgentGrant, AgentRole
from apps.events.models import SettingChange
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import make_role

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant
BASE = "/api/workspaces/dimagi/agent-roles"


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    u = {n: mk(n, f"{n}@dimagi.com", "pw") for n in ("boss", "radmin", "ed", "vw", "out", "cadmin")}
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u["boss"])
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u["boss"], parent=root)
    M.objects.create(user=u["boss"], workspace=root, role=M.OWNER)
    M.objects.create(user=u["radmin"], workspace=root, role=M.ADMIN)
    M.objects.create(user=u["cadmin"], workspace=connect, role=M.ADMIN)
    M.objects.create(user=u["ed"], workspace=root, role=M.EDITOR)
    M.objects.create(user=u["vw"], workspace=root, role=M.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=u["boss"])
    return {"root": root, "connect": connect, "agent": agent, **u}


def _c(user, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


def _json(c, method, path, body=None):
    return getattr(c, method)(path, json.dumps(body) if body is not None else None, content_type="application/json")


DOOR = {"key": "connector", "label": "Connect door", "description": "Uses Connect", "permissions": ["read", "use:connect"]}
DRIVER = {"key": "pilot", "label": "Pilot", "permissions": ["write"]}


def test_a_member_reads_the_catalog_with_words_and_the_fixed_roles(w):
    r = _c(w["vw"], step_up=False).get(BASE)
    assert r.status_code == 200
    body = r.json()
    keys = [x["key"] for x in body["roles"]]
    assert keys == ["editor", "read_write", "read_only"]                   # strongest first
    by = {x["key"]: x for x in body["roles"]}
    assert by["read_only"]["builtin"] and by["read_only"]["defined_on"] == "dimagi"
    assert "drive the whole agent" in by["read_write"]["can"] and by["editor"]["contains_write"]
    assert [f["key"] for f in body["fixed"]] == ["owner", "admin"]
    assert body["can_edit"] is False and body["can_give_write"] is False


def test_a_child_sees_its_roots_roles_and_a_stranger_gets_404(w):
    stranger = get_user_model().objects.create_user("s", "s@else.org", "pw")
    assert _c(stranger, False).get(BASE).status_code == 404
    r = _c(w["cadmin"], False).get("/api/workspaces/connect/agent-roles")
    assert {x["key"] for x in r.json()["roles"]} == {"editor", "read_write", "read_only"}


def test_an_admin_defines_a_door_role_and_it_is_one_audited_change(w):
    r = _json(_c(w["radmin"]), "post", BASE, DOOR)
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["key"] == "connector" and out["permissions"] == ["read", "use:connect"] and not out["builtin"]
    assert any("use it from the 'connect' connected site" in c for c in out["can"])
    change = SettingChange.objects.get()
    assert change.changes[0]["scope"] == "agent-roles:dimagi:connector"
    assert "0 access-list line(s)" in change.summary
    assert AgentRole.objects.get(key="connector").permissions == ["read", "use:connect"]


@pytest.mark.parametrize("body", [DRIVER, {"key": "tinkerer", "label": "T", "permissions": ["agent_edit"]}])
def test_a_role_that_drives_or_changes_the_agent_is_the_owners(w, body):
    assert _json(_c(w["radmin"]), "post", BASE, body).status_code == 403
    r = _json(_c(w["boss"]), "post", BASE, body)
    assert r.status_code == 201
    assert {"read", "write"} <= set(r.json()["permissions"]) and r.json()["contains_write"]   # closed


def test_it_takes_a_passkey_step_up_from_a_browser(w):
    r = _json(_c(w["radmin"], step_up=False), "post", BASE, DOOR)
    assert r.status_code == 403 and r.json().get("error") == "step_up_required"


def test_a_token_may_define_a_role_that_only_reads_and_never_one_that_drives(w):
    raw, _ = PersonalToken.create_for_user(user=w["boss"], label="t")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert _json(c, "post", BASE, DOOR).status_code == 201
    r = _json(c, "post", BASE, DRIVER)
    assert r.status_code == 403 and "passkey" in r.json()["detail"]


@pytest.mark.parametrize("perms_", [["admin"], ["own"], [], ["use:*"], ["wirte"]])
def test_the_vocabulary_is_fixed_never_admin_or_own(w, perms_):
    r = _json(_c(w["boss"]), "post", BASE, {"key": "x", "label": "X", "permissions": perms_})
    assert r.status_code == 422


@pytest.mark.parametrize("key", ["owner", "admin", "system", "viewer", "user", "member", "contact", "Bad Key", "9x", "editor"])
def test_reserved_malformed_and_taken_keys_are_refused(w, key):
    r = _json(_c(w["boss"]), "post", BASE, {"key": key, "label": "X", "permissions": ["read"]})
    assert r.status_code in (409, 422)


def test_keys_are_unique_along_a_chain_in_both_directions(w):
    assert _json(_c(w["cadmin"]), "post", "/api/workspaces/connect/agent-roles", DOOR).status_code == 201
    r = _json(_c(w["boss"]), "post", BASE, DOOR)       # root defining what a child already defines
    assert r.status_code == 409 and "connect" in r.json()["detail"]
    other = {**DOOR, "key": "reader2"}
    assert _json(_c(w["boss"]), "post", BASE, other).status_code == 201
    r = _json(_c(w["cadmin"]), "post", "/api/workspaces/connect/agent-roles", other)   # child redefining
    assert r.status_code == 409 and "dimagi" in r.json()["detail"]


def test_a_role_defined_on_the_child_is_listed_there_and_not_above(w):
    _json(_c(w["cadmin"]), "post", "/api/workspaces/connect/agent-roles", DOOR)
    child = {x["key"] for x in _c(w["cadmin"], False).get("/api/workspaces/connect/agent-roles").json()["roles"]}
    root = {x["key"] for x in _c(w["radmin"], False).get(BASE).json()["roles"]}
    assert "connector" in child and "connector" not in root


def test_editing_a_role_changes_what_its_holders_get_and_closes_the_set(w):
    row = make_role(w["root"], "connector", {"read", "use:connect"})
    AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    assert access.decide(w["agent"], w["ed"], verified=True).access != access.FULL
    r = _json(_c(w["boss"]), "patch", f"{BASE}/connector", {"permissions": ["agent_edit"], "label": "Pilot"})
    assert r.status_code == 200, r.content
    assert set(r.json()["permissions"]) == {"read", "write", "agent_edit"} and r.json()["in_use"]["lines"] == 1
    assert access.decide(w["agent"], w["ed"], verified=True).access == access.FULL
    change = SettingChange.objects.latest("pk")
    assert "1 access-list line(s)" in change.summary and "1 agent(s)" in change.summary
    assert change.changes[0]["before"]["permissions"] == ["read", "use:connect"]
    assert row.key == "connector"


def test_an_admin_cannot_raise_a_role_to_drive_nor_lower_one_that_does(w):
    make_role(w["root"], "connector", {"read", "use:connect"})
    make_role(w["root"], "pilot", {"write"})
    assert _json(_c(w["radmin"]), "patch", f"{BASE}/connector", {"permissions": ["write"]}).status_code == 403
    assert _json(_c(w["radmin"]), "patch", f"{BASE}/pilot", {"permissions": ["read"]}).status_code == 403
    assert _json(_c(w["radmin"]), "patch", f"{BASE}/connector", {"label": "Renamed"}).status_code == 200


def test_the_address_only_rail_holds_across_an_edit(w):
    """A verified-domain line can never drive an agent; editing the role it holds cannot change that."""
    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.DOMAIN, domain="dimagi.com", role="connector")
    r = _json(_c(w["boss"]), "patch", f"{BASE}/connector", {"permissions": ["write"]})
    assert r.status_code == 422 and "address" in r.json()["detail"]
    r = _json(_c(w["boss"]), "patch", f"{BASE}/connector", {"permissions": ["agent_edit"]})
    assert r.status_code == 422
    assert AgentRole.objects.get(key="connector").permissions == ["read", "use:connect"]
    line.delete()
    assert _json(_c(w["boss"]), "patch", f"{BASE}/connector", {"permissions": ["write"]}).status_code == 200


def test_a_builtin_is_editable_resettable_and_never_deletable(w):
    r = _json(_c(w["radmin"]), "patch", f"{BASE}/read_only", {"label": "Reader", "permissions": ["read", "use:connect"]})
    assert r.status_code == 200 and r.json()["label"] == "Reader"
    r = _json(_c(w["radmin"]), "post", f"{BASE}/read_only/reset")
    assert r.status_code == 200 and r.json()["label"] == "Read only" and r.json()["permissions"] == ["read"]
    assert _json(_c(w["boss"]), "delete", f"{BASE}/read_only").status_code == 409
    assert _json(_c(w["boss"]), "post", f"{BASE}/connector/reset").status_code == 404


def test_a_role_in_use_is_not_deleted_until_its_holders_move(w):
    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    r = _json(_c(w["radmin"]), "delete", f"{BASE}/connector")
    assert r.status_code == 409 and "ed@dimagi.com on hal" in r.json()["detail"]
    r = _json(_c(w["radmin"]), "delete", f"{BASE}/connector?reassign_to=read_only")
    assert r.status_code == 204
    line.refresh_from_db()
    assert line.role == "read_only" and line.role_def.key == "read_only"
    assert not AgentRole.objects.filter(key="connector").exists()
    assert "moving 1 line(s)" in SettingChange.objects.latest("pk").summary


def test_reassigning_to_a_role_that_drives_is_refused_for_address_lines_and_non_owners(w):
    make_role(w["root"], "connector", {"read", "use:connect"})
    AgentGrant.objects.create(agent=w["agent"], kind=G.DOMAIN, domain="dimagi.com", role="connector")
    assert _json(_c(w["radmin"]), "delete", f"{BASE}/connector?reassign_to=editor").status_code == 403
    r = _json(_c(w["boss"]), "delete", f"{BASE}/connector?reassign_to=editor")
    assert r.status_code == 409 and "address" in r.json()["detail"]
    assert AgentRole.objects.filter(key="connector").exists()


def test_a_role_above_is_changed_there_not_from_the_child(w):
    r = _json(_c(w["cadmin"]), "patch", "/api/workspaces/connect/agent-roles/read_only", {"label": "x"})
    assert r.status_code == 404 and "dimagi" in r.json()["detail"]


def test_a_role_is_available_to_an_access_line_below_and_rails_follow_its_contents(w):
    make_role(w["root"], "connector", {"read", "use:connect"})
    make_role(w["root"], "pilot", {"write"})
    c = _c(w["boss"], step_up=False)
    body = {"kind": "domain", "role": "connector", "domain": "dimagi.com"}
    assert _json(c, "post", "/api/agents/hal/access-list", body).status_code == 201
    r = _json(c, "post", "/api/agents/hal/access-list", {"kind": "contact", "role": "pilot"})
    assert r.status_code == 422 and "drive the whole agent" in r.json()["detail"]
    r = _json(c, "post", "/api/agents/hal/access-list", {"kind": "person", "role": "nope", "email": "ed@dimagi.com"})
    assert r.status_code == 422 and "no role called" in r.json()["detail"]
    ok = _json(c, "post", "/api/agents/hal/access-list", {"kind": "person", "role": "pilot", "email": "ed@dimagi.com"})
    assert ok.status_code == 201 and ok.json()["role"] == "pilot"
    listing = c.get("/api/agents/hal/access-list").json()
    by = {x["role"]: x for x in listing["roles"]}
    assert by["pilot"]["contains_write"] and not by["connector"]["contains_write"]
    assert by["connector"]["label"] == "Connector" and by["pilot"]["defined_on"] == "dimagi"


def test_roles_are_mcp_tools_without_an_exclusion(w):
    from asgiref.sync import async_to_sync

    from apps.mcp.server import mcp

    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"list_agent_roles", "create_agent_role", "change_agent_role", "reset_agent_role",
            "delete_agent_role"} <= set(names)


def test_a_move_carries_a_role_only_where_the_destination_has_the_same_one(w):
    from apps.agents import move

    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["boss"])
    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    keep = AgentGrant.objects.create(agent=w["agent"], kind=G.DOMAIN, domain="x.org", role="read_only")

    plan = move.plan(w["agent"], other, by=w["boss"])
    assert [b["kind"] for b in plan.stops_working if b["kind"] == "access_list_roles"] == ["access_list_roles"]
    with pytest.raises(move.MoveError, match="connector"):
        move.move(w["agent"], other, by=w["boss"])
    w["agent"].refresh_from_db()
    assert w["agent"].workspace_id == "connect"                      # nothing moved

    make_role(other, "connector", {"use:connect", "read"})            # the same meaning there
    move.move(w["agent"], other, by=w["boss"])
    line.refresh_from_db()
    keep.refresh_from_db()
    assert line.role_def.workspace_id == "other" and keep.role_def.workspace_id == "other"
    assert keep.role_def.key == "read_only" and keep.role_def.builtin is True


def test_a_move_inside_one_tree_leaves_every_line_where_it_is(w):
    from apps.agents import move

    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    move.move(w["agent"], w["root"], by=w["boss"])
    line.refresh_from_db()
    assert line.role_def.workspace_id == "dimagi"


def test_a_token_cannot_move_holders_onto_a_role_that_drives(w):
    """Deleting a harmless role is token-safe; moving its holders onto `editor` hands them `agent_edit`."""
    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    raw, _ = PersonalToken.create_for_user(user=w["boss"], label="t")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = c.delete(f"{BASE}/connector?reassign_to=editor")
    assert r.status_code == 403 and "passkey" in r.json()["detail"]
    line.refresh_from_db()
    assert line.role == "connector"
    assert c.delete(f"{BASE}/connector?reassign_to=read_only").status_code == 204


def test_adding_agent_edit_to_a_write_role_is_refused_while_an_address_line_holds_it(w):
    """`read_write` already drives, so the rail must ask what the edit adds, not only whether it drives."""
    make_role(w["root"], "pilot", {"read", "write"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.DOMAIN, domain="dimagi.com", role="pilot")
    r = _json(_c(w["boss"]), "patch", f"{BASE}/pilot", {"permissions": ["read", "write", "agent_edit"]})
    assert r.status_code == 422 and "address" in r.json()["detail"]
    assert "agent_edit" not in AgentRole.objects.get(key="pilot").permissions
    assert _json(_c(w["boss"]), "patch", f"{BASE}/pilot", {"label": "Pilot"}).status_code == 200
    line.delete()
    assert _json(_c(w["boss"]), "patch", f"{BASE}/pilot", {"permissions": ["agent_edit"]}).status_code == 200


def test_a_session_writers_envelope_stops_at_write():
    """They drive this conversation, not the agent: the own-repo rail reads `agent_edit` from this list."""
    d = turn_context.TurnContext(
        standing=turn_context.Standing(relationship="member", agent_role=roles.EDITOR, source="session",
                                       permissions=roles.ROLES.close({roles.WRITE})),
        access=turn_context.FULL, profile=turn_context.Profile(kind=turn_context.WHOLE_AGENT), basis="session:editor",
        session_key_suffix="", review=turn_context.Review(), stamped="", refusal=None, trace=())
    held = interface._held_permissions(d, None)
    assert "write" in held and "agent_edit" not in held


def _move(user, slug, parent):
    return _json(_c(user), "put", f"/api/workspaces/{slug}/parent", {"parent": parent})


def test_a_workspace_detached_from_its_tree_takes_only_roles_that_mean_the_same(w):
    """After detaching, a line must not hold a row the old tree's owners go on editing."""
    M.objects.create(user=w["boss"], workspace=w["connect"], role=M.OWNER)
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="read_write")
    old = line.role_def
    assert old.workspace_id == "dimagi"
    assert _move(w["boss"], "connect", None).status_code == 200
    line.refresh_from_db()
    assert line.role_def_id != old.pk and line.role_def.workspace_id == "connect"
    assert line.role_def.key == "read_write"


def test_a_move_is_refused_while_a_line_holds_a_role_with_no_equal_there(w):
    M.objects.create(user=w["boss"], workspace=w["connect"], role=M.OWNER)
    make_role(w["root"], "connector", {"read", "use:connect"})
    line = AgentGrant.objects.create(agent=w["agent"], kind=G.PERSON, user=w["ed"], role="connector")
    r = _move(w["boss"], "connect", None)
    assert r.status_code == 422 and "connector" in r.json()["detail"]
    w["connect"].refresh_from_db()
    assert w["connect"].parent_id == "dimagi"
    line.delete()
    assert _move(w["boss"], "connect", None).status_code == 200


def test_nesting_a_root_repoints_its_lines_at_the_roles_above(w):
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["boss"])
    M.objects.create(user=w["boss"], workspace=other, role=M.OWNER)
    agent = Agent.objects.create(slug="ott", name="Ott", workspace=other, owner=w["boss"])
    line = AgentGrant.objects.create(agent=agent, kind=G.PERSON, user=w["ed"], role="read_only")
    assert line.role_def.workspace_id == "other"
    assert _move(w["boss"], "other", "dimagi").status_code == 200
    line.refresh_from_db()
    assert line.role_def.workspace_id == "dimagi"
