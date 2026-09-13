"""Phase 0 of the agent-instances-and-ACL design: role gates on `/api/agents`.

Before this, `_get_agent_or_404` gated every write on workspace MEMBERSHIP
only — and back when `auto_join_workspaces` still existed, it handed out
`editor` to any allowlisted-domain user the instant they touched an agent
endpoint (that mechanism is gone as of the same design's self-join phase —
see `apps.workspaces.services.join_workspace` — but `editor` is still the
role a self-join grants, so it remains the role this file's tests exercise).
The credential/vault writers had no role check at all. See
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`
and `PHASE0-BRIEF.md`.

Per-endpoint coverage would be one test per line; these pin the TIERS instead:
- `_agent_for_write` (editor/owner) — reshaping the agent
- `_agent_for_admin` (owner only) — secrets + existence
- membership-only — the interaction tier a `viewer` must keep
plus the two security-critical specifics: a non-member gets 404 never 403 on
an owner endpoint, and an editor (the self-join default) is refused on all
three owner endpoints individually.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent, AgentTask
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

WS_SLUG = "acl-gate-ws"


@pytest.fixture
def acl(client):
    ws = a_workspace(WS_SLUG)  # named slug -> no self_join_domains (see testing.py)
    agent = Agent.objects.create(slug="aclbot", name="ACL Bot", workspace=ws, turn_mode="manual")
    owner = a_member(ws, email="acl-owner@dimagi.com", role=WorkspaceMembership.OWNER)
    editor = a_member(ws, email="acl-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    viewer = a_member(ws, email="acl-viewer@dimagi.com", role=WorkspaceMembership.VIEWER)
    nonmember = get_user_model().objects.create_user(
        username="acl-nonmember", email="acl-nonmember@dimagi.com"
    )
    return {
        "client": client, "ws": ws, "agent": agent,
        "owner": owner, "editor": editor, "viewer": viewer, "nonmember": nonmember,
    }


def _patch_turn_mode(client, mode="auto"):
    return client.patch(
        "/api/agents/aclbot/turn-mode",
        data={"turn_mode": mode}, content_type="application/json",
    )


def _put_credentials(client, values=None):
    return client.put(
        "/api/agents/aclbot/credentials",
        data={"values": values or {"a-secret": "shh"}}, content_type="application/json",
    )


def _put_vault(client):
    return client.put(
        "/api/agents/aclbot/vault",
        data={"vault": "op://acl-vault", "service_key": "sa-token"}, content_type="application/json",
    )


def _delete_credential(client, name="a-secret"):
    return client.delete(f"/api/agents/aclbot/credentials/{name}")


# --- viewer: refused on the reshaping tier and all three owner endpoints -------

def test_viewer_refused_on_representative_editor_endpoint(acl):
    acl["client"].force_login(acl["viewer"])
    res = _patch_turn_mode(acl["client"])
    assert res.status_code == 403, res.content


def test_viewer_refused_on_credentials(acl):
    acl["client"].force_login(acl["viewer"])
    assert _put_credentials(acl["client"]).status_code == 403


def test_viewer_refused_on_vault(acl):
    acl["client"].force_login(acl["viewer"])
    assert _put_vault(acl["client"]).status_code == 403


def test_viewer_refused_on_delete_credential(acl):
    acl["client"].force_login(acl["viewer"])
    assert _delete_credential(acl["client"]).status_code == 403


# --- editor: the auto-join default, refused on all three owner endpoints ------
# This is the finding's core: before Phase 0, an editor (i.e. anyone in the
# allowlisted domain) could overwrite an agent's encrypted credentials and
# vault pointer. Pin all three individually.

def test_editor_refused_on_credentials(acl):
    acl["client"].force_login(acl["editor"])
    res = _put_credentials(acl["client"])
    assert res.status_code == 403, res.content


def test_editor_refused_on_vault(acl):
    acl["client"].force_login(acl["editor"])
    res = _put_vault(acl["client"])
    assert res.status_code == 403, res.content


def test_editor_refused_on_delete_credential(acl):
    acl["client"].force_login(acl["editor"])
    res = _delete_credential(acl["client"])
    assert res.status_code == 403, res.content


# --- editor: succeeds on the reshaping tier ------------------------------------

def test_editor_succeeds_on_representative_editor_endpoint(acl):
    acl["client"].force_login(acl["editor"])
    res = _patch_turn_mode(acl["client"])
    assert res.status_code == 200, res.content
    acl["agent"].refresh_from_db()
    assert acl["agent"].turn_mode == "auto"


# --- viewer: still succeeds on the interaction tier ----------------------------

def test_viewer_succeeds_on_a_get(acl):
    acl["client"].force_login(acl["viewer"])
    res = acl["client"].get("/api/agents/aclbot/")
    assert res.status_code == 200, res.content


def test_viewer_succeeds_on_a_task_command(acl):
    task = AgentTask.objects.create(agent=acl["agent"], ext_id="t1", title="Task 1")
    acl["client"].force_login(acl["viewer"])
    res = acl["client"].post(
        f"/api/agents/aclbot/tasks/{task.id}/commands",
        data={"kind": "comment", "payload": {"note": "looks fine"}},
        content_type="application/json",
    )
    assert res.status_code == 201, res.content


# --- non-member: 404, never 403, on an owner endpoint --------------------------
# The property most likely to regress under refactoring: resolving the agent
# (membership, 404) must run BEFORE the role check (403), or a non-member
# learns the agent exists by getting 403 instead of 404.

def test_non_member_gets_404_not_403_on_owner_endpoint(acl):
    acl["client"].force_login(acl["nonmember"])
    res = _put_credentials(acl["client"])
    assert res.status_code == 404, res.content


def test_non_member_gets_404_not_403_on_write_tier_endpoint(acl):
    acl["client"].force_login(acl["nonmember"])
    res = _patch_turn_mode(acl["client"])
    assert res.status_code == 404, res.content
