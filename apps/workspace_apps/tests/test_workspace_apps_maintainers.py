"""App maintainers: an admin lets a named person or agent publish new versions of ONE app —
`content` (nothing about who can see or do what changes) or `permissions` (that too, approved
ahead of time) — and a maintainer publishes a whole manifest a repo owns as a new version."""
from __future__ import annotations

import copy

import pytest

from apps.agents.models import Agent
from apps.events.models import SettingChange
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services
from apps.workspace_apps.models import AppInstallation, AppRoleGrant, AppTemplateVersion
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _client, _user

pytestmark = pytest.mark.django_db
S = f"{BASE}/sales"

APP = {
    "key": "sales", "name": "Sales", "version": "1",
    "roles": {"seller": {"capability": "content.write"}},
    "collections": {"note": {"fields": {"body": {"type": "text"}}}},
    "actions": {"add_note": {"collection": "note", "op": "create", "role": "member", "input": ["body"]}},
    "pages": [
        {"slug": "gmail-panel", "title": "Gmail", "code": "function View(){return <Card>hi</Card>}"},
    ],
}
SF = [{"provider": "salesforce", "as": "viewer", "reads": [{"method": "GET", "path": "query"}], "writes": []}]
BROWSER = [{"page": "gmail-panel", "matches": ["https://mail.google.com/*"], "adapter": "gmail",
            "capabilities": ["read_open_email"], "events": ["email_opened"]}]


def _version(version, **changes):
    m = copy.deepcopy(APP)
    m["version"] = version
    m.update(changes)
    return m


@pytest.fixture()
def sales(world):
    r = _client(world["admin"]).post(f"{BASE}/", {"manifest": APP}, content_type=J)
    assert r.status_code == 201, r.content
    return AppInstallation.objects.get(workspace_id="dimagi", key="sales")


def _member(world, name, role=WorkspaceMembership.VIEWER):
    u = _user(name)
    WorkspaceMembership.objects.create(user=u, workspace=world["ws"], role=role)
    return u


