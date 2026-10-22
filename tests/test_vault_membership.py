"""Vault membership is first-class (spec 2026-10-22-vault-access-follows-canopy-access-design.md).

A vault's members are its direct members plus its links; an agent's vault starts
linked to the agent. A machine may hold a vault's key only while the person it
works for is a member, so approving a machine never grants on its own.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.credentials import vault_members, zk
from apps.agents.models import Agent, AgentAdmin, AgentLicense, Vault, VaultLink, VaultMember
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.models import UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

JWK = {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}


def _user(name: str):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _member(ws, user, role):
    return WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


@pytest.fixture
def world():
    class W:
        pass
    w = W()
    w.owner = _user("jj")
    w.org = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w.owner)
    w.ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=w.owner, parent=w.org)
    _member(w.ws, w.owner, WorkspaceMembership.OWNER)
    w.agent = Agent.objects.create(slug="ace", name="ACE", workspace=w.ws, owner=w.owner)
    w.vault = vault_members.vault_for_agent(w.agent)
    return w


def _keyset(user):
    return UserVaultKeyset.objects.create(user=user, public_jwk=JWK, seals=[])


def _device(agent, runner_owner, approved_by=None, name="box"):
    runner = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=runner_owner, status=Runner.ONLINE,
                                   workspace=agent.workspace, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return RunnerDeviceKey.objects.create(
        runner=runner, public_jwk=JWK, jkt=f"jkt-{name}", kind="secure-enclave", label=name,
        status=RunnerDeviceKey.ACTIVE, ecdh_public_jwk=JWK, ecdh_jkt=f"ecdh-{name}",
        ecdh_approved_at=timezone.now(), approved_by=approved_by)


# ---- the vault and its link -------------------------------------------------------------

def test_an_agent_vault_is_made_once_and_starts_linked_to_its_agent(world):
    assert vault_members.vault_for_agent(world.agent) == world.vault
    assert Vault.objects.filter(agent=world.agent).count() == 1
    link = VaultLink.objects.get(vault=world.vault)
    assert (link.kind, link.agent, link.role) == ("agent", world.agent, "use")


def test_full_access_makes_a_member_and_owners_and_admins_manage(world):
    editor, viewer, admin = _user("ed"), _user("vi"), _user("ad")
    _member(world.ws, editor, WorkspaceMembership.EDITOR)
    _member(world.ws, viewer, WorkspaceMembership.VIEWER)
    _member(world.ws, admin, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=world.agent, user=admin, granted_by=world.owner)
    ms = vault_members.members(world.vault)
    assert ms[world.owner.pk].role == "manage" and ms[admin.pk].role == "manage"
    assert ms[editor.pk].role == "use" and ms[editor.pk].sources == ["agent:ace"]
    assert viewer.pk not in ms


def test_an_active_license_makes_a_member_through_the_link(world):
    viewer = _user("vi")
    _member(world.ws, viewer, WorkspaceMembership.VIEWER)
    lic = AgentLicense.objects.create(agent=world.agent, user=viewer, status=AgentLicense.ACTIVE,
                                      basis="approved by jj@dimagi.com")
    assert vault_members.members(world.vault)[viewer.pk].sources == ["license:ace"]
    lic.status = AgentLicense.REVOKED
    lic.save()
    assert viewer.pk not in vault_members.members(world.vault)


def test_losing_full_access_drops_only_the_linked_membership(world):
    editor, also = _user("ed"), _user("al")
    ed_row = _member(world.ws, editor, WorkspaceMembership.EDITOR)
    al_row = _member(world.ws, also, WorkspaceMembership.EDITOR)
    vault_members.add_member(world.vault, also, role="use", by=world.owner)
    assert vault_members.members(world.vault)[also.pk].sources == ["direct", "agent:ace"]
    ed_row.role = al_row.role = WorkspaceMembership.VIEWER
    ed_row.save()
    al_row.save()
    ms = vault_members.members(world.vault)
    assert editor.pk not in ms
    assert ms[also.pk].sources == ["direct"]


def test_unlinking_keeps_direct_members_and_needs_a_manager(world):
    editor = _user("ed")
    _member(world.ws, editor, WorkspaceMembership.EDITOR)
    with pytest.raises(vault_members.VaultMembershipError) as e:
        vault_members.set_agent_link(world.vault, world.agent, linked=False)
    assert e.value.code == "vault_needs_a_manager"
    vault_members.add_member(world.vault, world.owner, role="manage", by=world.owner)
    vault_members.set_agent_link(world.vault, world.agent, linked=False)
    assert set(vault_members.members(world.vault)) == {world.owner.pk}


def test_a_direct_member_must_be_in_the_workspace(world):
    outsider = _user("out")
    with pytest.raises(vault_members.VaultMembershipError) as e:
        vault_members.add_member(world.vault, outsider, role="use", by=world.owner)
    assert e.value.code == "vault_not_in_workspace"
    assert not VaultMember.objects.exists()


def test_a_workspace_owners_link_flows_down_the_tree(world):
    org_owner = _user("org")
    _member(world.org, org_owner, WorkspaceMembership.OWNER)
    VaultLink.objects.create(vault=world.vault, kind="workspace_owners", workspace=world.ws, role="use")
    ms = vault_members.members(world.vault)
    assert "workspace_owners:connect" in ms[org_owner.pk].sources


# ---- the person layer reaches the vault's key copies -------------------------------------

def test_wraps_go_to_members_with_a_vault_key_not_only_admins(world):
    editor, viewer = _user("ed"), _user("vi")
    _member(world.ws, editor, WorkspaceMembership.EDITOR)
    _member(world.ws, viewer, WorkspaceMembership.VIEWER)
    for u in (world.owner, editor, viewer):
        _keyset(u)
    assert {k.user_id for k in zk.eligible_members(world.agent)} == {world.owner.pk, editor.pk}


def test_a_machine_needs_its_person_to_be_a_member(world):
    """A box that serves the agent is not enough: the person it works for must
    be a member of the vault. Here the vault follows its own list (unlinked), so
    an agent admin's box serves the agent and still gets nothing until they are added."""
    from apps.harness import device_keys  # noqa: PLC0415

    editor = _user("ed")
    _member(world.ws, editor, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=world.agent, user=editor, granted_by=world.owner)
    theirs = _device(world.agent, editor, approved_by=editor, name="theirs")
    assert theirs in set(device_keys.keys_for_agent(world.agent))
    assert theirs in set(zk.eligible_device_keys(world.agent))  # linked: the admin is a member
    vault_members.add_member(world.vault, world.owner, role="manage", by=world.owner)
    vault_members.set_agent_link(world.vault, world.agent, linked=False)
    assert theirs not in set(zk.eligible_device_keys(world.agent))
    assert not any(t["kind"] == "device" for t in zk.pending_targets(world.agent))
    vault_members.add_member(world.vault, editor, role="use", by=world.owner)
    assert theirs in set(zk.eligible_device_keys(world.agent))


