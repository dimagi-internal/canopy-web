"""Every agent gets <slug>@<domain>: a domain's automatic agent addresses.

The switch is the domain owner's. It covers every workspace that may use the domain (its
own, and each workspace it is shared with and everything below), makes real agent routes
marked `auto`, and keeps them true as agents are created and moved and the switch or a
share changes. It never takes an address someone else holds, and a route an admin edits
becomes theirs.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.agents.move import move
from apps.mail import auto_addresses, domains, ses
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _aws(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    # The sync runs once the save commits; a test's transaction never does, so run it now.
    monkeypatch.setattr(auto_addresses.transaction, "on_commit", lambda fn: fn())
    from apps.mail import dns
    monkeypatch.setattr(dns, "_resolve", lambda n, t: [])
    monkeypatch.setattr(ses, "identity_status", lambda d: ses.IdentityStatus("SUCCESS", "SUCCESS", []))


@pytest.fixture()
def w():
    root = User.objects.create_superuser("root", "root@dimagi.com", "pw")
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    acme = Workspace.objects.create(slug="acme", display_name="Acme", created_by=jj)
    for ws in (dimagi, connect, acme):
        WorkspaceMembership.objects.create(workspace=ws, user=jj, role="owner")
    d = MailDomain.objects.create(workspace=dimagi, name="dimagi.ai", status="verified")
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=dimagi, owner=jj)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=jj)
    zed = Agent.objects.create(slug="zed", name="Zed", workspace=acme, owner=jj)
    return type("W", (), dict(root=root, jj=jj, dimagi=dimagi, connect=connect, acme=acme, d=d,
                              ada=ada, hal=hal, zed=zed))


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def _switch(w, on=True):
    r = _c(w.jj).patch(f"/api/mail/domains/dimagi/{w.d.pk}", {"agent_addresses": on},
                       content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _routes(w):
    return {r.local_part: (r.workspace_id, r.agent.slug if r.agent_id else "", r.auto)
            for r in MailRoute.objects.filter(domain=w.d).select_related("agent")}


def test_the_switch_gives_every_covered_agent_a_real_route(w):
    out = _switch(w)
    assert out["agent_addresses"] is True
    # Not shared yet: only the domain's own workspace is covered.
    assert _routes(w) == {"ada": ("dimagi", "ada", True)}
    assert [r["address"] for r in out["routes"] if r["auto"]] == ["ada@dimagi.ai"]
    _, route = domains.resolve("ada@dimagi.ai")
    assert route.agent.slug == "ada"  # the inbox works like any hand-made address
    assert domains.agent_addresses(w.ada)[-1]["address"] == "ada@dimagi.ai"


def test_sharing_brings_the_shared_tree_in_and_unsharing_takes_it_out(w):
    _switch(w)
    _c(w.root).put(f"/api/mail/domains/dimagi/{w.d.pk}/shares", {"workspace": "dimagi"},
                   content_type="application/json")
    assert _routes(w) == {"ada": ("dimagi", "ada", True), "hal": ("connect", "hal", True)}
    _c(w.root).delete(f"/api/mail/domains/dimagi/{w.d.pk}/shares/dimagi")
    assert _routes(w) == {"ada": ("dimagi", "ada", True)}


def test_new_and_moved_agents_follow(w):
    _switch(w)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=w.dimagi, owner=w.jj)
    assert _routes(w)["eva"] == ("dimagi", "eva", True)
    move(eva, w.acme, by=w.jj)  # out of the domain's reach: its automatic address goes
    assert "eva" not in _routes(w)
    move(w.zed, w.dimagi, by=w.jj)  # into it: zed gets one
    assert _routes(w)["zed"] == ("dimagi", "zed", True)


def test_a_reparented_workspace_comes_into_reach(w):
    _switch(w)
    _c(w.root).put(f"/api/mail/domains/dimagi/{w.d.pk}/shares", {"workspace": "dimagi"},
                   content_type="application/json")
    w.acme.parent = w.dimagi
    w.acme.save()
    assert _routes(w)["zed"] == ("acme", "zed", True)


def test_it_never_takes_an_address_and_says_which_it_skipped(w):
    domains.save_route(w.d, local_part="ada", kind="forward", forward_to_user_id=w.jj.pk)
    out = _switch(w)
    assert _routes(w)["ada"] == ("dimagi", "", False)
    assert out["agent_addresses_skipped"] == ["ada"]


def test_a_hand_made_route_for_the_agent_counts_and_is_left_alone(w):
    domains.save_route(w.d, local_part="ada", kind="agent", agent_slug="ada")
    _switch(w)
    _switch(w, on=False)
    assert _routes(w) == {"ada": ("dimagi", "ada", False)}


def test_switching_off_removes_only_the_automatic_routes(w):
    _switch(w)
    Agent.objects.create(slug="eva", name="Eva", workspace=w.dimagi, owner=w.jj)
    # An admin edits eva's automatic route (here: disables it): it is theirs now.
    r = _c(w.jj).put(f"/api/mail/domains/dimagi/{w.d.pk}/routes",
                     {"local_part": "eva", "kind": "agent", "agent_slug": "eva", "enabled": False},
                     content_type="application/json")
    assert r.json()["auto"] is False
    _switch(w, on=False)
    assert _routes(w) == {"eva": ("dimagi", "eva", False)}


def test_an_automatic_route_cannot_be_removed_by_hand(w):
    _switch(w)
    rid = MailRoute.objects.get(domain=w.d, local_part="ada").pk
    r = _c(w.jj).delete(f"/api/mail/domains/dimagi/{w.d.pk}/routes/{rid}")
    assert r.status_code == 409
    assert "automatic" in r.json()["detail"]


def test_only_the_viewers_own_agents_are_reported_skipped(w):
    _c(w.root).put(f"/api/mail/domains/dimagi/{w.d.pk}/shares", {"workspace": "dimagi"},
                   content_type="application/json")
    domains.save_route(w.d, local_part="hal", kind="forward", forward_to_user_id=w.jj.pk)
    _switch(w)
    w.d.refresh_from_db()
    assert auto_addresses.skipped_here(w.d, w.dimagi) == []
    assert auto_addresses.skipped_here(w.d, w.connect) == ["hal"]