def _agent(world, slug="eva", member=True):
    login = _user(f"{slug}-login")
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace_id="dimagi", owner=world["admin"], user=login)
    if member:
        WorkspaceMembership.objects.create(user=login, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    return agent, _client(login)


def _grant(world, level="content", **who):
    r = _client(world["admin"]).post(f"{S}/maintainers", {"level": level, **who}, content_type=J)
    assert r.status_code == 201, r.content
    return r.json()


def _release(client, m, note=""):
    return client.post(f"{S}/versions", {"manifest": m, "note": note}, content_type=J)


# --- granting and revoking -------------------------------------------------------------------


def test_an_admin_grants_a_person_and_an_agent_changes_a_level_and_revokes(world, sales):
    molly = _member(world, "molly")
    _agent(world)
    admin = _client(world["admin"])

    person = _grant(world, email=molly.email)
    assert (person["level"], person["agent"], person["granted_by"]) == ("content", None, world["admin"].email)
    bot = _grant(world, agent="eva", level="permissions")
    assert (bot["agent"], bot["name"], bot["level"]) == ("eva", "Eva", "permissions")

    listed = admin.get(f"{S}/maintainers").json()
    assert [(m["email"], m["level"]) for m in listed] == [(molly.email, "content"), ("eva-login@dimagi.com", "permissions")]

    # Naming her again changes her level, never a second grant.
    assert _grant(world, email=molly.email, level="permissions")["id"] == person["id"]
    assert AppRoleGrant.objects.get(pk=person["id"]).level == "permissions"

    assert admin.delete(f"{S}/maintainers/{bot['id']}").status_code == 204
    assert admin.delete(f"{S}/maintainers/{bot['id']}").status_code == 404
    assert [m["agent"] for m in admin.get(f"{S}/maintainers").json()] == [None]

    # Every grant, level change and revocation is a setting change, with who made it.
    ops = list(SettingChange.objects.filter(workspace_id="dimagi", operation__contains="maintainer")
               .values_list("operation", "actor_email"))
    assert [o for o, _ in ops].count("set_workspace_app_maintainer") == 3
    assert ("remove_workspace_app_maintainer", world["admin"].email) in ops


def test_only_an_admin_names_maintainers_and_only_members_can_be_one(world, sales):
    molly = _member(world, "molly")
    body = {"email": molly.email, "level": "permissions"}
    for who in (world["editor"], world["viewer"]):
        assert _client(who).post(f"{S}/maintainers", body, content_type=J).status_code == 403
        assert _client(who).get(f"{S}/maintainers").status_code == 403
    assert _client(world["outsider"]).post(f"{S}/maintainers", body, content_type=J).status_code == 404
    # A maintainer cannot raise their own level.
    _grant(world, email=molly.email)
    assert _client(molly).post(f"{S}/maintainers", body, content_type=J).status_code == 403

    admin = _client(world["admin"])
    assert admin.post(f"{S}/maintainers", {"email": world["outsider"].email}, content_type=J).status_code == 422
    _agent(world, slug="ada", member=False)
    r = admin.post(f"{S}/maintainers", {"agent": "ada"}, content_type=J)
    assert r.status_code == 422 and "not a member" in r.json()["detail"]
    assert admin.post(f"{S}/maintainers", {"agent": "nobody"}, content_type=J).status_code == 404
    assert admin.post(f"{S}/maintainers", {"agent": "eva", "email": molly.email}, content_type=J).status_code == 422


# --- publishing a new version ----------------------------------------------------------------


def test_a_content_maintainer_publishes_a_version_that_widens_nothing(world, sales):
    molly = _member(world, "molly")
    _grant(world, email=molly.email)
    m = _version("2")
    m["pages"][0]["code"] = "function View(){return <Card>hello</Card>}"
    m["collections"]["note"]["fields"]["topic"] = {"type": "text", "label": "Topic"}
    r = _release(_client(molly), m, note="topic field")
    assert r.status_code == 201, r.content
    v = r.json()
    assert (v["number"], v["origin"], v["template_version"], v["reach_changes"], v["published_under"]) == (
        2, "release", "2", [], "maintainer:content")
    sales.refresh_from_db()
    assert "topic" in sales.manifest["collections"]["note"]["fields"] and sales.live.published_by == molly


def test_a_content_maintainer_is_refused_a_version_that_widens_and_nothing_is_kept(world, sales):
    molly = _member(world, "molly")
    _grant(world, email=molly.email)
    r = _release(_client(molly), _version("2", connections=SF, browser=BROWSER))
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "Salesforce" in detail and "browser tabs" in detail and "permissions" in detail
    assert not AppTemplateVersion.objects.filter(template=sales.template, version="2").exists()
    sales.refresh_from_db()
    assert sales.live.number == 1 and not sales.manifest["connections"]


def test_a_permissions_maintainer_agent_changes_connections_and_browser_after_install(world, sales):
    _, eva = _agent(world)
    _grant(world, agent="eva", level="permissions")
    r = _release(eva, _version("2", connections=SF, browser=BROWSER))
    assert r.status_code == 201, r.content
    v = r.json()
    assert v["published_under"] == "maintainer:permissions"
    assert any("Salesforce" in c for c in v["reach_changes"]) and any("browser" in c for c in v["reach_changes"])
    sales.refresh_from_db()
    assert sales.manifest["connections"][0]["provider"] == "salesforce"
    assert sales.manifest["browser"][0]["adapter"] == "gmail"
    # The record is on the version list for whoever maintains it.
    row = _client(world["admin"]).get(f"{S}/versions").json()[0]
    assert row["reach_changes"] == v["reach_changes"] and row["published_by"] == "eva-login@dimagi.com"


def test_a_non_maintainer_or_a_revoked_one_cannot_publish(world, sales):
    m = _version("2")
    for who in (world["editor"], world["viewer"]):
        assert _release(_client(who), m).status_code == 403
    assert _release(_client(world["outsider"]), m).status_code == 404
    _, eva = _agent(world)
    g = _grant(world, agent="eva", level="permissions")
    assert _client(world["admin"]).delete(f"{S}/maintainers/{g['id']}").status_code == 204
    assert _release(eva, m).status_code == 403


def test_the_new_version_is_validated_like_an_install(world, sales):
    admin = _client(world["admin"])
    bad_browser = [{**BROWSER[0], "matches": ["https://evil.example.com/*"]}]
    r = _release(admin, _version("2", browser=bad_browser))
    assert r.status_code == 422 and "browser" in r.json()["detail"]
    r = _release(admin, _version("2", connections=[{"provider": "salesforce", "as": "nobody"}]))
    assert r.status_code == 422 and "connections" in r.json()["detail"]
    assert _release(admin, {**_version("2"), "key": "other"}).status_code == 422
    assert _release(admin, _version("1")).status_code == 409  # that version string is taken
    # An admin may widen; the version without a version string gets the next free one.
    r = _release(admin, {**_version("", connections=SF)})
    assert r.status_code == 201 and r.json()["published_under"] == "admin" and r.json()["template_version"] == "2"


def test_an_open_draft_and_canopys_own_templates_refuse_a_new_version(world, sales, hiring):
    admin = _client(world["admin"])
    assert admin.post(f"{S}/draft", {"note": "x"}, content_type=J).status_code == 201
    r = _release(admin, _version("2"))
    assert r.status_code == 409 and "draft" in r.json()["detail"]
    hm = copy.deepcopy(services.bundled_manifest("hiring"))
    hm["version"] = "99.0.0"
    r = admin.post(f"{BASE}/hiring/versions", {"manifest": hm}, content_type=J)
    assert r.status_code == 409 and "canopy's own template" in r.json()["detail"]


def test_the_workspaces_own_changes_ride_onto_the_new_version(world, sales):
    admin = _client(world["admin"])
    assert admin.post(f"{S}/draft", {}, content_type=J).status_code == 201
    op = {"op": "add_field", "collection": "note", "key": "x_mood", "field": {"type": "text", "label": "Mood"},
          "intent": "a mood"}
    assert admin.post(f"{S}/draft/ops", op, content_type=J).status_code == 201
    assert admin.post(f"{S}/draft/publish", content_type=J).status_code == 200
    r = _release(admin, _version("2"))
    assert r.status_code == 201, r.content
    sales.refresh_from_db()
    assert "x_mood" in sales.manifest["collections"]["note"]["fields"]


# --- rollback and the draft flow -------------------------------------------------------------


def test_rollback_after_a_maintainer_version_and_going_back_is_judged_too(world, sales):
    molly = _member(world, "molly")
    _grant(world, email=molly.email)
    _, eva = _agent(world)
    _grant(world, agent="eva", level="permissions")
    assert _release(eva, _version("2", connections=SF)).status_code == 201  # v2: adds Salesforce reads
    assert _release(_client(molly), _version("3", connections=SF, name="Sales CRM")).status_code == 201

    # Going back to v1 would take the Salesforce calls away: that is a permissions change too.
    r = _client(molly).post(f"{S}/rollback", {"number": 1}, content_type=J)
    assert r.status_code == 403 and "Salesforce" in r.json()["detail"]
    # Back to v2 changes only the name: a content maintainer may.
    assert _client(molly).post(f"{S}/rollback", {"number": 2}, content_type=J).status_code == 200
    r = eva.post(f"{S}/rollback", {"number": 1}, content_type=J)
    assert r.status_code == 200, r.content
    sales.refresh_from_db()
    assert sales.manifest["connections"] == [] and sales.live.origin == "rollback"
    assert sales.live.published_under == "maintainer:permissions" and sales.live.reach_changes


def test_a_permissions_maintainer_publishes_a_draft_a_content_one_cannot(world, hiring):
    m = copy.deepcopy(services.bundled_manifest("hiring"))
    m["version"], m["changelog"] = "9.9.0", "interviewers see the reports"
    next(p for p in m["pages"] if p["slug"] == "reports")["role"] = "interviewer"
    hiring.template.versions.create(version="9.9.0", manifest=mf.validate(m), changelog=m["changelog"])
    molly = _member(world, "molly")
    AppRoleGrant.objects.create(installation=hiring, user=molly, role="maintainer", granted_by=world["admin"])
    c = _client(molly)
    assert c.post(f"{BASE}/hiring/draft", {"upgrade": True}, content_type=J).status_code == 201
    r = c.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 403 and "permissions" in r.json()["detail"]

    g = AppRoleGrant.objects.get(installation=hiring, user=molly, role="maintainer")
    r = _client(world["admin"]).post(f"{BASE}/hiring/maintainers", {"email": molly.email, "level": "permissions"},
                                     content_type=J)
    assert r.status_code == 201 and r.json()["id"] == g.pk
    r = c.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 200, r.content
    hiring.refresh_from_db()
    assert hiring.live.published_under == "maintainer:permissions"
    assert hiring.live.reach_changes == ["changes who opens “Reports”: recruiter → interviewer"]
