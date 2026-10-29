"""An act-as-you permission (a `HostGrant`) is usable only once the person confirmed it
with their passkey, for 30 minutes from that touch, and a contact never holds one.

* Unconfirmed: the gateway, Views (`lookup`), Settings and the gateway's own
  `context_for_grant` all refuse it; the panel sees it waiting (`confirmed: false`).
* Confirming is a real WebAuthn assertion with one of the token's user's own passkeys
  (`SoftAuthenticator`), through the panel's token; anything else is refused.
* A re-mint of the same page inherits the confirmation AND its end — renewing the
  token never asks again and never extends the permission.
* A contact is redeemed no grant, and is never offered an agent that serves the site
  on its pages (it could act there only through a permission they cannot confirm).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.host_grants import host_gateway, host_grants
from apps.tokens.models import AppCredentialAgent, ContactToken, DelegatedToken, HostGrant
from tests.fixtures import passkey_confirm
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db


@pytest.fixture()
def host(monkeypatch):
    return sg.host_mcp(monkeypatch)


@pytest.fixture()
def w():
    return sg.world()


def _panel_token(w, grant) -> str:
    """The panel's token, carrying `grant` as the page it is on."""
    raw, _ = DelegatedToken.issue(app=w["app"], user=w["visitor"], ttl_seconds=600, host_grant=grant)
    return raw


# --- unconfirmed: nothing uses it ----------------------------------------------------------


def test_an_unconfirmed_grant_is_refused_by_the_gateway_and_the_host_is_never_called(w, host):
    sg.grant(w, confirmed_at=None)
    with sg.visitor(w), pytest.raises(Exception, match="confirm with their passkey"):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == [] and host.seen == []


def test_a_send_alone_never_asks_only_a_turn_that_needs_the_grant_does(w, host):
    """The panel asks for the passkey only once the agent's turn needs to act as the
    person: the grant reads `requested: false` until the gateway is refused for it,
    and `true` after — the agent is told it was asked, and to retry."""
    waiting = sg.grant(w, confirmed_at=None)
    panel = passkey_confirm.panel(_panel_token(w, waiting))
    assert panel.get("/api/embed/host-grant").json()["grant"]["requested"] is False
    with sg.visitor(w), pytest.raises(Exception, match="I have asked the person"):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert panel.get("/api/embed/host-grant").json()["grant"]["requested"] is True
    assert host.calls == []


def test_a_renewal_while_asked_keeps_the_ask(w):
    """The panel re-mints every few minutes; a renewal mid-ask must not close the
    prompt the person is about to answer."""
    asked = sg.grant(w, confirmed_at=None, requested_at=timezone.now())
    renewal = HostGrant(app=w["app"], subject=asked.subject, user=asked.user, scope=asked.scope,
                        expires_at=timezone.now() + timedelta(minutes=10))
    host_grants._inherit(renewal)
    assert renewal.requested_at == asked.requested_at and renewal.confirmed_at is None


def test_lookup_says_unconfirmed_and_views_get_the_passkey_message(w):
    grant = sg.grant(w, confirmed_at=None)
    w["session"].refresh_from_db()
    found = host_grants.lookup(w["app"], agent=w["agent"], user=w["visitor"], session=w["session"])
    assert found.grant is None and found.reason == host_grants.UNCONFIRMED
    # Outside a conversation too: only a confirmed grant is ever the person's latest.
    assert host_grants.grant_for(w["app"], agent=w["agent"], user=w["visitor"]) is None
    assert "passkey" in host_gateway.no_grant_message("connect-labs", found.reason, "ACE")
    # The gateway's one door for a stored grant refuses it as well.
    with pytest.raises(host_gateway.GatewayRefusal) as refused:
        host_gateway.context_for_grant(w["app"], grant, turn_id="t", agent_slug="ace", ceiling=[])
    assert refused.value.code == "unconfirmed"


def test_settings_lists_only_confirmed_grants_and_the_panel_sees_one_waiting(w):
    waiting = sg.grant(w, confirmed_at=None)
    assert host_grants.live_for(user=w["visitor"]) == []
    browser = Client()
    browser.force_login(w["visitor"])
    assert browser.get("/api/me/host-grants/").json() == []
    r = passkey_confirm.panel(_panel_token(w, waiting)).get("/api/embed/host-grant")
    assert r.status_code == 200, r.content
    assert r.json()["grant"]["id"] == waiting.pk and r.json()["grant"]["confirmed"] is False


