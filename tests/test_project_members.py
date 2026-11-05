"""A project's people are first-class, and a vault attached to a project opens to THEM.

The case that shaped it: a token kept for a podcast project shared by two agents. Dozens of
people have full access to one of those agents, and none of them should open that token because
of it: only members of a project have access to its vault. Its agents still read it in turns.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents import project_members
from apps.agents.credentials import vault_members, workspace_vaults
from apps.agents.models import Agent, AgentAdmin, AgentProject, ProjectMember, ProjectShare, VaultMember
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import editors_run

pytestmark = pytest.mark.django_db
JWK = {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}
URL = "/api/workspace-projects/dimagi/podcast/members/"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _member(ws, user, role=WorkspaceMembership.EDITOR):
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
    w.owner, w.lead, w.editor, w.fan = _user("owner"), _user("lead"), _user("editor"), _user("fan")
    w.dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w.owner)
    w.commcare = Workspace.objects.create(slug="commcare", display_name="CommCare", created_by=w.owner, parent=w.dimagi)
    w.labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=w.owner, parent=w.dimagi)
    _member(w.dimagi, w.owner, WorkspaceMembership.OWNER)
    for u in (w.lead, w.editor, w.fan):
        _member(w.dimagi, u)
    w.eva = Agent.objects.create(slug="eva", name="Eva", workspace=w.dimagi, owner=w.owner)
    w.muse = Agent.objects.create(slug="muse", name="Muse", workspace=w.commcare, owner=w.owner)
    # `fan` has full access to Eva, and that is all.
    AgentAdmin.objects.create(agent=w.eva, user=w.fan, granted_by=w.owner)
    editors_run(w.eva)
    w.project = AgentProject.objects.create(name="Podcast", workspace=w.dimagi, handle="podcast", owner_user=w.lead)
    w.project.member_agents.add(w.eva)
    ProjectShare.objects.create(project=w.project, workspace=w.commcare)
    w.project.member_agents.add(w.muse)
    w.vault = workspace_vaults.create(w.dimagi, "Podcast", by=w.owner)
    workspace_vaults.attach(w.vault, project=w.project, by=w.owner)
    return w


def _device(agent, owner, name):
    runner = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=owner, status=Runner.ONLINE,
                                   workspace=agent.workspace, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return RunnerDeviceKey.objects.create(
        runner=runner, public_jwk=JWK, jkt=f"jkt-{name}", kind="secure-enclave", label=name,
        status=RunnerDeviceKey.ACTIVE, ecdh_public_jwk=JWK, ecdh_jkt=f"ecdh-{name}",
        ecdh_approved_at=timezone.now(), approved_by=owner)


# ---- who may open the project's vault ------------------------------------------------------

def test_full_access_to_a_member_agent_does_not_open_the_projects_vault(w):
    assert vault_members.role_of(w.vault, w.fan) is None
    assert w.fan.pk not in vault_members.member_ids(w.vault)


def test_the_agents_still_read_it_in_their_turns(w):
    assert {a.slug for a in workspace_vaults.attached_agents(w.vault)} == {"eva", "muse"}
    assert [v.pk for v in workspace_vaults.layers_for(w.eva)] == [w.vault.pk]


def test_the_projects_people_open_it_and_say_why(w):
    project_members.add_member(w.project, w.editor, role="member", by=w.owner)
    ms = vault_members.members(w.vault)
    assert ms[w.editor.pk].role == "member" and ms[w.editor.pk].sources == ["project:dimagi/podcast"]
    assert ms[w.lead.pk].role == "admin"  # the lead runs the project's vault, never owns it
    assert ms[w.owner.pk].role == "owner"  # through the workspace's own link
    project_members.remove_member(w.project, w.editor.pk)
    assert vault_members.role_of(w.vault, w.editor) is None


def test_an_agent_attached_by_name_still_brings_its_people(w):
    workspace_vaults.attach(w.vault, agent=w.eva, by=w.owner)
    assert vault_members.members(w.vault)[w.fan.pk].sources == ["attached:eva"]


def test_a_box_owner_who_is_not_on_the_project_gets_no_copy_until_they_join(w):
    key = _device(w.eva, w.fan, "fan-box")
    assert not workspace_vaults.device_may_hold(w.vault, key)
    assert key not in workspace_vaults.eligible_device_keys(w.vault)
    assert workspace_vaults.project_vault_box_owners(w.project) == {"fan@dimagi.com": ["eva"]}

    project_members.add_member(w.project, w.fan, role="member", by=w.owner)
    assert workspace_vaults.device_may_hold(w.vault, key)
    assert workspace_vaults.project_vault_box_owners(w.project) == {}


def test_someone_on_the_project_in_a_workspace_below_the_vaults_opens_it(w):
    amie = _user("amie")
    _member(w.commcare, amie)
    project_members.add_member(w.project, amie, role="member", by=w.owner)
    assert vault_members.role_of(w.vault, amie) == "member"
    assert _client(amie).get(f"/api/vaults/{w.vault.pk}").status_code == 200


def test_a_project_person_outside_the_vaults_tree_does_not_open_it(w):
    proj = AgentProject.objects.create(name="Div", workspace=w.commcare, handle="div")
    ProjectShare.objects.create(project=proj, workspace=w.labs)
    vault = workspace_vaults.create(w.commcare, "Div", by=w.owner)
    workspace_vaults.attach(vault, project=proj, by=w.owner)
    beside = _user("beside")
    _member(w.labs, beside)
    project_members.add_member(proj, beside, role="member", by=w.owner)
    assert project_members.is_member(proj, beside)
    assert vault_members.role_of(vault, beside) is None


def test_leaving_the_workspace_ends_it(w):
    project_members.add_member(w.project, w.editor, role="member", by=w.owner)
    WorkspaceMembership.objects.filter(user=w.editor).delete()
    assert not project_members.is_member(w.project, w.editor)
    assert vault_members.role_of(w.vault, w.editor) is None


def test_a_vault_with_people_added_by_name_keeps_them(w):
    VaultMember.objects.create(vault=w.vault, user=w.editor, role="member", added_by=w.owner)
    assert vault_members.members(w.vault)[w.editor.pk].sources == ["direct"]


# ---- who may change the list ----------------------------------------------------------------

def test_the_lead_an_admin_and_a_workspace_owner_may_change_it(w):
    admin = _user("admin")
    _member(w.dimagi, admin)
    project_members.add_member(w.project, admin, role="admin", by=w.owner)
    for user in (w.lead, admin, w.owner):
        assert project_members.may_manage(w.project, user), user


def test_an_editor_and_a_plain_member_may_not(w):
    project_members.add_member(w.project, w.fan, role="member", by=w.owner)
    for user in (w.editor, w.fan):
        assert not project_members.may_manage(w.project, user)
    r = _client(w.editor).post(URL, {"email": "editor@dimagi.com"}, content_type="application/json")
    assert r.status_code == 403
    assert not ProjectMember.objects.filter(project=w.project, user=w.editor).exists()


def test_an_agents_login_never_may_and_never_is_a_member(w):
    login = _user("eva-login")
    _member(w.dimagi, login, WorkspaceMembership.OWNER)
    w.eva.user = login
    w.eva.save(update_fields=["user"])
    assert not project_members.may_manage(w.project, login)
    with pytest.raises(project_members.MembershipError) as exc:
        project_members.add_member(w.project, login, role="member", by=w.owner)
    assert exc.value.code == "project_not_a_person"


def test_someone_who_cannot_read_the_project_cannot_be_added(w):
    stranger = _user("stranger")
    _member(w.labs, stranger)
    with pytest.raises(project_members.MembershipError) as exc:
        project_members.add_member(w.project, stranger, role="member", by=w.owner)
    assert exc.value.code == "project_not_in_reach"


def test_a_personal_project_has_only_its_lead(w):
    personal = AgentProject.objects.create(name="Mine", workspace=w.dimagi, handle="mine", owner_user=w.lead,
                                           visibility=AgentProject.PERSONAL)
    with pytest.raises(project_members.MembershipError) as exc:
        project_members.add_member(personal, w.editor, role="member", by=w.lead)
    assert exc.value.code == "project_is_personal"
    ProjectMember.objects.create(project=personal, user=w.editor)  # a row made while it was not personal
    assert set(project_members.members(personal)) == {w.lead.pk}


def test_a_project_with_people_on_it_cannot_go_personal(w):
    from apps.agents.services.projects import _personal_blockers  # noqa: PLC0415

    project_members.add_member(w.project, w.editor, role="member", by=w.owner)
    assert "people have been added to it" in _personal_blockers(w.project)


# ---- what being on a project does, and the routes ------------------------------------------------

def test_a_member_may_change_the_project_and_finds_it_among_theirs(w):
    viewer = _user("viewer")
    _member(w.dimagi, viewer, WorkspaceMembership.VIEWER)
    patch = {"outcome": "Better episodes"}
    assert _client(viewer).patch("/api/workspace-projects/dimagi/podcast/", patch,
                                 content_type="application/json").status_code == 403
    project_members.add_member(w.project, viewer, role="member", by=w.owner)
    c = _client(viewer)
    assert c.patch("/api/workspace-projects/dimagi/podcast/", patch, content_type="application/json").status_code == 200
    assert "podcast" in {p["handle"] for p in c.get("/api/me/projects/").json()}


def test_the_routes_add_list_and_remove(w):
    c = _client(w.lead)
    r = c.post(URL, {"email": "EDITOR@dimagi.com", "role": "admin"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["may_manage"] is True
    assert [(m["email"], m["role"], m["sources"]) for m in body["members"]] == [
        ("lead@dimagi.com", "owner", ["lead"]), ("editor@dimagi.com", "admin", ["direct"])]
    assert c.get(URL).json()["members"] == body["members"]
    r = c.delete(f"{URL}{w.editor.pk}/")
    assert [m["email"] for m in r.json()["members"]] == ["lead@dimagi.com"]


def test_an_unknown_address_is_a_422(w):
    r = _client(w.lead).post(URL, {"email": "nobody@dimagi.com"}, content_type="application/json")
    assert r.status_code == 422 and r.json()["error"] == "project_no_such_user"


def test_only_a_manager_is_told_whose_machines_are_missing(w):
    _device(w.eva, w.fan, "fan-box")
    assert _client(w.lead).get(URL).json()["box_owners_missing"] == [{"email": "fan@dimagi.com", "agents": ["eva"]}]
    assert _client(w.editor).get(URL).json()["box_owners_missing"] == []


def test_a_person_in_two_organisations_reaches_a_project_by_address_unpinned(w):
    """The URL names the workspace, so this is not the read `workspace-required` refuses."""
    other = Workspace.objects.create(slug="family", display_name="Family", created_by=w.lead)
    _member(other, w.lead)
    c = _client(w.lead)
    assert c.get("/api/workspace-projects/dimagi/podcast/").status_code == 200
    assert c.get(URL).status_code == 200
    assert c.patch("/api/workspace-projects/dimagi/podcast/", {"outcome": "x"},
                   content_type="application/json").status_code == 200
