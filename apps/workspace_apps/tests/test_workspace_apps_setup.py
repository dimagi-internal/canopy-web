"""Hiring 2.2 through the framework: an agent asked to work on ONE draft (and no
more), the first-run setup list that ticks itself, and the store naming each email."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services
from apps.workspace_apps.models import AppInstallation, AppRoleGrant
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _client, _job, _user

pytestmark = pytest.mark.django_db
H = f"{BASE}/hiring"
OP = {"op": "add_option", "collection": "application", "field": "stage", "option": "Phone screen",
      "before": "Interview", "intent": "a phone screen before interviews"}


def _agent(world, slug="hal", home="dimagi", member_role=WorkspaceMembership.VIEWER):
    """An agent whose own login is (optionally) a member of the app's workspace."""
    login = _user(f"{slug}-login")
    if home != "dimagi" and not Workspace.objects.filter(slug=home).exists():
        Workspace.objects.create(slug=home, display_name=home, created_by=world["admin"])
        WorkspaceMembership.objects.create(user=world["admin"], workspace_id=home, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace_id=home, owner=world["admin"], user=login)
    if member_role:
        WorkspaceMembership.objects.create(user=login, workspace=world["ws"], role=member_role)
    return agent, _client(login)


def _maintainer(world, hiring):
    u = _user("molly")
    WorkspaceMembership.objects.create(user=u, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    AppRoleGrant.objects.create(installation=hiring, user=u, role="maintainer", granted_by=world["admin"])
    return _client(u)


def test_an_agent_asked_onto_a_draft_may_change_and_test_it_but_never_publish(world, hiring):
    molly = _maintainer(world, hiring)
    agent, bot = _agent(world, home="elsewhere")  # lives in another workspace; its login is a viewer here
    # Molly sees agents only where she is a member: none until she is in the agent's workspace too.
    assert "hal" not in {h["slug"] for h in molly.get(f"{H}/helpers").json()}
    WorkspaceMembership.objects.create(user=User.objects.get(username="molly"), workspace_id="elsewhere",
                                       role=WorkspaceMembership.VIEWER)

    # Before anyone asks it, the agent cannot touch the app's drafts.
    assert molly.post(f"{H}/draft", {"note": "x"}, content_type=J).status_code == 201
    assert bot.post(f"{H}/draft/ops", OP, content_type=J).status_code == 403
    assert bot.get(f"{H}/guide").status_code == 403

    listed = {h["slug"]: h for h in molly.get(f"{H}/helpers").json()}
    assert listed["hal"]["reachable"] and not listed["hal"]["helping"] and listed["hal"]["home"] == "elsewhere"
    r = molly.post(f"{H}/draft/helpers", {"agent": "hal"}, content_type=J)
    assert r.status_code == 200, r.content
    assert {h["slug"]: h["helping"] for h in r.json()}["hal"] is True

    assert bot.get(f"{H}/guide").status_code == 200
    assert bot.post(f"{H}/draft/ops", OP, content_type=J).status_code == 201
    draft = bot.get(f"{H}/draft").json()
    assert bot.get(f"{H}?draft={draft['id']}").status_code == 200  # the test copy
    assert bot.post(f"{H}/draft/checks", content_type=J).status_code == 200
    for path, method in ((f"{H}/draft/publish", "post"), (f"{H}/draft", "delete"), (f"{H}/rollback", "post")):
        assert getattr(bot, method)(path, {"number": 1}, content_type=J).status_code == 403, path
    assert bot.post(f"{H}/draft", {"note": "mine"}, content_type=J).status_code == 403
    assert bot.get(f"{H}/helpers").status_code == 403

    # Publishing closes the draft, and the agent's access with it.
    assert molly.post(f"{H}/draft/publish", content_type=J).status_code == 200
    assert molly.post(f"{H}/draft", {"note": "next"}, content_type=J).status_code == 201
    assert bot.post(f"{H}/draft/ops", OP, content_type=J).status_code == 403


def test_only_an_agent_that_is_a_member_can_be_asked_and_never_a_person(world, hiring):
    molly = _maintainer(world, hiring)
    _agent(world, slug="ada", member_role=None)  # its login is no member of the app's workspace
    assert molly.post(f"{H}/draft", {"note": "x"}, content_type=J).status_code == 201
    listed = {h["slug"]: h for h in molly.get(f"{H}/helpers").json()}
    assert listed["ada"]["reachable"] is False
    r = molly.post(f"{H}/draft/helpers", {"agent": "ada"}, content_type=J)
    assert r.status_code == 422 and "ada-login@dimagi.com" in r.json()["detail"]
    assert molly.post(f"{H}/draft/helpers", {"agent": "nobody"}, content_type=J).status_code == 404
    # A plain member (not a maintainer) cannot hand out draft access.
    assert _client(world["viewer"]).post(f"{H}/draft/helpers", {"agent": "ada"}, content_type=J).status_code == 403
    # An agent in a workspace the maintainer cannot see is not offered, and cannot be named.
    other_login = _user("spy-login")
    Agent.objects.create(slug="spy", name="Spy", workspace_id="other", owner=world["outsider"], user=other_login)
    WorkspaceMembership.objects.create(user=other_login, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    assert "spy" not in {h["slug"] for h in molly.get(f"{H}/helpers").json()}
    assert molly.post(f"{H}/draft/helpers", {"agent": "spy"}, content_type=J).status_code == 404


def test_the_setup_list_ticks_itself_off_from_the_app(world, hiring):
    admin = _client(world["admin"])
    steps = {s["id"]: s for s in admin.get(f"{H}/setup").json()["steps"]}
    assert set(steps) >= {"recruiters", "compliance", "wording", "first_job", "interview_plan", "careers", "import"}
    assert steps["recruiters"]["done"] is True  # the fixture named a recruiter
    assert not steps["first_job"]["done"] and not steps["careers"]["done"] and not steps["wording"]["done"]

    _job(_client(world["editor"]))
    AppInstallation.objects.filter(pk=hiring.pk).update(public=True)
    r = admin.post(f"{H}/setup", {"step": "wording", "done": True}, content_type=J)
    assert r.status_code == 200
    steps = {s["id"]: s for s in r.json()["steps"]}
    assert steps["first_job"]["done"] and steps["careers"]["done"] and steps["wording"]["done"]
    # A step the app checks for itself cannot be ticked by hand.
    assert admin.post(f"{H}/setup", {"step": "first_job"}, content_type=J).status_code == 422
    assert admin.post(f"{H}/setup", {"step": "wording", "done": False}, content_type=J).json()["steps"][2]["done"] is False
    assert admin.post(f"{H}/setup", {"hidden": True}, content_type=J).json()["hidden"] is True
    # Only someone who maintains the app sees it.
    assert _client(world["viewer"]).get(f"{H}/setup").status_code == 403


def test_a_setup_step_must_name_what_the_app_can_check():
    raw = services._read_bundled("hiring")
    raw["setup"] = [{"id": "x", "title": "X", "check": {"setting": "no_such_setting"}}]
    with pytest.raises(ValueError, match="setup"):
        mf.validate(raw)
    raw["setup"] = [{"id": "x", "title": "X", "check": {"manual": True}, "go": "nowhere"}]
    with pytest.raises(ValueError, match="go"):
        mf.validate(raw)


def test_the_store_names_each_email_and_who_gets_it(world):
    r = _client(world["admin"]).get("/api/workspace-app-store/dimagi/detail?template=hiring")
    assert r.status_code == 200, r.content
    emails = {e["name"]: e for e in r.json()["emails"]}
    assert emails["Not moving forward"]["to"] == "to the applicant" and emails["Not moving forward"]["delayable"]
    assert emails["Interview invitation"]["to"] == "to the candidate and the interviewers"
    assert "Apply" not in emails  # an action's button label is not an email's name


def test_today_comes_first_and_every_member_reaches_it(world, hiring):
    pages = [p["slug"] for p in _client(world["viewer"]).get(H).json()["pages"]]
    assert pages[0] == "today"
    assert hiring.manifest["version"] == "2.2.0"
