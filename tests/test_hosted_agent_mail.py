"""Canopy-hosted email for new agents (marshellis.com, 2026-10-18): an agent whose address
is on a workspace's verified mail domain is routed when it is created, the setup
checklist and the runner treat it as working with no Gmail sign-in, and people with a
verified Google account at such a domain may sign in and ask to join."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.agents.setup import _mailbox
from apps.mail import domains
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(settings):
    settings.MAIL_ENABLED = True
    owner = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role="editor")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified", receiving=True)
    return type("W", (), dict(owner=owner, editor=editor, ws=ws, d=d))


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def _create(user, slug, email):
    return _c(user).post("/api/agents/", {
        "slug": slug, "name": slug.title(), "description": "d", "persona": "", "email": email,
        "avatar_url": "", "workspace": "marshellis"}, content_type="application/json")


def test_creating_an_agent_on_a_hosted_domain_routes_its_mailbox(w):
    assert _create(w.owner, "scout", "Scout@marshellis.com").status_code == 201
    r = MailRoute.objects.get(domain=w.d, local_part="scout")
    assert r.kind == "agent" and r.agent.slug == "scout"
    box = domains.agent_mailbox(Agent.objects.get(slug="scout"))
    assert box["kind"] == "hosted" and box["live"] is True


def test_a_google_address_gets_no_route(w):
    assert _create(w.owner, "gee", "gee@gmail.com").status_code == 201
    assert not MailRoute.objects.filter(agent__slug="gee").exists()
    assert domains.agent_mailbox(Agent.objects.get(slug="gee"))["kind"] == "google"


def test_an_editor_cannot_claim_an_address_by_naming_an_agent_after_it(w):
    # Unrouted mail falls to the catch-all; only someone who may change routes may take one.
    assert _create(w.editor, "billing", "billing@marshellis.com").status_code == 201
    assert not MailRoute.objects.filter(local_part="billing").exists()
    step = _mailbox(Agent.objects.get(slug="billing"), "/x")
    assert step["status"] != "done"


def test_an_address_that_already_goes_elsewhere_is_never_taken_over(w):
    MailRoute.objects.create(domain=w.d, local_part="support", kind="forward", forward_to=w.owner)
    assert _create(w.owner, "support", "support@marshellis.com").status_code == 201
    r = MailRoute.objects.get(domain=w.d, local_part="support")
    assert r.kind == "forward"
    assert domains.agent_mailbox(Agent.objects.get(slug="support"))["route"] == "taken"


def test_the_checklist_counts_a_live_hosted_route_as_done(w):
    _create(w.owner, "scout", "scout@marshellis.com")
    step = _mailbox(Agent.objects.get(slug="scout"), "/x")
    assert step["status"] == "done" and "canopy-hosted" in step["detail"]


def test_resolve_says_the_mailbox_is_ses_so_the_runner_skips_gmail(w):
    _create(w.owner, "scout", "scout@marshellis.com")
    from apps.agents.api import _mailbox_backend

    assert _mailbox_backend(Agent.objects.get(slug="scout")) == "ses"


def test_agent_mailbox_api_sets_and_routes_an_address(w):
    _create(w.owner, "scout", "")
    r = _c(w.owner).put("/api/mail/agents/scout/mailbox", {"address": "scout@marshellis.com"},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["kind"] == "hosted" and body["live"] is True and body["hosted_domains"] == ["marshellis.com"]
    assert _c(w.owner).get("/api/mail/agents/scout/mailbox").json()["live"] is True


def test_a_verified_domain_can_let_its_google_accounts_sign_in(w):
    assert not wsvc.email_admitted_outside_domain("mom@marshellis.com")
    r = _c(w.owner).patch(f"/api/mail/domains/marshellis/{w.d.pk}", {"sign_in": True},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["sign_in"] is True
    assert wsvc.email_admitted_outside_domain("mom@marshellis.com")
    assert not wsvc.email_admitted_outside_domain("mom@elsewhere.com")
    assert [ws.slug for ws, _, _ in wsvc.requestable_workspaces(
        User.objects.create_user("mom", "mom@marshellis.com", "pw"))] == ["marshellis"]


def test_only_an_owner_and_only_a_verified_domain(w):
    WorkspaceMembership.objects.filter(user=w.editor).update(role="admin")
    r = _c(w.editor).patch(f"/api/mail/domains/marshellis/{w.d.pk}", {"sign_in": True},
                           content_type="application/json")
    assert r.status_code == 403
    MailDomain.objects.filter(pk=w.d.pk).update(status="pending")
    r = _c(w.owner).patch(f"/api/mail/domains/marshellis/{w.d.pk}", {"sign_in": True},
                          content_type="application/json")
    assert r.status_code == 409


def test_a_request_list_domain_without_a_verified_mail_domain_is_not_a_login_grant(w):
    Workspace.objects.filter(slug="marshellis").update(access_request_domains=["gmail.com"])
    assert not wsvc.email_admitted_outside_domain("anyone@gmail.com")
