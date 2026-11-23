"""A project's files: a git repository canopy hosts for the project, drafted by whoever may
change the project and published only by a person its vaults already open to.

The case that shaped it: a shared Intacct project whose skill signs in to Intacct with a
script, using the login in the project's vault. The script is tooling many projects'
agents use, so it lives with the project, not in one agent; and since it runs with the
vault's secrets, an agent may propose a change to it but never publish one.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents import definition_git as git
from apps.agents import project_files as pf
from apps.agents.models import Agent, AgentProject, ProjectFilesRevision, ProjectGitRef, ProjectMember
from apps.harness import caller_tokens
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.canopy_sessions.models import Session
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.token_helpers import bearer_headers

pytestmark = pytest.mark.django_db
BASE = "/api/workspace-projects/dimagi/intacct/files"
SKILL = "---\nname: intacct-login\ndescription: Sign in to Intacct read-only\n---\nRun login.py.\n"
SCRIPT = "import os\nprint('signing in as', os.environ['INTACCT_USER'])\n"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _client(user):
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _pat(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return bearer_headers(raw)


@pytest.fixture
def w():
    class W:
        pass

    w = W()
    w.owner, w.lead, w.editor, w.viewer = _user("owner"), _user("lead"), _user("editor"), _user("viewer")
    w.hal_login = _user("hal-login")
    w.ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w.owner)
    WorkspaceMembership.objects.create(workspace=w.ws, user=w.owner, role=WorkspaceMembership.OWNER)
    for u in (w.lead, w.editor):
        WorkspaceMembership.objects.create(workspace=w.ws, user=u, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=w.ws, user=w.viewer, role=WorkspaceMembership.VIEWER)
    WorkspaceMembership.objects.create(workspace=w.ws, user=w.hal_login, role=WorkspaceMembership.EDITOR)
    w.hal = Agent.objects.create(slug="hal", name="Hal", workspace=w.ws, owner=w.owner, user=w.hal_login)
    w.project = AgentProject.objects.create(name="Intacct", workspace=w.ws, handle="intacct", owner_user=w.lead)
    w.project.member_agents.add(w.hal)
    return w


def _draft(client, *, base=0, files=None, headers=None, message="first"):
    changes = [{"path": p, "content": c} for p, c in (files or {"skills/intacct-login/SKILL.md": SKILL,
                                                                 "skills/intacct-login/login.py": SCRIPT}).items()]
    return client.put(f"{BASE}/draft", data={"base": base, "changes": changes, "message": message},
                      content_type="application/json", **(headers or {}))


# -- the file policy ----------------------------------------------------------------

def test_the_policy_takes_skills_with_their_scripts_and_docs():
    assert pf.editable("README.md")
    assert pf.editable("docs/how/intacct.md")
    assert pf.editable("skills/intacct-login/SKILL.md")
    assert pf.editable("skills/intacct-login/login.py")
    assert pf.editable("skills/intacct-login/lib/export.sql")
    assert not pf.editable("hooks/pre.sh")
    assert not pf.editable(".claude-plugin/plugin.json")
    assert not pf.editable("skills/Bad Name/SKILL.md")
    assert not pf.editable("skills/x/blob.bin")
    assert not pf.editable("bin/run")


def test_the_policy_refuses_an_executable_a_secret_and_a_stray_path():
    problems = pf.check_tree({
        "skills/x/run.sh": (b"echo hi\n", True),
        "skills/x/conf.py": (b"TOKEN = 'ghp_" + b"a" * 30 + b"'\n", False),
        "bin/tool": (b"x", False),
    })
    assert {(p.path, p.code) for p in problems} == {
        ("skills/x/run.sh", "executable"), ("skills/x/conf.py", "secret"), ("bin/tool", "not_editable")}


# -- drafting and publishing ----------------------------------------------------------

def test_a_member_agent_drafts_and_the_lead_publishes(w):
    hal = Client()
    resp = _draft(hal, headers=_pat(w.hal_login))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "draft" and body["author_agent"] == "hal"
    assert {c["path"] for c in body["changes"]} == {"skills/intacct-login/SKILL.md", "skills/intacct-login/login.py"}

    # The agent cannot publish what it wrote, however it signs in.
    refused = hal.post(f"{BASE}/draft/{body['number']}/publish", data={}, content_type="application/json",
                       **_pat(w.hal_login))
    assert refused.status_code == 403 and refused.json()["reason"] == "agent_login"

    published = _client(w.lead).post(f"{BASE}/draft/{body['number']}/publish",
                                     data={"expect_root": body["root_sha256"]}, content_type="application/json")
    assert published.status_code == 200, published.content
    assert published.json()["status"] == "published" and published.json()["published_by"] == "lead@dimagi.com"

    listing = _client(w.viewer).get(f"{BASE}/").json()
    assert listing["live"]["number"] == body["number"] and listing["draft"] is None
    assert listing["skills"] == [{"name": "intacct-login", "description": "Sign in to Intacct read-only",
                                  "files": ["skills/intacct-login/login.py"]}]
    assert listing["may_publish"] is False and listing["may_draft"] is False
    got = _client(w.viewer).get(f"{BASE}/content", {"path": "skills/intacct-login/login.py"})
    assert got.status_code == 200 and got.json()["content"] == SCRIPT


def test_an_editor_drafts_but_does_not_publish(w):
    editor = _client(w.editor)
    rev = _draft(editor).json()
    resp = editor.post(f"{BASE}/draft/{rev['number']}/publish", data={}, content_type="application/json")
    assert resp.status_code == 403 and resp.json()["reason"] == "not_project_admin"


def test_a_project_admin_and_a_workspace_owner_publish(w):
    ProjectMember.objects.create(project=w.project, user=w.editor, role="admin", added_by=w.lead)
    rev = _draft(_client(w.lead)).json()
    assert _client(w.editor).post(f"{BASE}/draft/{rev['number']}/publish", data={},
                                  content_type="application/json").status_code == 200
    second = _draft(_client(w.lead), base=rev["number"], files={"README.md": "Intacct tooling\n"}).json()
    assert _client(w.owner).post(f"{BASE}/draft/{second['number']}/publish", data={},
                                 content_type="application/json").status_code == 200


def test_a_conversations_caller_token_never_publishes(w):
    rev = _draft(_client(w.lead)).json()
    session = Session.objects.create(workspace=w.ws, project="canopy-web", created_by=w.lead, title="chat")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="publish",
                               idempotency_key="k1", initiator_kind=who.USER, initiator_user=w.lead,
                               initiator_assurance=who.SESSION)
    raw = caller_tokens.mint(turn, scoped=True)
    resp = Client().post(f"{BASE}/draft/{rev['number']}/publish", data={}, content_type="application/json",
                         **bearer_headers(raw))
    assert resp.status_code == 403


def test_the_lead_publishes_with_a_pat(w):
    rev = _draft(_client(w.lead)).json()
    resp = Client().post(f"{BASE}/draft/{rev['number']}/publish", data={}, content_type="application/json",
                         **_pat(w.lead))
    assert resp.status_code == 200, resp.content


def test_a_viewer_neither_drafts_nor_sees_a_draft(w):
    _draft(_client(w.lead))
    viewer = _client(w.viewer)
    assert _draft(viewer).status_code == 403
    assert viewer.get(f"{BASE}/").json()["draft"] is None
    assert viewer.get(f"{BASE}/content", {"path": "skills/intacct-login/login.py", "revision": 1}).status_code == 404


def test_a_draft_on_files_no_longer_live_is_refused(w):
    lead = _client(w.lead)
    first = _draft(lead).json()
    lead.post(f"{BASE}/draft/{first['number']}/publish", data={}, content_type="application/json")
    stale = _draft(lead, base=0, files={"README.md": "x\n"})
    assert stale.status_code == 409 and stale.json()["error"] == "stale_base"


def test_a_new_draft_replaces_the_old_and_a_refused_file_refuses_the_draft(w):
    lead = _client(w.lead)
    one = _draft(lead).json()
    two = _draft(lead, files={"README.md": "x\n"}).json()
    assert ProjectFilesRevision.objects.get(project=w.project, number=one["number"]).status == "discarded"
    assert two["status"] == "draft"
    bad = _draft(lead, files={"skills/x/creds.py": "KEY = 'AKIA" + "A" * 16 + "'\n"})
    assert bad.status_code == 422 and bad.json()["problems"][0]["code"] == "secret"
    # The refused draft changed nothing: the earlier draft is still the draft.
    assert pf.draft(w.project).number == two["number"]


def test_a_rollback_is_a_new_revision_and_history_is_kept(w):
    lead = _client(w.lead)
    one = _draft(lead).json()
    lead.post(f"{BASE}/draft/{one['number']}/publish", data={}, content_type="application/json")
    two = _draft(lead, base=one["number"], files={"skills/intacct-login/login.py": None}).json()
    lead.post(f"{BASE}/draft/{two['number']}/publish", data={}, content_type="application/json")
    assert "skills/intacct-login/login.py" not in {f["path"] for f in lead.get(f"{BASE}/").json()["live"]["files"]}
    back = lead.post(f"{BASE}/revisions/{one['number']}/rollback", data={}, content_type="application/json").json()
    assert back["status"] == "published" and back["number"] == two["number"] + 1
    assert [r["status"] for r in lead.get(f"{BASE}/revisions/").json()] == ["published", "superseded", "superseded"]


def test_the_repository_is_real_git(w):
    lead = _client(w.lead)
    rev = _draft(lead).json()
    lead.post(f"{BASE}/draft/{rev['number']}/publish", data={}, content_type="application/json")
    refs = git.refs_of(w.project.pk, model=ProjectGitRef, owner=pf.OWNER)
    assert refs[git.MAIN] == rev["commit"] == refs[git.tag_ref(rev["number"])]
    assert git.DRAFT not in refs
    files = git.files_at(pf.object_store(w.project), rev["commit"])
    assert files["skills/intacct-login/login.py"] == (SCRIPT.encode(), False)


def test_only_its_author_or_a_publisher_discards_a_draft(w):
    rev = _draft(_client(w.editor)).json()
    other = _user("other")
    WorkspaceMembership.objects.create(workspace=w.ws, user=other, role=WorkspaceMembership.EDITOR)
    assert _client(other).delete(f"{BASE}/draft/{rev['number']}").status_code == 403
    assert _client(w.lead).delete(f"{BASE}/draft/{rev['number']}").json()["status"] == "discarded"
