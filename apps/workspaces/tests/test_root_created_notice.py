"""A new top-level workspace created by someone an org counts as its own emails that org's
root owners (Jonathan, 2026-10-16: two divisions' agents lived in root workspaces nobody meant
to create, outside the dimagi tree and invisible to the fleet)."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.workspaces import notifications
from apps.workspaces.models import Workspace, WorkspaceMembership as M

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def _public_base(settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example"


def _user(email):
    return User.objects.create(username=email, email=email)


@pytest.fixture
def org():
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(slug="org", display_name="Org", created_by=owner,
                                  access_request_domains=["dimagi.com"])
    M.objects.create(workspace=ws, user=owner, role=M.OWNER)
    admin = _user("admin@dimagi.com")
    M.objects.create(workspace=ws, user=admin, role=M.ADMIN)
    return {"ws": ws, "owner": owner, "admin": admin}


def _create(user, slug, parent=None):
    c = Client()
    c.force_login(user)
    body = {"slug": slug, "display_name": slug.title()}
    if parent:
        body["parent"] = parent
    return c.post("/api/workspaces/", data=json.dumps(body), content_type="application/json")


def test_a_root_created_by_an_org_domain_user_emails_the_org_owners_only(org):
    mail.outbox.clear()
    r = _create(_user("teammate@dimagi.com"), "strategy")
    assert r.status_code == 201
    assert [m.to for m in mail.outbox] == [["owner@dimagi.com"]]  # owners, not admins
    msg = mail.outbox[0]
    assert "strategy" in msg.subject and "outside Org" in msg.subject
    assert "/api/workspaces/strategy/parent" in msg.body
    assert "move its agents (with their history) into org" in msg.body
    assert msg.reply_to == ["teammate@dimagi.com"]


def test_a_nested_workspace_sends_nothing(org):
    mail.outbox.clear()
    assert _create(org["owner"], "division", parent="org").status_code == 201
    assert mail.outbox == []


def test_another_domain_sends_nothing(org):
    outsider = _user("someone@family.example")
    M.objects.create(workspace=org["ws"], user=outsider, role=M.VIEWER)  # eligible to create
    mail.outbox.clear()
    assert _create(outsider, "family").status_code == 201
    assert mail.outbox == []


def test_the_org_owner_is_not_told_about_their_own_root(org):
    mail.outbox.clear()
    assert _create(org["owner"], "side").status_code == 201
    assert mail.outbox == []


def test_a_mail_failure_does_not_fail_the_create(org, monkeypatch):
    monkeypatch.setattr(notifications, "_send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp")))
    assert _create(_user("teammate@dimagi.com"), "strategy").status_code == 201
    assert Workspace.objects.filter(slug="strategy").exists()
