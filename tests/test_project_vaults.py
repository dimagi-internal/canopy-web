"""A workspace vault attached to a PROJECT serves the project's current member agents.

The case that shaped it: `dimagi/canopy-support` carries skills for the whole organisation,
one of them needs a secret (`GOOGLE_PROVISIONER_KEY`), and the agents that joined the project
to use the skills should get it without anyone attaching the vault agent by agent.

The rule under test: the vault serves a member agent while it is a member NOW (nothing is
stored, nothing swept) and only if the agent and the project both lie in the vault's own
workspace tree. Every consumer agrees: the turn's layers, the machine rule, the membership link.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.agents import move, services
from apps.agents.credentials import vault_members, workspace_vaults
from apps.agents.models import (
    Agent,
    AgentAdmin,
    AgentProject,
    ProjectShare,
    Vault,
    VaultAttachment,
    VaultKeyWrap,
    VaultLink,
    VaultSecret,
)
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import editors_run

pytestmark = pytest.mark.django_db

J = "application/json"
JWK = {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _member(ws, user, role):
    return WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


def _client(user):
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


@pytest.fixture
def w():
    class W:
        pass

    w = W()
    w.jj = _user("jj")
    w.dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w.jj)
    w.connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=w.jj, parent=w.dimagi)
    w.team = Workspace.objects.create(slug="connect-team", display_name="Team", created_by=w.jj, parent=w.connect)
    w.labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=w.jj, parent=w.dimagi)
    _member(w.dimagi, w.jj, WorkspaceMembership.OWNER)
    w.ada = Agent.objects.create(slug="ada", name="Ada", workspace=w.dimagi, owner=w.jj)
    w.hal = Agent.objects.create(slug="hal", name="Hal", workspace=w.connect, owner=w.jj)
    w.deep = Agent.objects.create(slug="deep", name="Deep", workspace=w.team, owner=w.jj)
    w.lab = Agent.objects.create(slug="lab", name="Lab", workspace=w.labs, owner=w.jj)
    w.eve = Agent.objects.create(slug="eve", name="Eve", workspace=w.connect, owner=w.jj)
    # The org-wide project, led by ada, and a connect project led by hal.
    w.support = AgentProject.objects.create(agent=w.ada, ext_id="P1", name="Canopy support", workspace=w.dimagi,
                                            handle="canopy-support")
    w.proj = AgentProject.objects.create(agent=w.hal, ext_id="P1", name="Connect work", workspace=w.connect,
                                         handle="connect-work")
    ProjectShare.objects.create(project=w.proj, workspace=w.team)   # deep may join it
    w.org_vault = workspace_vaults.create(w.dimagi, "Google", by=w.jj)
    w.vault = workspace_vaults.create(w.connect, "Shared", by=w.jj)
    return w


def _join(project, *agents):
    for a in agents:
        project.member_agents.add(a)


def _layers(agent):
    return [v.name for v in workspace_vaults.layers_for(agent)]


def _served(vault):
    return {a.slug for a in workspace_vaults.attached_agents(vault)}


# ---- the turn's layers ---------------------------------------------------------------------

def test_a_project_vault_is_layered_into_a_member_agents_turns_and_no_ones_else(w):
    services.set_member_agents(w.proj, [w.hal, w.deep])
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    assert _layers(w.hal) == ["Shared"] and _layers(w.deep) == ["Shared"]   # the lead is a member
    assert _layers(w.eve) == [] and _layers(w.ada) == [] and _layers(w.lab) == []
    assert _served(w.vault) == {"hal", "deep"}


def test_joining_and_leaving_the_project_changes_it_with_no_sweep(w):
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    rows = list(VaultAttachment.objects.values_list("pk", flat=True))
    assert _layers(w.eve) == []
    w.proj.member_agents.add(w.eve)
    assert _layers(w.eve) == ["Shared"] and "eve" in _served(w.vault)
    w.proj.member_agents.remove(w.eve)
    assert _layers(w.eve) == [] and "eve" not in _served(w.vault)
    assert list(VaultAttachment.objects.values_list("pk", flat=True)) == rows   # nothing was written


def test_a_member_outside_the_vaults_tree_is_not_served_and_is_listed_as_not_served(w):
    ProjectShare.objects.create(project=w.proj, workspace=w.labs)
    services.set_member_agents(w.proj, [w.hal, w.lab, w.ada])   # ada is of the org ABOVE connect
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    assert _layers(w.lab) == [] and _layers(w.ada) == [] and _served(w.vault) == {"hal"}
    served, unserved = vault_members.project_agents(w.vault, w.proj)
    assert [a.slug for a in served] == ["hal"] and [a.slug for a in unserved] == ["ada", "lab"]
    c = _client(w.jj)
    [att] = c.get(f"/api/vaults/{w.vault.pk}").json()["attachments"]
    assert att["project"] == "connect/connect-work"
    assert att["served"] == ["hal"] and att["not_served"] == ["ada", "lab"] and not att["project_outside_tree"]


def test_an_org_wide_vault_serves_a_division_agent_that_joined_through_a_share(w):
    ProjectShare.objects.create(project=w.support, workspace=w.connect)
    services.set_member_agents(w.support, [w.ada, w.hal, w.deep])
    workspace_vaults.attach(w.org_vault, project=w.support, by=w.jj)
    assert _layers(w.hal) == ["Google"] and _layers(w.deep) == ["Google"] and _layers(w.ada) == ["Google"]
    assert _layers(w.eve) == []


def test_revoking_the_share_removes_the_shared_agents_vault_access(w):
    ProjectShare.objects.create(project=w.support, workspace=w.connect)
    services.set_member_agents(w.support, [w.ada, w.hal])
    workspace_vaults.attach(w.org_vault, project=w.support, by=w.jj)
    assert _layers(w.hal) == ["Google"]
    assert services.unshare_project(w.support, "connect") == ["hal"]
    assert _layers(w.hal) == [] and _served(w.org_vault) == {"ada"}


def test_the_vault_layers_between_the_workspaces_and_the_agents_own_attachment(w):
    other = workspace_vaults.create(w.dimagi, "Tree", by=w.jj)
    direct = workspace_vaults.create(w.connect, "Direct", by=w.jj)
    workspace_vaults.attach(direct, agent=w.hal, by=w.jj)
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    workspace_vaults.attach(other, workspace=w.dimagi, by=w.jj)
    assert _layers(w.hal) == ["Tree", "Shared", "Direct"]   # a later layer wins a name clash


def test_a_project_that_left_the_vaults_tree_serves_nobody_even_before_any_sweep(w):
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    # Moved by hand, bypassing `promote_project`: the read still refuses.
    AgentProject.objects.filter(pk=w.proj.pk).update(workspace=w.labs)
    w.proj.refresh_from_db()
    assert _layers(w.hal) == [] and _served(w.vault) == set()
    [att] = _client(w.jj).get(f"/api/vaults/{w.vault.pk}").json()["attachments"]
    assert att["project_outside_tree"] and att["served"] == [] and att["not_served"] == ["hal"]


def test_the_turn_layers_and_the_served_agents_always_agree(w):
    ProjectShare.objects.create(project=w.proj, workspace=w.labs)
    services.set_member_agents(w.proj, [w.hal, w.deep, w.lab, w.ada])
    for vault in (w.vault, w.org_vault):
        workspace_vaults.attach(vault, project=w.proj, by=w.jj)
    for agent in (w.ada, w.hal, w.deep, w.lab, w.eve):
        for vault in (w.vault, w.org_vault):
            assert (vault in workspace_vaults.layers_for(agent)) == (agent in workspace_vaults.attached_agents(vault)), \
                (agent.slug, vault.name)


# ---- the machine rule ----------------------------------------------------------------------

def _device(agent, owner, name):
    runner = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=owner, status=Runner.ONLINE,
                                   workspace=agent.workspace, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return RunnerDeviceKey.objects.create(
        runner=runner, public_jwk=JWK, jkt=f"jkt-{name}", kind="secure-enclave", label=name,
        status=RunnerDeviceKey.ACTIVE, ecdh_public_jwk=JWK, ecdh_jkt=f"ecdh-{name}",
        ecdh_approved_at=timezone.now(), approved_by=owner)


def test_a_machine_of_a_project_served_agent_may_hold_the_key_only_for_a_vault_member(w):
    adm = _user("adm")
    _member(w.team, adm, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w.deep, user=adm, granted_by=w.jj)
    editors_run(w.deep)
    services.set_member_agents(w.proj, [w.hal, w.deep])
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    key = _device(w.deep, adm, "box-deep")
    # adm administers deep, a served agent, so the attached link makes them a member ...
    assert vault_members.role_of(w.vault, adm) == "member"
    assert workspace_vaults.device_may_hold(w.vault, key) and key in workspace_vaults.eligible_device_keys(w.vault)
    # ... and a machine whose person is not a member holds nothing, however it is routed.
    VaultLink.objects.filter(vault=w.vault, kind=VaultLink.ATTACHED).delete()
    assert vault_members.role_of(w.vault, adm) is None
    assert not workspace_vaults.device_may_hold(w.vault, key)
    assert key not in workspace_vaults.eligible_device_keys(w.vault)


def test_a_machine_of_an_agent_the_project_does_not_serve_is_not_eligible(w):
    # lab is a member through a share but outside the vault's tree; its machine's person is
    # even a direct member of the vault - the agent is the thing that is not served.
    ProjectShare.objects.create(project=w.proj, workspace=w.labs)
    services.set_member_agents(w.proj, [w.hal, w.lab])
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    labadm = _user("labadm")
    _member(w.labs, labadm, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w.lab, user=labadm, granted_by=w.jj)
    _member(w.connect, labadm, WorkspaceMembership.EDITOR)
    vault_members.add_member(w.vault, labadm, role="member", by=w.jj)
    key = _device(w.lab, labadm, "box-lab")
    assert vault_members.role_of(w.vault, labadm) == "member"
    assert not workspace_vaults.device_may_hold(w.vault, key)
    assert key not in workspace_vaults.eligible_device_keys(w.vault)


def test_leaving_the_project_ends_the_machines_standing_at_once(w):
    adm = _user("adm")
    _member(w.connect, adm, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w.eve, user=adm, granted_by=w.jj)
    vault_members.add_member(w.vault, adm, role="member", by=w.jj)
    key = _device(w.eve, adm, "box-eve")
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    assert not workspace_vaults.device_may_hold(w.vault, key)   # eve is not a member yet
    w.proj.member_agents.add(w.eve)
    assert workspace_vaults.device_may_hold(w.vault, key)
    w.proj.member_agents.remove(w.eve)
    assert not workspace_vaults.device_may_hold(w.vault, key)


def test_a_device_reads_the_project_vault_only_while_its_person_is_a_member(w):
    adm = _user("adm")
    _member(w.team, adm, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w.deep, user=adm, granted_by=w.jj)
    editors_run(w.deep)
    services.set_member_agents(w.proj, [w.hal, w.deep])
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    Vault.objects.filter(pk=w.vault.pk).update(kv=1)
    w.vault.refresh_from_db()
    VaultSecret.objects.create(vault=w.vault, name="GOOGLE_PROVISIONER_KEY", value_zk="zk1:x", kv=1)
    key = _device(w.deep, adm, "box-deep")
    VaultKeyWrap.objects.create(vault=w.vault, kv=1, kind="device", device_key=key, wrap={"alg": "ecdh-es"})
    [layer] = workspace_vaults.layers_payload(w.deep, key, None)
    assert layer["wrap"] is not None and list(layer["values"]) == ["GOOGLE_PROVISIONER_KEY"]
    w.proj.member_agents.remove(w.deep)          # leaves the project: no layer at all
    assert workspace_vaults.layers_payload(w.deep, key, None) == []


# ---- who becomes a member of the vault -----------------------------------------------------

def test_the_attached_link_follows_the_project_served_agents(w):
    person = _user("pat")
    _member(w.team, person, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=w.deep, user=person, granted_by=w.jj)
    editors_run(w.deep)
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    assert vault_members.role_of(w.vault, person) is None          # deep is not in the project yet
    w.proj.member_agents.add(w.deep)
    ms = vault_members.members(w.vault)
    assert ms[person.pk].role == "member" and ms[person.pk].sources == ["attached:deep"]
    w.proj.member_agents.remove(w.deep)
    assert vault_members.role_of(w.vault, person) is None


# ---- the attachment itself -----------------------------------------------------------------

def test_an_attachment_names_exactly_one_target(w):
    def make(**kw):
        with transaction.atomic():
            VaultAttachment.objects.create(vault=w.vault, **kw)

    with pytest.raises(IntegrityError):
        make(agent=w.hal, project=w.proj)
    with pytest.raises(IntegrityError):
        make(workspace=w.connect, project=w.proj)
    with pytest.raises(IntegrityError):
        make()
    make(project=w.proj)
    with pytest.raises(IntegrityError):
        make(project=w.proj)    # one per (vault, project)
    with pytest.raises(workspace_vaults.ZkError) as exc:
        workspace_vaults.attach(w.vault, agent=w.hal, project=w.proj)
    assert exc.value.code == "vault_bad_attachment"


def test_a_project_outside_the_vaults_tree_cannot_be_attached(w):
    with pytest.raises(workspace_vaults.ZkError) as exc:
        workspace_vaults.attach(w.vault, project=w.support)      # a dimagi project, a connect vault
    assert exc.value.status == 422 and exc.value.code == "vault_bad_attachment"
    assert not VaultAttachment.objects.exists()


def test_deleting_the_project_deletes_its_attachment_and_keeps_the_vault(w):
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    w.proj.delete()
    assert not VaultAttachment.objects.exists() and Vault.objects.filter(pk=w.vault.pk).exists()
    assert _layers(w.hal) == []


def test_moving_a_project_out_of_the_vaults_tree_drops_the_attachment(w):
    keep = workspace_vaults.create(w.dimagi, "Org", by=w.jj)
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    workspace_vaults.attach(keep, project=w.proj, by=w.jj)
    AgentProject.objects.filter(pk=w.proj.pk).update(agent=w.ada)       # an org agent leads it
    w.proj.refresh_from_db()
    services.promote_project(w.proj, "connect-work", "labs")
    assert list(VaultAttachment.objects.values_list("vault__name", flat=True)) == ["Org"]


def test_moving_a_project_within_the_tree_keeps_it(w):
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    services.promote_project(w.proj, "connect-work", "connect-team")
    assert VaultAttachment.objects.filter(vault=w.vault, project=w.proj).exists()


def test_moving_an_agent_out_of_the_tree_reports_the_vault_it_stops_getting(w):
    _join(w.proj, w.eve)
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    p = move.plan(w.eve, w.labs, by=w.jj)
    [b] = [b for b in p.stops_working if b["kind"] == "workspace_vaults_through_projects"]
    assert "connect/Shared (via connect/connect-work)" in b["detail"]
    ok = move.plan(w.eve, w.team, by=w.jj)
    assert not [b for b in ok.stops_working if b["kind"] == "workspace_vaults_through_projects"]


# ---- the API -------------------------------------------------------------------------------

def _attach(c, vault, project):
    return c.post(f"/api/vaults/{vault.pk}/attachments", data={"project": project}, content_type=J)


def test_a_vault_manager_who_edits_the_projects_workspace_attaches_it(w):
    c = _client(w.jj)
    r = _attach(c, w.vault, "connect/connect-work")
    assert r.status_code == 200, r.content
    [att] = r.json()["attachments"]
    assert att["project"] == "connect/connect-work" and att["agent"] is None and att["workspace"] is None
    assert att["served"] == ["hal"] and att["not_served"] == []
    assert _attach(c, w.vault, "connect/connect-work").status_code == 200       # again: a no-op
    assert VaultAttachment.objects.count() == 1
    assert c.delete(f"/api/vaults/{w.vault.pk}/attachments/{att['id']}").status_code == 200
    assert _layers(w.hal) == []


def test_a_project_outside_the_vaults_tree_is_a_422(w):
    r = _attach(_client(w.jj), w.vault, "dimagi/canopy-support")
    assert r.status_code == 422 and r.json()["error"] == "vault_bad_attachment"


def test_someone_who_does_not_manage_the_vault_is_refused(w):
    ed = _user("ed")
    _member(w.connect, ed, WorkspaceMembership.EDITOR)
    r = _attach(_client(ed), w.vault, "connect/connect-work")
    assert r.status_code == 403
    assert not VaultAttachment.objects.exists()


def test_a_vault_manager_who_is_not_an_editor_of_the_project_gets_a_404(w):
    # an admin of THIS vault, a mere viewer in the project's workspace: handing the vault to every
    # present and future member is the project's editors' call, not theirs.
    mgr = _user("mgr")
    _member(w.connect, mgr, WorkspaceMembership.VIEWER)
    vault_members.add_member(w.vault, mgr, role="admin", by=w.jj)
    r = _attach(_client(mgr), w.vault, "connect/connect-work")
    assert r.status_code == 404, r.content
    assert not VaultAttachment.objects.exists()
    # and the project's own lead agent being a member of it does not substitute for the role
    assert _attach(_client(w.jj), w.vault, "connect/connect-work").status_code == 200


def test_a_project_nobody_can_see_is_a_404_not_an_oracle(w):
    c = _client(w.jj)
    assert _attach(c, w.vault, "connect/nope").status_code == 404
    assert _attach(c, w.vault, "nope").status_code == 404
    other = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=w.jj)
    AgentProject.objects.create(name="Secret", workspace=other, handle="secret")
    assert _attach(c, w.vault, "elsewhere/secret").status_code == 404


def test_two_targets_are_a_422(w):
    r = _client(w.jj).post(f"/api/vaults/{w.vault.pk}/attachments",
                           data={"project": "connect/connect-work", "agent": "hal"}, content_type=J)
    assert r.status_code == 422 and r.json()["error"] == "vault_bad_attachment"
    assert not VaultAttachment.objects.exists()


def test_the_project_page_lists_its_vaults_by_name_to_those_who_belong_to_the_vaults_workspace(w):
    VaultSecret.objects.create(vault=w.vault, name="GOOGLE_PROVISIONER_KEY", value_zk="zk1:SECRET-CIPHERTEXT", kv=1)
    workspace_vaults.attach(w.vault, project=w.proj, by=w.jj)
    r = _client(w.jj).get("/api/workspace-projects/connect/connect-work/")
    assert r.status_code == 200, r.content
    [v] = r.json()["vaults"]
    assert v["name"] == "Shared" and v["served"] == ["hal"] and v["secrets"] == ["GOOGLE_PROVISIONER_KEY"]
    assert "SECRET-CIPHERTEXT" not in r.content.decode()
    # a reader of the project who belongs to a workspace BELOW the vault's does not learn of it
    reader = _user("reader")
    _member(w.team, reader, WorkspaceMembership.EDITOR)
    r = _client(reader).get("/api/workspace-projects/connect/connect-work/")
    assert r.status_code == 200 and r.json()["vaults"] == []