# --- confirming --------------------------------------------------------------------------


def test_a_passkey_assertion_confirms_the_grant_for_thirty_minutes_at_most(w):
    grant = sg.grant(w, confirmed_at=None, expires_at=timezone.now() + timedelta(hours=2))
    auth = passkey_confirm.give_passkey(w["visitor"])
    r = passkey_confirm.confirm(_panel_token(w, grant), auth)
    assert r.status_code == 200, r.content
    assert r.json()["grant"]["confirmed"] is True
    grant.refresh_from_db()
    assert grant.confirmed_at is not None
    assert grant.expires_at <= grant.confirmed_at + timedelta(seconds=host_grants.CONFIRM_SECONDS)
    assert host_grants.live_for(user=w["visitor"]) == [grant]
    from apps.events.models import SettingChange

    assert SettingChange.objects.filter(operation__icontains="finish_confirm_host_grant").exists()


def test_confirming_never_outlives_the_hosts_own_token(w):
    end = timezone.now() + timedelta(minutes=5)
    grant = sg.grant(w, confirmed_at=None, expires_at=end)
    passkey_confirm.confirm(_panel_token(w, grant), passkey_confirm.give_passkey(w["visitor"]))
    grant.refresh_from_db()
    assert grant.confirmed_at is not None and grant.expires_at == end


def test_someone_elses_passkey_does_not_confirm(w):
    grant = sg.grant(w, confirmed_at=None)
    passkey_confirm.give_passkey(w["visitor"])
    stranger = passkey_confirm.give_passkey(User.objects.create_user("eve", "eve@x.org", "pw"))
    token = _panel_token(w, grant)
    started = passkey_confirm.begin(token)
    assert started.status_code == 200, started.content
    r = passkey_confirm.finish(token, stranger.get(started.json()["options"]))
    assert r.status_code == 400
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_finishing_without_a_ceremony_is_refused(w):
    grant = sg.grant(w, confirmed_at=None)
    auth = passkey_confirm.give_passkey(w["visitor"])
    forged = auth.get({"challenge": "bm90LWEtY2hhbGxlbmdl"})
    assert passkey_confirm.finish(_panel_token(w, grant), forged).status_code == 400
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_a_challenge_begun_for_one_page_cannot_confirm_another(w):
    page_a = sg.grant(w, confirmed_at=None, acted=False)
    page_b = sg.grant(w, confirmed_at=None, acted=False, scope="data:read")
    auth = passkey_confirm.give_passkey(w["visitor"])
    started = passkey_confirm.begin(_panel_token(w, page_a))
    assert started.status_code == 200
    r = passkey_confirm.finish(_panel_token(w, page_b), auth.get(started.json()["options"]))
    assert r.status_code == 400
    page_b.refresh_from_db()
    assert page_b.confirmed_at is None


def test_with_no_passkey_the_panel_is_told_to_register_one(w):
    grant = sg.grant(w, confirmed_at=None)
    r = passkey_confirm.begin(_panel_token(w, grant))
    assert r.status_code == 409 and r.json()["error"] == "no_passkey"


def test_a_token_carrying_no_grant_has_nothing_to_confirm(w):
    passkey_confirm.give_passkey(w["visitor"])
    raw, _ = DelegatedToken.issue(app=w["app"], user=w["visitor"], ttl_seconds=600)
    assert passkey_confirm.begin(raw).status_code == 404


def test_a_contact_token_cannot_confirm_anything(w):
    grant = HostGrant.objects.create(app=w["app"], subject="u-42", contact=w["contact"],
                                     access_token_enc="x", resource=sg.RESOURCE, dpop_jkt="j",
                                     expires_at=timezone.now() + timedelta(minutes=10))
    raw, _ = ContactToken.issue(app=w["app"], contact=w["contact"], ttl_seconds=600, host_grant=grant)
    r = passkey_confirm.begin(raw)
    assert r.status_code in (401, 403, 404), r.content
    grant.refresh_from_db()
    assert grant.confirmed_at is None


# --- a re-mint inherits the confirmation and its end ------------------------------------


