"""Shared mail domains: a superuser lets other workspaces hold addresses on a domain.

The grant names a workspace and covers everything below it. Each workspace sees and
manages only its own addresses; the domain's own workspace keeps DNS, the catch-all and
the role addresses; a shared workspace sends under its own cap; revoking the grant makes
its addresses inert without deleting anything.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import domains, ses
from apps.mail.models import MailDomain, MailDomainShare, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _aws(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    from apps.mail import dns
    monkeypatch.setattr(dns, "_resolve", lambda n, t: [])
    monkeypatch.setattr(ses, "identity_verified", lambda d: True)
    monkeypatch.setattr(ses, "identity_status", lambda d: ses.IdentityStatus("SUCCESS", "SUCCESS", []))


@pytest.fixture()
def w(monkeypatch):
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or f"ses-{len(sent)}")
    root = User.objects.create_superuser("root", "root@dimagi.com", "pw")
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    cadmin = User.objects.create_user("c", "c@dimagi.com", "pw")
    owner_ws = Workspace.objects.create(slug="labs", display_name="Labs", created_by=jj)
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    other = Workspace.objects.create(slug="acme", display_name="Acme", created_by=jj)
    WorkspaceMembership.objects.create(workspace=owner_ws, user=jj, role="owner")
    WorkspaceMembership.objects.create(workspace=connect, user=cadmin, role="admin")
    WorkspaceMembership.objects.create(workspace=other, user=cadmin, role="admin")
    d = MailDomain.objects.create(workspace=owner_ws, name="dimagi.ai", status="verified", daily_send_cap=100)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=connect, owner=cadmin)
    Agent.objects.create(slug="zed", name="Zed", workspace=other, owner=cadmin)
    return type("W", (), dict(root=root, jj=jj, cadmin=cadmin, owner_ws=owner_ws, dimagi=dimagi,
                              connect=connect, other=other, d=d, eva=eva, sent=sent))


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def _share(w, ws="dimagi", **kw):
    return _c(w.root).put(f"/api/mail/domains/labs/{w.d.pk}/shares", {"workspace": ws, **kw},
                          content_type="application/json")


def _route(w, ws, body, user=None):
    return _c(user or w.cadmin).put(f"/api/mail/domains/{ws}/{w.d.pk}/routes", body,
                                    content_type="application/json")


def test_only_a_superuser_shares_and_the_grant_covers_descendants(w):
    assert _c(w.jj).put(f"/api/mail/domains/labs/{w.d.pk}/shares", {"workspace": "dimagi"},
                        content_type="application/json").status_code == 403
    r = _share(w)
    assert r.status_code == 200, r.content
    assert r.json()["shares"] == [{"workspace": "dimagi", "daily_send_cap": 200}]
    listed = _c(w.cadmin).get("/api/mail/domains/connect").json()["items"]
    assert [(i["name"], i["owned"], i["owner_workspace"], i["records"]) for i in listed] == [
        ("dimagi.ai", False, "labs", [])]
    assert _c(w.cadmin).get("/api/mail/domains/acme").json()["items"] == []


def test_sharing_with_the_owner_reaches_its_sub_workspaces(w):
    child = Workspace.objects.create(slug="labs-sub", display_name="Sub", created_by=w.jj, parent=w.owner_ws)
    WorkspaceMembership.objects.create(workspace=child, user=w.cadmin, role="admin")
    assert _c(w.cadmin).get("/api/mail/domains/labs-sub").json()["items"] == []
    assert _share(w, ws="labs").status_code == 200
    [item] = _c(w.cadmin).get("/api/mail/domains/labs-sub").json()["items"]
    assert item["owned"] is False and item["owner_workspace"] == "labs"
    # the owner's own view is unchanged: still owned, still its DNS
    [own] = _c(w.jj).get("/api/mail/domains/labs").json()["items"]
    assert own["owned"] is True


def test_shared_workspace_routes_its_agent_and_mail_lands_in_its_workspace(w, monkeypatch):
    _share(w)
    r = _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"})
    assert r.status_code == 200, r.content
    route = MailRoute.objects.get(local_part="eva")
    assert route.workspace_id == "connect"
    domain, got = domains.resolve("eva@dimagi.ai")
    assert got == route
    assert _c(w.cadmin).get("/api/mail/resolve", {"address": "eva@dimagi.ai"}).json() == {
        "backend": "ses", "workspace": "connect", "agent": "eva", "address": "eva@dimagi.ai"}
    # the owner sees its domain, but not another tenant's address on it
    assert _c(w.jj).get("/api/mail/domains/labs").json()["items"][0]["routes"] == []


def test_send_from_a_shared_address_is_the_workspaces_own_and_capped(w):
    _share(w, daily_send_cap=2)
    _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"})
    send = lambda: _c(w.cadmin).post("/api/mail/send/connect",  # noqa: E731
                                     {"agent": "eva", "to": ["x@x.org"], "subject": "Hi", "text": "x"},
                                     content_type="application/json")
    assert send().status_code == 200
    assert w.sent[0]["from_addr"] == "eva@dimagi.ai"
    t = MailThread.objects.get()
    assert t.workspace_id == "connect" and MailMessage.objects.get().workspace_id == "connect"
    assert send().status_code == 200
    r = send()
    assert r.status_code == 429 and "this workspace" in r.json()["detail"]


def test_the_domain_wide_cap_counts_every_workspace(w):
    MailDomain.objects.filter(pk=w.d.pk).update(daily_send_cap=1)
    _share(w)
    _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"})
    body = {"agent": "eva", "to": ["x@x.org"], "subject": "Hi", "text": "x"}
    assert _c(w.cadmin).post("/api/mail/send/connect", body, content_type="application/json").status_code == 200
    r = _c(w.cadmin).post("/api/mail/send/connect", body, content_type="application/json")
    assert r.status_code == 429 and "dimagi.ai reached" in r.json()["detail"]


def test_an_address_held_elsewhere_is_taken_and_never_replaced(w):
    _share(w)
    MailDomainShare.objects.create(domain=w.d, workspace=w.other)
    assert _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"}).status_code == 200
    r = _route(w, "acme", {"local_part": "eva", "kind": "agent", "agent_slug": "zed"})
    assert r.status_code == 409 and "connect" not in r.content.decode()
    assert MailRoute.objects.get(local_part="eva").workspace_id == "connect"


@pytest.mark.parametrize("local", ["*", "postmaster", "abuse", "dmarc"])
def test_owner_only_addresses_are_refused_to_a_shared_workspace(w, local):
    _share(w)
    r = _route(w, "connect", {"local_part": local, "kind": "drop"})
    assert r.status_code == 422


def test_a_workspace_not_covered_cannot_route_or_manage_the_domain(w):
    _share(w)
    assert _route(w, "acme", {"local_part": "zed", "kind": "agent", "agent_slug": "zed"}).status_code == 404
    # a shared workspace manages routes, never the domain itself
    assert _c(w.cadmin).patch(f"/api/mail/domains/connect/{w.d.pk}", {"daily_send_cap": 5000},
                              content_type="application/json").status_code == 404
    assert _c(w.cadmin).delete(f"/api/mail/domains/connect/{w.d.pk}").status_code == 404


def test_unsharing_makes_the_address_inert_but_keeps_everything(w):
    _share(w)
    _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"})
    assert _c(w.cadmin).post("/api/mail/send/connect",
                             {"agent": "eva", "to": ["x@x.org"], "subject": "Hi", "text": "x"},
                             content_type="application/json").status_code == 200
    r = _c(w.root).delete(f"/api/mail/domains/labs/{w.d.pk}/shares/dimagi")
    assert r.status_code == 200 and r.json()["shares"] == []
    assert domains.resolve("eva@dimagi.ai")[1] is None
    r = _c(w.cadmin).post("/api/mail/send/connect",
                          {"agent": "eva", "to": ["x@x.org"], "subject": "Hi", "text": "x"},
                          content_type="application/json")
    assert r.status_code == 409 and "no longer shared" in r.json()["detail"]
    assert MailRoute.objects.filter(local_part="eva").exists() and MailThread.objects.count() == 1
    _share(w)
    assert domains.resolve("eva@dimagi.ai")[1] is not None  # re-sharing revives it


def test_unshared_address_falls_to_the_owners_catch_all(w):
    owner_agent_user = w.jj
    MailRoute.objects.create(domain=w.d, local_part="*", kind="forward", forward_to=owner_agent_user)
    _share(w)
    _route(w, "connect", {"local_part": "eva", "kind": "agent", "agent_slug": "eva"})
    MailDomainShare.objects.all().delete()
    assert domains.resolve("eva@dimagi.ai")[1].local_part == "*"
