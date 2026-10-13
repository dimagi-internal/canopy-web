# tests/test_mail_domains_api.py
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import domains, ses
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def stub_aws(monkeypatch, settings):
    settings.MAIL_ENABLED = True
    monkeypatch.setattr(ses, "create_identity", lambda d, mail_from: ["t1", "t2", "t3"])
    monkeypatch.setattr(ses, "identity_verified", lambda d: True)
    monkeypatch.setattr(ses, "delete_identity", lambda d: None)
    from apps.mail import dns
    monkeypatch.setattr(dns, "_resolve", lambda n, t: ["10 mx.zoho.com."] if t == "MX" else [])


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jjackson@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=w, user=owner, role="owner")
    return w


def _c(u):
    c = Client(); c.force_login(u); return c


def test_admin_adds_a_domain_and_sees_records_and_current_provider(owner, ws):
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "Marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["name"] == "marshellis.com"
    assert body["mx_provider"] == "Zoho"
    assert {rec["purpose"] for rec in body["records"]} >= {"dkim-1", "spf", "dmarc", "mx"}


def test_editor_cannot_add_a_domain(ws):
    ed = User.objects.create_user("ed", "ed@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role="editor")
    r = _c(ed).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                    content_type="application/json")
    assert r.status_code == 403


def test_domain_taken_elsewhere_is_409_without_naming_the_holder(owner, ws):
    other = Workspace.objects.create(slug="o", display_name="O", created_by=owner)
    MailDomain.objects.create(workspace=other, name="marshellis.com")
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 409 and "o" not in r.json().get("detail", "").split()


def test_forward_route_requires_a_member(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    stranger = User.objects.create_user("s", "s@x.com", "pw")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": stranger.pk},
                      content_type="application/json")
    assert r.status_code == 422
    ok = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                       {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": owner.pk},
                       content_type="application/json")
    assert ok.status_code == 200, ok.content


def test_reserved_local_part_cannot_be_routed(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "canopy-forwarder", "kind": "drop"}, content_type="application/json")
    assert r.status_code == 422


def test_resolve_exact_beats_catch_all_and_subdomain_uses_parent(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("Ada@marshellis.com")[1].kind == "agent"
    assert domains.resolve("apple@marshellis.com")[1].kind == "forward"
    assert domains.resolve("ada@mail.marshellis.com")[1].kind == "agent"
    assert domains.resolve("x@nobody.org") == (None, None)


def test_pending_domain_routes_nothing(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="pending")
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("a@marshellis.com") == (d, None)


def test_refresh_flips_pending_to_verified(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", dkim_tokens=["t1"])
    assert domains.refresh(d, force=True).status == "verified"
