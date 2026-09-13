"""Phase 0 of the agent-instances-and-ACL design: role gates on `/api/agents`.

Before this, `_get_agent_or_404` gated every write on workspace MEMBERSHIP
only — and back when `auto_join_workspaces` still existed, it handed out
`editor` to any allowlisted-domain user the instant they touched an agent
endpoint (that mechanism is gone as of the same design's self-join phase —
see `apps.workspaces.services.join_workspace` — but `editor` is still the
role a self-join grants, so it remains the role this file's tests exercise).
The credential/vault writers had no role check at all. See
`docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`,
whose Phase 0 this implements.

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


def _command(client, task, kind, payload=None):
    return client.post(
        f"/api/agents/aclbot/tasks/{task.id}/commands",
        data={"kind": kind, "payload": payload or {}},
        content_type="application/json",
    )


@pytest.fixture
def task(acl):
    return AgentTask.objects.create(agent=acl["agent"], ext_id="t1", title="Task 1")


@pytest.mark.parametrize("kind,payload", [
    ("comment", {"note": "looks fine"}),
    ("accept", {}),
    ("decline", {"reason": "not now"}),
])
def test_viewer_may_still_decide_an_item(acl, task, kind, payload):
    """The interaction tier, and the reason this endpoint is not simply gated.

    Commenting on a task and accepting or declining one are DECIDING an item
    that is already on the board — exactly what the User tier exists for
    ("decide an item, read the board"). Gating these would take the one thing a
    viewer is for away from them.
    """
    acl["client"].force_login(acl["viewer"])
    assert _command(acl["client"], task, kind, payload).status_code == 201


@pytest.mark.parametrize("kind,payload", [
    ("edit", {"title": "rewritten by a viewer"}),
    ("reassign", {"assignee": "someone-else"}),
    ("done", {}),
    ("dispatch", {}),
])
def test_viewer_refused_on_a_reshaping_command(acl, task, kind, payload):
    """The bypass this closes, and it was a door beside a gate that already existed.

    `PATCH /tasks/{id}/` is gated at `_agent_for_write`, and `kind: "edit"`
    reaches the SAME mutation through `services.create_command` — it sets
    title/next_action/plan/owner/assigned on the task and saves. So a viewer
    refused on the PATCH could perform it verbatim through the command queue.
    `done` rewrites status, `reassign` moves who holds the task, and `dispatch`
    queues fresh agent work.

    This is pinned per kind rather than once, because the gate is a membership
    test on a SET: adding a kind to `AgentTaskCommand.KIND_CHOICES` without
    adding it to `_RESHAPING_COMMAND_KINDS` silently lands it at the
    interaction tier, and a parametrized test is what makes that visible.
    """
    acl["client"].force_login(acl["viewer"])
    res = _command(acl["client"], task, kind, payload)
    assert res.status_code == 403, res.content
    task.refresh_from_db()
    assert task.title == "Task 1", "the refused command mutated the task anyway"


def test_editor_may_edit_via_a_command(acl, task):
    """The other half: the gate must not break the tier it belongs to."""
    acl["client"].force_login(acl["editor"])
    assert _command(acl["client"], task, "edit", {"title": "retitled"}).status_code == 201
    task.refresh_from_db()
    assert task.title == "retitled"


def test_non_member_gets_404_on_a_reshaping_command(acl, task):
    """Resolve-then-authorize survives the kind branch."""
    acl["client"].force_login(acl["nonmember"])
    assert _command(acl["client"], task, "edit", {"title": "x"}).status_code == 404


def test_every_command_kind_is_deliberately_tiered():
    """No kind may be left untiered by accident.

    The gate reads a set of RESHAPING kinds and treats everything else as
    interaction — which fails OPEN for a kind nobody classified. This asserts
    the two tiers partition `KIND_CHOICES`, so adding a kind forces a decision
    here rather than defaulting it to the viewer tier in silence.
    """
    from apps.agents.api import _RESHAPING_COMMAND_KINDS
    from apps.agents.models import AgentTaskCommand

    all_kinds = {k for k, _ in AgentTaskCommand.KIND_CHOICES}
    interaction = {AgentTaskCommand.ACCEPT, AgentTaskCommand.DECLINE, AgentTaskCommand.COMMENT}
    assert _RESHAPING_COMMAND_KINDS | interaction == all_kinds, (
        f"unclassified command kinds: {all_kinds - (_RESHAPING_COMMAND_KINDS | interaction)}"
    )
    assert not (_RESHAPING_COMMAND_KINDS & interaction)


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


# --- upsert: a non-member must not learn a slug is taken -----------------------

def test_upsert_of_another_tenants_agent_gives_404_not_403(acl):
    """`Agent.slug` is globally unique, which made this route an oracle.

    `POST /api/agents/` gates on the EXISTING agent's own workspace (so a
    caller cannot reshape another tenant's agent by defaulting into their own).
    That gate was correct and is what closed a real cross-tenant write — but it
    answered 403 to a NON-member, and 403-vs-201 on a globally unique slug tells
    you whether an agent by that name exists in a tenant you cannot see. Any
    editor of any workspace could enumerate the fleet's names one guess at a
    time.

    A non-member now gets the same 404 `GET /api/agents/aclbot/` gives them.
    """
    other_ws = a_workspace("oracle-probe-ws")
    prober = a_member(other_ws, email="prober@dimagi.com", role=WorkspaceMembership.OWNER)
    acl["client"].force_login(prober)
    res = acl["client"].post(
        "/api/agents/",
        data={"slug": "aclbot", "name": "Mine Now"},
        content_type="application/json",
    )
    assert res.status_code == 404, res.content
    acl["agent"].refresh_from_db()
    assert acl["agent"].name == "ACL Bot", "another tenant's agent was overwritten"
    assert acl["agent"].workspace_id == WS_SLUG


def test_upsert_of_a_free_slug_still_says_403_for_an_under_privileged_member(acl):
    """The 404 is scoped to an EXISTING agent, and that scoping matters.

    On a genuine create there is nothing to leak — the caller already knows the
    slug is free — so 404-ing them would be a lie about the only fact in
    evidence. A viewer in the target workspace gets a 403 that tells them
    something true and actionable about their own tenant.
    """
    acl["client"].force_login(acl["viewer"])
    res = acl["client"].post(
        "/api/agents/",
        data={"slug": "brand-new-bot", "name": "New"},
        content_type="application/json",
    )
    assert res.status_code == 403, res.content
    assert not Agent.objects.filter(slug="brand-new-bot").exists()
