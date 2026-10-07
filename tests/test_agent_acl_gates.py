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
    ws = a_workspace(WS_SLUG)  # named slug -> no access_request_domains (see testing.py)
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
    res = acl["client"].patch("/api/agents/aclbot/turn-mode", {"turn_mode": "manual"},
                              content_type="application/json")
    assert res.status_code == 200, res.content
    acl["agent"].refresh_from_db()
    assert acl["agent"].turn_mode == "manual"


def test_editor_refused_auto_on_the_agents_switch(acl):
    # `auto` is the last rung of the routing ladder: an agent admin's to set
    # (docs/architecture/access.md). An editor may set manual.
    acl["client"].force_login(acl["editor"])
    res = _patch_turn_mode(acl["client"])
    assert res.status_code == 403, res.content


# --- viewer: still succeeds on the interaction tier ----------------------------

def test_viewer_succeeds_on_a_get(acl):
    acl["client"].force_login(acl["viewer"])
    res = acl["client"].get("/api/agents/aclbot/")
    assert res.status_code == 200, res.content


def _act(client, task, action, comment=""):
    return client.post(
        f"/api/agents/aclbot/tasks/{task.ext_id}/actions",
        data={"action": action, "comment": comment},
        content_type="application/json",
    )


@pytest.fixture
def task(acl):
    return AgentTask.objects.create(agent=acl["agent"], ext_id="T1", title="Task 1",
                                    ask_kind=AgentTask.ASK_REVIEW)


@pytest.mark.parametrize("action,comment", [
    ("reply", "looks fine"),
    ("approve", ""),
    ("decline", "not now"),
])
def test_viewer_may_still_answer_a_task(acl, task, action, comment):
    """The interaction tier, and the reason this endpoint is not simply gated.

    Approving, declining or replying to a task answers what is already on the
    board — exactly what the User tier exists for. Gating these would take the
    one thing a viewer is for away from them.
    """
    acl["client"].force_login(acl["viewer"])
    assert _act(acl["client"], task, action, comment).status_code == 200


@pytest.mark.parametrize("action", ["done", "dispatch"])
def test_viewer_refused_on_a_reshaping_action(acl, task, action):
    """`done` rewrites status and `dispatch` queues fresh agent work — reshapes,
    so editor. Pinned per action because the gate is a membership test on a SET:
    an action added to the model without a tier decision must be visible here."""
    acl["client"].force_login(acl["viewer"])
    res = _act(acl["client"], task, action)
    assert res.status_code == 403, res.content
    task.refresh_from_db()
    assert task.status == "suggested", "the refused action changed the task anyway"
    assert not task.actions.exists()


def test_viewer_refused_on_patch(acl, task):
    """Field edits (what `edit`/`reassign` commands used to do) are a PATCH, editor-gated."""
    acl["client"].force_login(acl["viewer"])
    res = acl["client"].patch("/api/agents/aclbot/tasks/T1/", data={"title": "rewritten"},
                              content_type="application/json")
    assert res.status_code == 403
    task.refresh_from_db()
    assert task.title == "Task 1"


def test_editor_may_patch_and_mark_done(acl, task):
    """The other half: the gate must not break the tier it belongs to."""
    acl["client"].force_login(acl["editor"])
    assert acl["client"].patch("/api/agents/aclbot/tasks/T1/", data={"title": "retitled"},
                               content_type="application/json").status_code == 200
    assert _act(acl["client"], task, "done").status_code == 200
    task.refresh_from_db()
    assert task.title == "retitled" and task.status == "done"


def test_non_member_gets_404_on_a_reshaping_action(acl, task):
    """Resolve-then-authorize survives the action branch."""
    acl["client"].force_login(acl["nonmember"])
    assert _act(acl["client"], task, "done").status_code == 404


def test_every_action_is_deliberately_tiered():
    """No action may be left untiered by accident.

    The gate reads a set of EDITOR actions and treats everything else as
    interaction — which fails OPEN for an action nobody classified. This asserts
    the two tiers partition the model's choices.
    """
    from apps.agents.api import _EDITOR_ACTIONS
    from apps.agents.models import AgentTaskAction

    all_actions = {a for a, _ in AgentTaskAction.ACTION_CHOICES}
    interaction = {AgentTaskAction.APPROVE, AgentTaskAction.DECLINE, AgentTaskAction.REPLY}
    assert _EDITOR_ACTIONS | interaction == all_actions, (
        f"unclassified actions: {all_actions - (_EDITOR_ACTIONS | interaction)}"
    )
    assert not (_EDITOR_ACTIONS & interaction)


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
