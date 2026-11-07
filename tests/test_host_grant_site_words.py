"""The permission prompt says what the SITE says about itself: its name, the page this
permission is for, and what each scope lets an agent do (its signed site.json, which canopy
already reads and caches). Descriptive only, never enforced, and never able to fail the route."""
from __future__ import annotations

from unittest import mock

import pytest
from canopy_sdk import site as wire
from django.test import Client

from apps.host_grants import site_needs
from apps.tokens.models import DelegatedToken
from tests.fixtures import passkey_confirm
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db

SCOPES = "marketplace:read reviews:read marketplace:review"


@pytest.fixture()
def w():
    return sg.world()


def _needs(**extra):
    doc = wire.site_document(
        "connect-labs",
        pages=[{"key": "marketplace:round", "title": "A marketplace round",
                "scopes": SCOPES.split()},
               {"key": "marketplace:network", "title": "The partner network", "scopes": ["marketplace:read"]}],
        **extra)
    return site_needs.Needs(url="https://labs.example/canopy/site.json",
                            document=wire.SiteNeeds.from_claims(doc, site_name="connect-labs"))


def _grant_out(w, needs, scope=SCOPES):
    grant = sg.grant(w, confirmed_at=None, scope=scope)
    raw, _ = DelegatedToken.issue(app=w["app"], user=w["visitor"], ttl_seconds=600, host_grant=grant)
    with mock.patch.object(site_needs, "needs_for", needs):
        response = passkey_confirm.panel(raw).get("/api/embed/host-grant")
    assert response.status_code == 200, response.content
    return response.json()["grant"]


def test_the_grant_carries_the_sites_name_page_and_scope_phrases(w):
    needs = _needs(title="Connect Labs", scope_labels={
        "reviews:read": "read a round's responses and reviews",
        "marketplace:read": "read the network's organisations and rounds",
        "marketplace:review": "write AI reviews, each confirmed on a card",
        "elsewhere:read": "a scope this grant does not hold",
    })
    out = _grant_out(w, lambda app: needs)
    assert out["site"] == "connect-labs" and out["site_title"] == "Connect Labs"
    assert out["page_title"] == "A marketplace round"
    # In the order of the grant's own scopes, and only the ones it holds.
    assert [(s["scope"], s["label"]) for s in out["scope_labels"]] == [
        ("marketplace:read", "read the network's organisations and rounds"),
        ("reviews:read", "read a round's responses and reviews"),
        ("marketplace:review", "write AI reviews, each confirmed on a card"),
    ]


def test_a_site_that_publishes_nothing_gets_the_plain_grant(w):
    out = _grant_out(w, lambda app: _needs())
    assert out["site_title"] == "" and out["scope_labels"] == []
    assert out["scope"] == SCOPES, "the raw scopes are always there"


def test_no_page_title_when_two_pages_grant_exactly_these_scopes(w):
    doc = wire.site_document("connect-labs", pages=[
        {"key": "a", "title": "One", "scopes": ["marketplace:read"]},
        {"key": "b", "title": "Two", "scopes": ["marketplace:read"]}])
    needs = site_needs.Needs(url="u", document=wire.SiteNeeds.from_claims(doc, site_name="connect-labs"))
    assert _grant_out(w, lambda app: needs, scope="marketplace:read")["page_title"] == ""


@pytest.mark.parametrize("failure", [
    lambda app: site_needs.Needs(url="", document=None, reason="unreachable"),
    mock.Mock(side_effect=RuntimeError("the site is down")),
])
def test_an_unreadable_site_never_fails_the_grant(w, failure):
    out = _grant_out(w, failure)
    assert out["confirmed"] is False and out["site_title"] == "" and out["scope_labels"] == []


def test_the_settings_list_says_the_same(w):
    needs = _needs(title="Connect Labs", scope_labels={"marketplace:read": "read organisations"})
    sg.grant(w, scope="marketplace:read")
    browser = Client()
    browser.force_login(w["visitor"])
    with mock.patch.object(site_needs, "needs_for", lambda app: needs):
        rows = browser.get("/api/me/host-grants/").json()
    assert rows[0]["site_title"] == "Connect Labs"
    assert rows[0]["scope_labels"] == [{"scope": "marketplace:read", "label": "read organisations"}]
