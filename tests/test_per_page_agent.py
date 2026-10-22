"""A page that chose its agent binds its visitor's token to that agent.

connect-labs puts `labsdata` (under ZDR) on its data pages and keeps ACE (no ZDR)
on the marketplace. The page signs its choice into the visitor assertion
(`canopy_agent`, `canopy_sdk.contract.AGENT_CLAIM`); canopy checks it is the agent
the arrival names and one the site offers, then holds the token — every door a
site's token reaches — and the host grant it redeems to that agent alone. An
arrival with no claim is exactly what it was before: the site's whole allow-list.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from canopy_sdk import contract
from canopy_sdk.host import HostConfig, sign_visitor_assertion
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.tokens import assertions, delegation
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

IFACE = {"capabilities": {"connect": {"callers": ["contact", "member"],
                                      "tools": ["mcp__*canopy-web__who_is_asking"]}}}


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def key():
    return generate_private_key("EdDSA")


@pytest.fixture()
def site(key):
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                               interface=parse(IFACE))
    labsdata = Agent.objects.create(slug="labsdata", name="Labs data", workspace=ws, owner=owner,
                                    interface=parse(IFACE))
    hidden = Agent.objects.create(slug="hidden", name="Not offered", workspace=ws, owner=owner,
                                  interface=parse(IFACE))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save()
    for agent in (ace, labsdata):
        AppCredentialAgent.objects.create(app=app, agent=agent)
    return {"owner": owner, "ws": ws, "ace": ace, "labsdata": labsdata, "hidden": hidden,
            "app": app}


def _arrive(key, *, bound="", named=None, requirements=(), subject="u-42", email="",
            verified=False):
    cfg = HostConfig(signing_key=key, canopy_base_url=assertions.audience(),
                     app_name="connect-labs")
    assertion = sign_visitor_assertion(cfg, subject, name="Gillian", email=email,
                                       email_verified=verified, agent=bound,
                                       runner_requirements=requirements)
    return Client().post(contract.ARRIVAL_PATH,
                         data={"assertion": assertion,
                               "agent_slug": bound if named is None else named},
                         content_type="application/json")


def _bearer(raw):
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _token(response):
    assert response.status_code == 200, response.content
    return response.json()["token"]


# --- the arrival ------------------------------------------------------------------


def test_a_bound_arrival_mints_a_token_bound_to_its_agent(site, key):
    _token(_arrive(key, bound="labsdata", requirements=["zdr"]))
    token = ContactToken.objects.get()
    assert token.bound_agent == site["labsdata"] and token.runner_requirements == ["zdr"]


def test_an_unbound_arrival_is_what_it_always_was(site, key):
    _token(_arrive(key, named="ace"))
    assert ContactToken.objects.get().bound_agent is None


def test_the_signed_agent_must_be_the_one_the_arrival_names(site, key):
    for named in ("ace", ""):
        r = _arrive(key, bound="labsdata", named=named)
        assert r.status_code == 400 and "agent_mismatch" in r.json()["detail"]
    assert not ContactToken.objects.exists()


def test_the_signed_agent_must_be_one_the_site_offers(site, key):
    r = _arrive(key, bound="hidden")
    assert r.status_code == 403 and not ContactToken.objects.exists()


def test_a_member_arrival_is_bound_too(site, key):
    member = User.objects.create_user("mem", "mem@dimagi.com", "pw")
    EmailAddress.objects.create(user=member, email=member.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=member, workspace=site["ws"],
                                       role=WorkspaceMembership.EDITOR)
    r = _arrive(key, bound="labsdata", email="mem@dimagi.com", verified=True)
    assert r.json()["kind"] == "user"
    assert DelegatedToken.objects.get().bound_agent == site["labsdata"]
    raw = r.json()["token"]
    # The embed picker offers only the page's agent…
    assert [a["slug"] for a in _bearer(raw).get("/api/embed/agents").json()] == ["labsdata"]
    # …and no other agent's chat can be opened with this token.
    refused = _bearer(raw).post("/api/canopy-sessions/", {"agent_slug": "ace"},
                                content_type="application/json")
    assert refused.status_code == 404
    ok = _bearer(raw).post("/api/canopy-sessions/", {"agent_slug": "labsdata"},
                           content_type="application/json")
    assert ok.status_code == 200, ok.content


# --- a contact's doors ------------------------------------------------------------


def test_a_bound_contact_reaches_only_its_agent(site, key):
    unbound = _token(_arrive(key, named="ace"))
    ace_chat = _bearer(unbound).post("/api/contact/sessions", {"agent_slug": "ace"},
                                     content_type="application/json").json()["id"]

    raw = _token(_arrive(key, bound="labsdata"))
    me = _bearer(raw).get("/api/contact/me").json()
    assert [a["slug"] for a in me["agents"]] == ["labsdata"]
    assert _bearer(raw).post("/api/contact/sessions", {"agent_slug": "ace"},
                             content_type="application/json").status_code == 404
    mine = _bearer(raw).post("/api/contact/sessions", {"agent_slug": "labsdata"},
                             content_type="application/json")
    assert mine.status_code == 200, mine.content
    # The visitor's earlier ACE conversation is neither listed nor reachable here.
    listed = [s["id"] for s in _bearer(raw).get("/api/contact/sessions").json()]
    assert listed == [mine.json()["id"]]
    assert _bearer(raw).get(f"/api/contact/sessions/{ace_chat}").status_code == 404
    send = _bearer(raw).post(f"/api/contact/sessions/{ace_chat}/send", {"text": "hi"},
                             content_type="application/json")
    assert send.status_code == 404
    assert Session.objects.get(pk=ace_chat).messages.count() == 0
    # The unbound token still reaches both, as before.
    assert _bearer(unbound).get(f"/api/contact/sessions/{ace_chat}").status_code == 200


def test_unlisting_the_bound_agent_does_not_widen_the_token(site):
    offered = {site["ace"].pk, site["labsdata"].pk}
    assert delegation.narrowed(offered, site["labsdata"].pk) == {site["labsdata"].pk}
    assert delegation.narrowed({site["ace"].pk}, site["labsdata"].pk) == set()
    assert delegation.narrowed(offered, None) == offered
    assert delegation.narrowed(None, site["labsdata"].pk) is None