@pytest.fixture()
def redeemable(monkeypatch):
    """`host_grants.redeem` with the host's side stood in: every redemption returns a
    30-minute access token for `marketplace:read`."""
    monkeypatch.setattr(host_grants, "check_id_jag", lambda app, id_jag, *, subject: {})
    monkeypatch.setattr(host_grants, "discover",
                        lambda issuer: {"issuer": issuer, "token_endpoint": "https://host.test/o/token/"})
    monkeypatch.setattr(host_grants, "_token_request", lambda endpoint, form: (200, {
        "access_token": "host-at-NEW", "token_type": "DPoP", "expires_in": 1800,
        "scope": "marketplace:read"}))

    def redeem(w, **kw):
        return host_grants.redeem(w["app"], "id-jag", subject="u-42", user=w["visitor"], **kw)
    return redeem


def test_a_fresh_page_is_unconfirmed(w, redeemable):
    assert redeemable(w).confirmed_at is None


def test_a_remint_inherits_the_confirmation_and_never_extends_it(w, redeemable):
    now = timezone.now()
    touched = now - timedelta(minutes=20)
    first = sg.grant(w, confirmed_at=touched, expires_at=now + timedelta(minutes=10))
    again = redeemable(w)
    assert again.confirmed_at == touched
    assert again.expires_at == touched + timedelta(seconds=host_grants.CONFIRM_SECONDS)
    assert again.expires_at <= first.expires_at + timedelta(seconds=1)


def test_after_the_confirmations_thirty_minutes_a_remint_asks_again(w, redeemable):
    now = timezone.now()
    sg.grant(w, confirmed_at=now - timedelta(minutes=31), expires_at=now - timedelta(minutes=1))
    assert redeemable(w).confirmed_at is None


def test_another_pages_confirmation_is_not_inherited(w, redeemable):
    sg.grant(w, scope="data:read")  # a different page's live, confirmed permission
    assert redeemable(w).confirmed_at is None


def test_a_stopped_pages_confirmation_is_not_inherited(w, redeemable):
    stopped = sg.grant(w)
    host_grants.revoke(stopped)
    assert redeemable(w).confirmed_at is None


def test_a_contact_is_never_redeemed_a_grant(w, redeemable):
    with pytest.raises(host_grants.HostGrantError) as refused:
        host_grants.redeem(w["app"], "id-jag", subject="u-42", contact=w["contact"])
    assert refused.value.code == "contact"
    assert not HostGrant.objects.exists()


def test_the_live_probe_still_redeems_its_own_grant_no_one_can_reach(w, redeemable):
    probe = host_grants.redeem(w["app"], "id-jag", subject="canopy-probe", probe=True)
    assert probe.user is None and probe.contact is None and probe.confirmed_at is not None
    # Usable by the probe's own door, and by no person's lookup.
    host_gateway.context_for_grant(w["app"], probe, turn_id="probe", agent_slug="canopy-probe",
                                   ceiling=[])
    assert host_grants.grant_for(w["app"], agent=None, user=w["visitor"]) is None
    assert host_grants.grant_for(w["app"], agent=None, contact=w["contact"]) is None


# --- contacts are not offered agents that serve the site on its pages -------------------


def test_a_contact_is_offered_only_agents_that_do_not_act_on_the_site(w):
    # `ace` serves connect-labs on its pages (its interface lists the site in `sites:`).
    helper = Agent.objects.create(slug="helper", name="Helper", workspace=w["ws"], owner=w["owner"])
    AppCredentialAgent.objects.create(app=w["app"], agent=helper)
    raw, _ = ContactToken.issue(app=w["app"], contact=w["contact"], ttl_seconds=600)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    me = c.get("/api/contact/me")
    assert me.status_code == 200, me.content
    assert [a["slug"] for a in me.json()["agents"]] == ["helper"]
    refused = c.post("/api/contact/sessions", data={"agent_slug": "ace"}, content_type="application/json")
    assert refused.status_code == 404
    started = c.post("/api/contact/sessions", data={"agent_slug": "helper"}, content_type="application/json")
    assert started.status_code == 200, started.content


def test_a_site_offering_only_such_agents_offers_a_contact_none(w):
    raw, _ = ContactToken.issue(app=w["app"], contact=w["contact"], ttl_seconds=600)
    me = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get("/api/contact/me")
    assert me.status_code == 200 and me.json()["agents"] == []
