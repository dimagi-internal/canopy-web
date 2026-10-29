"""A template update is judged on what IT changes about who can see and do what —
not only on the workspace's own changes riding along with it."""
from __future__ import annotations

import copy

import pytest

from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services, template_diff
from apps.workspace_apps.models import AppRoleGrant
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _client, _user

pytestmark = pytest.mark.django_db


def _hiring():
    return mf.validate(services._read_bundled("hiring"))


def test_an_update_that_touches_no_access_is_not_flagged():
    m = _hiring()
    new = copy.deepcopy(m)
    new["version"] = "9.9.0"
    new["collections"]["application"]["fields"]["salary_expectation"] = {"type": "text", "label": "Salary"}
    next(p for p in new["pages"] if p["slug"] == "today")["code"] += "\n// a member page's code changed"
    assert template_diff.changes(m, new) == []


@pytest.mark.parametrize("mutate, expect", [
    (lambda m: m["roles"]["recruiter"].update(capability="content.write"),
     "changes who holds 'recruiter': from workspace admins to every editor"),
    (lambda m: m["roles"].update(auditor={"capability": "read", "description": ""}),
     "adds the role 'auditor', held by every member"),
    (lambda m: m["collections"]["offer"].update(read="interviewer"), "changes who reads Offer"),
    (lambda m: m["collections"]["application"]["fields"]["eeo_gender"].pop("read"), "changes who reads “"),
    (lambda m: m["collections"]["job"].pop("scope"), "per-record access) in: Job"),
    (lambda m: m["collections"]["job"]["public"]["fields"].append("hiring_manager"),
     "shows more of Job publicly: hiring_manager"),
    (lambda m: next(p for p in m["pages"] if p["slug"] == "careers").update(code="function View(){return null}"),
     "changes the public page"),
    (lambda m: next(p for p in m["pages"] if p["slug"] == "offers").update(role="interviewer"),
     "changes who opens “Offers”: recruiter → interviewer"),
    (lambda m: m["actions"]["apply"]["input"].append("tags"), "changes what anyone on the public pages submits"),
    (lambda m: m["actions"]["send_offer"].update(role="interviewer"), "changes who can “Send the offer”"),
    (lambda m: m["actions"]["reject_and_email"]["effects"][0].update(to_field="referrer"), "changes who “Reject and email” emails"),
    (lambda m: m["links"]["offer"].update(days=365), "changes the private links"),
    (lambda m: m["retention"].pop(), "changes what candidate data is anonymized or deleted"),
])
def test_each_access_surface_is_named_in_plain_words(mutate, expect):
    m = _hiring()
    new = copy.deepcopy(m)
    mutate(new)
    found = template_diff.changes(m, new)
    assert any(expect in f for f in found), found


def test_a_maintainer_cannot_publish_an_update_that_changes_access_and_an_admin_can(world, hiring):
    molly = _user("molly")
    WorkspaceMembership.objects.create(user=molly, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    AppRoleGrant.objects.create(installation=hiring, user=molly, role="maintainer", granted_by=world["admin"])
    m = copy.deepcopy(services.bundled_manifest("hiring"))
    m["version"], m["changelog"] = "9.9.0", "9.9.0 — interviewers see the reports."
    next(p for p in m["pages"] if p["slug"] == "reports")["role"] = "interviewer"
    hiring.template.versions.create(version="9.9.0", manifest=mf.validate(m), changelog=m["changelog"])

    c = _client(molly)
    d = c.post(f"{BASE}/hiring/draft", {"upgrade": True}, content_type=J).json()
    assert d["review"]["template_change"]["risks"] == ["changes who opens “Reports”: recruiter → interviewer"]
    r = c.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 403 and "workspace admin" in r.json()["detail"]
    assert _client(world["admin"]).post(f"{BASE}/hiring/draft/publish").status_code == 200


def test_the_real_hiring_updates_are_judged_as_they_should_be():
    """2.1.0 changed who is a recruiter and added per-job access: it needs an
    admin. 2.2.0 (Today, a setup list, email names) changes no access."""
    m = _hiring()
    older = copy.deepcopy(m)
    older["roles"]["recruiter"]["capability"] = "content.write"
    assert template_diff.changes(older, m)
    assert template_diff.changes(m, copy.deepcopy(m)) == []