def test_a_machine_works_for_its_runner_owner_else_its_approver(world):
    key = _device(world.agent, world.owner, approved_by=None, name="owned")
    assert vault_members.person_of_device(key) == world.owner.pk
    key.runner.owner = None
    key.approved_by = world.owner
    assert vault_members.person_of_device(key) == world.owner.pk


# ---- the API ----------------------------------------------------------------------------

def test_members_api_lists_sources_and_your_role(world, client):
    client.force_login(world.owner)
    body = client.get("/api/agents/ace/vault/members").json()
    assert body["you"] == "manage" and body["agent_link"] == {"linked": True, "role": "use"}
    assert [(m["email"], m["role"], m["sources"]) for m in body["members"]] == [
        ("jj@dimagi.com", "manage", ["agent:ace"])]


def test_only_a_manager_changes_membership(world, client):
    editor = _user("ed")
    _member(world.ws, editor, WorkspaceMembership.EDITOR)
    client.force_login(editor)
    grant_step_up(client)
    res = client.post("/api/agents/ace/vault/members", data={"email": "ed@dimagi.com", "role": "manage"},
                      content_type="application/json")
    assert res.status_code == 403


def test_a_manager_adds_and_removes_a_direct_member(world, client):
    teammate = _user("tm")
    _member(world.ws, teammate, WorkspaceMembership.VIEWER)
    client.force_login(world.owner)
    grant_step_up(client)
    res = client.post("/api/agents/ace/vault/members", data={"email": "tm@dimagi.com", "role": "use"},
                      content_type="application/json")
    assert res.status_code == 200, res.content
    assert {m["email"]: m["sources"] for m in res.json()["members"]}["tm@dimagi.com"] == ["direct"]
    res = client.delete(f"/api/agents/ace/vault/members/{teammate.pk}")
    assert res.status_code == 200
    assert "tm@dimagi.com" not in {m["email"] for m in res.json()["members"]}


def test_unlinking_the_last_managers_is_refused(world, client):
    client.force_login(world.owner)
    grant_step_up(client)
    res = client.put("/api/agents/ace/vault/agent-link", data={"linked": False},
                     content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "vault_needs_a_manager"


def test_an_agents_login_or_a_system_account_is_never_a_member(world):
    """They hold no vault key, and an agent able to change its own vault's membership
    could hand its key to anyone. 2026-10-22: in production ACE's own login and Ada's were
    listed as managers of ACE's vault, and an alarm sender as a user."""
    from apps.workspaces.models import SystemAccount

    login = _user("ace-login")
    _member(world.ws, login, WorkspaceMembership.EDITOR)
    world.agent.user = login
    world.agent.save(update_fields=["user"])
    AgentAdmin.objects.create(agent=world.agent, user=login, granted_by=world.owner)
    alarms = _user("alarms")
    _member(world.ws, alarms, WorkspaceMembership.EDITOR)
    SystemAccount.objects.create(workspace=world.ws, user=alarms, name="alarms")
    ms = vault_members.members(world.vault)
    assert login.pk not in ms and alarms.pk not in ms and world.owner.pk in ms
    with pytest.raises(vault_members.VaultMembershipError) as e:
        vault_members.add_member(world.vault, login, role="manage", by=world.owner)
    assert e.value.code == "vault_not_a_person"
