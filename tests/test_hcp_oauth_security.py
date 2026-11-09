"""The HCP service's OAuth properties that protect a person, one attack at a time.

`test_hcp_service.py` walks the flow and pins each gate once. This file attacks it:
each test is something an outside app, another client or another person would try, and
the property that stops it. It reuses that file's helpers and fixtures.

* a code or refresh token belongs to the client it was issued to;
* PKCE cannot be downgraded (a plain verifier presented as a challenge buys nothing);
* a redirect URI matches exactly, in every near-miss shape;
* a token never carries more scope than the grant, whatever the client asks later;
* a revoked or expired grant opens nothing, at the token endpoint or at the API;
* a token minted into a session is not a person: it cannot answer the consent screen;
* one person's token reaches nothing of another's, and one client cannot touch another's;
* the internal-only gate holds when its domain list is empty.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets

import pytest
from django.test import Client
from django.utils import timezone

from apps.contacts import hcp, hcp_oauth
from apps.contacts.models import HcpClient, PersonAuditEvent, PersonFact, PersonGrant
from tests.fixtures.hcp_oauth import (
    READ,
    REDIRECT,
    WRITE,
    _as,
    _consent,
    _params,
    _personal,
    _pkce,
    _search,
    _token,
    _tokens,
)
from tests.token_helpers import bearer_headers

pytestmark = pytest.mark.django_db

AUTHORIZE = "/api/hcp/oauth/authorize"


@pytest.fixture(autouse=True)
def _on(agent_memory_on, settings):
    """Production's login gate on, internal audience, a clean cache (rate counters and
    pending approvals live there): the same footing as test_hcp_service.py."""
    from django.core.cache import cache

    settings.REQUIRE_AUTH = True
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com,dimagi-ai.com"
    settings.HCP_SERVICE_AUDIENCE = "internal"
    cache.clear()
    yield


@pytest.fixture()
def admin():
    from django.contrib.auth.models import User

    return User.objects.create_superuser("root", "root@dimagi.com", "pw")


@pytest.fixture()
def lili():
    from django.contrib.auth.models import User

    return User.objects.create_user("lili", "lili@dimagi.com", "pw")


def _register(admin, name="Recipe Planner"):
    r = _as(admin).post("/api/hcp-admin/clients", data=json.dumps({
        "name": name, "operator": "Example Co", "redirect_uris": [REDIRECT],
        "allowed_scopes": [READ, WRITE], "webhook_url": "https://app.example.org/hcp-hook"}),
        content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()["client_id"]


@pytest.fixture()
def client_id(admin):
    return _register(admin)


@pytest.fixture()
def other_client_id(admin):
    """A second registered app, so one client can be told apart from another."""
    return _register(admin)


@pytest.fixture()
def bob():
    from django.contrib.auth.models import User

    return User.objects.create_user("bob", "bob@dimagi.com", "pw")


def _refresh(client_id, refresh_token, **extra):
    return Client().post("/api/hcp/oauth/token", {
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token, **extra})


def _add(access, text="Writes through the API."):
    return Client().post("/api/hcp/v1/preferences/add", data=json.dumps({
        "category": "general_preferences", "preference": text, "declarationType": "user-declared",
        "sourceContext": "c"}), content_type="application/json", **bearer_headers(access))


# --- a credential belongs to its client ------------------------------------------------------


def test_a_code_is_redeemable_only_by_the_client_it_was_issued_to(lili, client_id, other_client_id):
    verifier, code = _consent(_as(lili), client_id)
    stolen = _token(other_client_id, verifier, code)
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
    # A misdirected attempt is not a replay: the real client still redeems it.
    assert PersonGrant.objects.get().status == "active"
    assert _token(client_id, verifier, code).status_code == 200


def test_a_refresh_token_is_redeemable_only_by_its_client(lili, client_id, other_client_id):
    tokens = _tokens(_as(lili), client_id)
    r = _refresh(other_client_id, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert PersonGrant.objects.get().status == "active"
    assert _refresh(client_id, tokens["refresh_token"]).status_code == 200


def test_a_client_cannot_revoke_another_clients_token(lili, client_id, other_client_id):
    tokens = _tokens(_as(lili), client_id)
    r = Client().post("/api/hcp/oauth/revoke", {"client_id": other_client_id,
                                                "token": tokens["access_token"]})
    assert r.status_code == 200                      # RFC 7009: never says whether it knew the token
    assert PersonGrant.objects.get().status == "active"
    assert _search(tokens["access_token"]).status_code == 200


def test_an_unknown_or_disabled_client_gets_no_token(lili, client_id, admin):
    verifier, code = _consent(_as(lili), client_id)
    r = _token("hcpc_nobody", verifier, code)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    _as(admin).patch(f"/api/hcp-admin/clients/{client_id}", data=json.dumps({"disabled": True}),
                     content_type="application/json")
    r = _token(client_id, verifier, code)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_the_token_endpoint_takes_only_post(client_id):
    assert Client().get("/api/hcp/oauth/token").status_code == 405
    assert Client().get("/api/hcp/oauth/revoke").status_code == 405


# --- PKCE cannot be downgraded -----------------------------------------------------------------


def test_a_plain_verifier_offered_as_a_challenge_buys_nothing(lili, client_id):
    """The downgrade: label it S256 but send the verifier itself as the "challenge", as a
    plain-PKCE client would. The server hashes the verifier, so it never matches."""
    verifier = secrets.token_urlsafe(32)               # 43 base64url characters
    _, code = _consent(_as(lili), client_id, pkce=(verifier, verifier))
    r = _token(client_id, verifier, code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_malformed_or_wrongly_cased_pkce_is_refused_at_authorize(lili, client_id):
    _, challenge = _pkce()
    for bad in ({"code_challenge": "short"}, {"code_challenge": challenge + "="},
                {"code_challenge_method": "s256"}, {"code_challenge_method": ""}):
        r = _as(lili).get(AUTHORIZE, {**_params(client_id, challenge), **bad})
        assert r.status_code == 302 and "error=invalid_request" in r["Location"], bad


def test_a_token_request_without_a_verifier_is_refused(lili, client_id):
    _, code = _consent(_as(lili), client_id)
    r = Client().post("/api/hcp/oauth/token", {"grant_type": "authorization_code", "client_id": client_id,
                                               "code": code, "redirect_uri": REDIRECT})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


# --- the redirect URI matches exactly ------------------------------------------------------------


@pytest.mark.parametrize("uri", [
    REDIRECT + "/",                                # a trailing slash
    REDIRECT + "?x=1",                             # an added query
    REDIRECT + "x",                                # a longer path
    REDIRECT.replace("https://", "http://"),       # a downgrade
    REDIRECT.replace("app.", "APP."),              # another case
    REDIRECT.replace("app.example.org", "app.example.org.evil.example"),
    "https://app.example.org@evil.example/callback",
    "https://evil.example/callback#https://app.example.org/callback",
    "",
])
def test_a_redirect_uri_must_match_exactly(lili, client_id, uri):
    _, challenge = _pkce()
    r = _as(lili).get(AUTHORIZE, _params(client_id, challenge, redirect_uri=uri))
    assert r.status_code == 400 and "Location" not in r, uri         # shown, never redirected to
    r = _as(lili).post(AUTHORIZE, {**_params(client_id, challenge, redirect_uri=uri), "step": "allow"})
    assert r.status_code == 400 and "Location" not in r, uri


def test_a_disabled_client_is_shown_an_error_not_redirected_to(lili, client_id, admin):
    _as(admin).patch(f"/api/hcp-admin/clients/{client_id}", data=json.dumps({"disabled": True}),
                     content_type="application/json")
    _, challenge = _pkce()
    r = _as(lili).get(AUTHORIZE, _params(client_id, challenge))
    assert r.status_code == 400 and "Location" not in r and b"disabled" in r.content


# --- a token never carries more than the grant ----------------------------------------------------


def test_refreshing_cannot_widen_the_scope(lili, client_id):
    tokens = _tokens(_as(lili), client_id, scope=READ)
    r = _refresh(client_id, tokens["refresh_token"], scope=f"{READ} {WRITE}")
    assert r.status_code == 200 and r.json()["scope"] == READ
    assert _add(r.json()["access_token"]).status_code == 403            # still cannot write


def test_a_read_grant_cannot_write(lili, client_id):
    access = _tokens(_as(lili), client_id, scope=READ)["access_token"]
    r = _add(access)
    assert r.status_code == 403 and r.json()["type"].endswith("scope-denied")
    assert not PersonFact.objects.filter(statement="Writes through the API.").exists()


def test_the_keep_answer_cannot_change_what_was_allowed(lili, client_id):
    """Act 2 carries a nonce, not the request: scope, redirect and expiry posted with it
    are ignored, so the second act cannot widen the first."""
    _, challenge = _pkce()
    p = _params(client_id, challenge, scope=READ)
    c = _as(lili)
    r = c.post(AUTHORIZE, {**p, "step": "allow", "expiry": "1h"})
    nonce = re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()
    r = c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "no", "scope": f"{READ} {WRITE}",
                           "redirect_uri": "https://evil.example/cb", "expiry": "24h"})
    assert r.status_code == 302 and r["Location"].startswith(REDIRECT)
    grant = PersonGrant.objects.get()
    assert grant.scopes == [READ]
    assert grant.expires_at - timezone.now() <= dt.timedelta(hours=1)


def _after_act_one(c, client_id, *, scope=READ):
    """Answer act 1 and return the nonce act 2 must present."""
    _, challenge = _pkce()
    r = c.post(AUTHORIZE, {**_params(client_id, challenge, scope=scope), "step": "allow"})
    assert r.status_code == 200 and b"keep this access" in r.content
    return re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()


def _nothing_was_made(r):
    assert r.status_code == 400 and "Location" not in r             # shown, never redirected to
    assert not PersonGrant.objects.exists()
    from apps.contacts.models import HcpAuthorizationCode
    assert not HcpAuthorizationCode.objects.exists()
    assert not PersonAuditEvent.objects.filter(event_type="grant.issued").exists()


def test_a_client_disabled_between_the_acts_gets_no_grant_and_no_code(lili, client_id, admin):
    c = _as(lili)
    nonce = _after_act_one(c, client_id)
    assert _patch(admin, client_id, {"disabled": True}).status_code == 200
    r = c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"})
    _nothing_was_made(r)
    assert b"disabled" in r.content


def test_scopes_narrowed_between_the_acts_get_no_grant_and_no_code(lili, client_id, admin):
    c = _as(lili)
    nonce = _after_act_one(c, client_id, scope=f"{READ} {WRITE}")
    assert _patch(admin, client_id, {"allowed_scopes": [READ]}).status_code == 200
    r = c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"})
    _nothing_was_made(r)
    assert WRITE.encode() in r.content


def test_a_client_deleted_between_the_acts_gets_no_grant_and_no_code(lili, client_id):
    c = _as(lili)
    nonce = _after_act_one(c, client_id)
    HcpClient.objects.filter(client_id=client_id).delete()
    r = c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"})
    _nothing_was_made(r)
    assert b"not registered" in r.content


def test_a_return_address_removed_between_the_acts_is_not_redirected_to(lili, client_id):
    c = _as(lili)
    nonce = _after_act_one(c, client_id)
    HcpClient.objects.filter(client_id=client_id).update(redirect_uris=["https://other.example/cb"])
    _nothing_was_made(c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"}))


def test_an_unchanged_client_still_gets_its_grant_and_code_at_the_keep_step(lili, client_id, admin):
    """Edits that leave the request valid (a wider allowed list, a rename) change nothing."""
    c = _as(lili)
    nonce = _after_act_one(c, client_id, scope=READ)
    assert _patch(admin, client_id, {"allowed_scopes": [READ]}).status_code == 200
    r = c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"})
    assert r.status_code == 302 and r["Location"].startswith(REDIRECT) and "code=" in r["Location"]
    assert PersonGrant.objects.get().scopes == [READ]
    assert PersonAuditEvent.objects.filter(event_type="grant.issued").count() == 1


def test_the_acts_state_cannot_be_replayed_to_widen_scopes(lili, bob, client_id, admin):
    # A nonce holds what act 1 showed; neither a wider scope posted with act 2 nor
    # another person's session can use it, and a refused keep spends it.
    nonce = _after_act_one(_as(lili), client_id, scope=READ)
    r = _as(bob).post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes", "scope": f"{READ} {WRITE}"})
    assert r.status_code == 400 and not PersonGrant.objects.exists()
    assert _patch(admin, client_id, {"disabled": True}).status_code == 200
    c = _as(lili)
    _nothing_was_made(c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes",
                                          "scope": f"{READ} {WRITE}"}))
    assert _patch(admin, client_id, {"disabled": False}).status_code == 200
    assert c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"}).status_code == 400
    assert not PersonGrant.objects.exists()


def test_the_keep_nonce_works_once(lili, client_id):
    _, challenge = _pkce()
    p = _params(client_id, challenge)
    c = _as(lili)
    r = c.post(AUTHORIZE, {**p, "step": "allow"})
    nonce = re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()
    assert c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"}).status_code == 302
    assert c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "yes"}).status_code == 400
    assert PersonGrant.objects.count() == 1


def test_a_workspace_the_person_was_not_offered_cannot_be_ticked(lili, client_id):
    _, challenge = _pkce()
    p = _params(client_id, challenge)
    c = _as(lili)
    r = c.post(AUTHORIZE, {**p, "step": "allow", "workspace": ["dimagi", "not-theirs"]})
    nonce = re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()
    c.post(AUTHORIZE, {"step": "keep", "nonce": nonce, "keep": "no"})
    sources = PersonGrant.objects.get().restrictions[0]["narrowedTo"]["sources"]
    assert sources == [hcp_oauth.PERSONAL]


# --- revoked and expired grants open nothing ----------------------------------------------------------


def test_a_revoked_grant_cannot_be_redeemed_for_tokens(lili, client_id):
    verifier, code = _consent(_as(lili), client_id)
    hcp_oauth.revoke(PersonGrant.objects.get(), actor=hcp.SYSTEM)
    r = _token(client_id, verifier, code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert b"access_token" not in r.content


def test_an_expired_temporary_grant_stops_its_access_token(lili, client_id):
    access = _tokens(_as(lili), client_id, expiry="1h")["access_token"]
    assert _search(access).status_code == 200
    PersonGrant.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    r = _search(access)
    assert r.status_code == 401 and r.json()["type"].endswith("invalid-token")
    assert PersonGrant.objects.get().status == "expired"
    assert PersonAuditEvent.objects.filter(event_type="grant.expired").exists()


def test_a_temporary_grants_tokens_never_outlive_it(lili, client_id):
    tokens = _tokens(_as(lili), client_id, expiry="1h")
    grant = PersonGrant.objects.get()
    assert tokens["expires_in"] <= 3600
    from apps.contacts.models import HcpToken

    for tok in HcpToken.objects.filter(grant=grant):
        assert tok.expires_at <= grant.expires_at


def test_a_persistent_grant_ends_when_the_person_ends_it(lili, client_id):
    tokens = _tokens(_as(lili), client_id, keep="yes")
    grant = PersonGrant.objects.get()
    assert grant.grant_type == "persistent"
    r = _as(lili).delete(f"/api/hcp/v1/grants/{hcp.entry_urn(grant.grant_id)}")
    assert r.status_code == 200
    assert _search(tokens["access_token"]).status_code == 401
    assert _refresh(client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


# --- a machine is not a person ---------------------------------------------------------------------------


def test_a_session_minted_from_a_token_cannot_answer_the_consent_screen(lili, client_id):
    from apps.common.views_debug import DEBUG_SESSION_MARKER

    c = _as(lili)
    session = c.session
    session[DEBUG_SESSION_MARKER] = True
    session.save()
    _, challenge = _pkce()
    r = c.get(AUTHORIZE, _params(client_id, challenge))
    assert r.status_code == 403 and b"in your browser" in r.content
    r = c.post(AUTHORIZE, {**_params(client_id, challenge), "step": "allow"})
    assert r.status_code == 403
    assert not PersonGrant.objects.exists()


def test_a_personal_access_token_cannot_answer_the_consent_screen(lili, client_id):
    from tests.token_helpers import pat_token

    _, challenge = _pkce()
    r = Client().get(AUTHORIZE, _params(client_id, challenge), **bearer_headers(pat_token(lili)))
    assert r.status_code in (302, 401, 403)
    assert r.status_code != 200
    assert not PersonGrant.objects.exists()


# --- people are separate -----------------------------------------------------------------------------------


def test_one_persons_token_reaches_nothing_of_anothers(lili, bob, client_id):
    mine = _personal(lili, "I prefer short answers.")
    theirs = _personal(bob, "I prefer long answers.")
    access = _tokens(_as(lili), client_id)["access_token"]
    hits = _search(access, query="I prefer answers").json()["entries"]
    assert [h["id"] for h in hits] == [mine]
    r = Client().get(f"/api/hcp/v1/preferences/{theirs}", **bearer_headers(access))
    assert r.status_code == 403                                  # the same as a missing one (3.3.6)
    r = Client().delete(f"/api/hcp/v1/preferences/{theirs}", **bearer_headers(access))
    assert r.status_code == 403
    assert PersonFact.objects.get(entry_id=hcp.parse_entry_id(theirs)).retracted_at is None


def test_two_people_authorizing_one_client_get_separate_grants(lili, bob, client_id):
    a = _tokens(_as(lili), client_id)["access_token"]
    b = _tokens(_as(bob), client_id)["access_token"]
    mine = _personal(lili, "Prefers tea.")
    _personal(bob, "Prefers coffee.")
    assert [h["id"] for h in _search(a, query="Prefers").json()["entries"]] == [mine]
    assert len(Client().get("/api/hcp/v1/grants", **bearer_headers(b)).json()["grants"]) == 1
    # Neither person can revoke the other's grant.
    lili_grant = PersonGrant.objects.get(person__user=lili)
    r = _as(bob).delete(f"/api/hcp/v1/grants/{hcp.entry_urn(lili_grant.grant_id)}")
    assert r.status_code == 403
    assert PersonGrant.objects.get(pk=lili_grant.pk).status == "active"


def test_a_client_sees_only_its_own_grant_when_a_person_has_given_two(lili, client_id, other_client_id):
    a = _tokens(_as(lili), client_id)["access_token"]
    _tokens(_as(lili), other_client_id)
    grants = Client().get("/api/hcp/v1/grants", **bearer_headers(a)).json()["grants"]
    assert [g["client"]["id"] for g in grants] == [client_id]


# --- the internal-only gate ----------------------------------------------------------------------------------


def test_internal_with_no_allowed_domains_authorizes_nobody(lili, client_id, settings):
    """`may_authorize` is defence in depth: an empty domain list must not read as "no
    restriction", or a mis-set AUTH_ALLOWED_EMAIL_DOMAIN would open the service."""
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = ""
    _, challenge = _pkce()
    r = _as(lili).get(AUTHORIZE, _params(client_id, challenge))
    assert r.status_code == 403
    settings.HCP_SERVICE_AUDIENCE = "public"
    assert _as(lili).get(AUTHORIZE, _params(client_id, challenge)).status_code == 200


def test_the_audience_setting_is_read_in_one_place():
    """`HCP_SERVICE_AUDIENCE` is the one switch between internal and public; a second
    reader would be a second gate that can disagree with the first."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    readers = []
    for p in (root / "apps").rglob("*.py"):
        if "HCP_SERVICE_AUDIENCE" in p.read_text(encoding="utf-8"):
            readers.append(p.relative_to(root).as_posix())
    assert readers == ["apps/contacts/hcp_oauth.py"], readers


def test_registering_a_client_has_no_public_route(lili):
    """No self-registration: every write to the registry is a superuser's, and the
    registry is not an MCP tool (an agent must not enrol an app that asks people)."""
    from apps.mcp.api_tools import EXCLUDED as excluded

    for op in ("hcp_listClients", "hcp_registerClient", "hcp_updateClient"):
        assert op in excluded, op
    assert HcpClient.objects.count() == 0


# --- the registry's allowed_scopes (the admin page edits it) ---------------------------------------------


def _patch(who, client_id, body):
    return _as(who).patch(f"/api/hcp-admin/clients/{client_id}", data=json.dumps(body),
                          content_type="application/json")


def test_the_scopes_the_service_publishes_are_exactly_the_ones_it_will_accept(admin, client_id):
    """The admin page offers `scopes_supported` and nothing else; each must be a scope
    the registry accepts, and every scope the registry accepts must be offered."""
    published = Client().get("/api/hcp/.well-known/oauth-authorization-server").json()["scopes_supported"]
    assert published == hcp_oauth.all_scopes()
    assert len(published) == len(hcp.HELD_CATEGORIES) * len(hcp.ACTIONS)
    r = _patch(admin, client_id, {"allowed_scopes": published})
    assert r.status_code == 200 and r.json()["allowed_scopes"] == published


def test_the_pages_category_labels_cover_every_category_the_service_holds():
    """`MY_CATEGORIES` (frontend/src/api/people.ts) labels scopes on the admin page and
    is the person's own category picker; a category added to the service without a
    label there would show as its raw name."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent / "frontend/src/api/people.ts").read_text()
    block = src.split("export const MY_CATEGORIES = [", 1)[1].split("] as const", 1)[0]
    labelled = re.findall(r"value: '(\w+)'", block)
    assert sorted(labelled) == sorted(hcp.HELD_CATEGORIES)


def test_an_admin_sets_allowed_scopes_and_the_service_then_holds_the_client_to_them(admin, lili, client_id):
    r = _patch(admin, client_id, {"allowed_scopes": [READ]})
    assert r.status_code == 200 and r.json()["allowed_scopes"] == [READ]
    _, challenge = _pkce()
    r = _as(lili).get(AUTHORIZE, _params(client_id, challenge, scope=WRITE))
    assert r.status_code == 302 and "error=invalid_scope" in r["Location"]
    assert _as(lili).get(AUTHORIZE, _params(client_id, challenge, scope=READ)).status_code == 200


@pytest.mark.parametrize("scopes", [["hcp:*:read"], ["hcp:general_preferences:*"], ["hcp:health_context:read"],
                                    ["openid"], []])
def test_allowed_scopes_the_service_does_not_hold_are_refused(admin, client_id, scopes):
    r = _patch(admin, client_id, {"allowed_scopes": scopes})
    assert r.status_code == 422, scopes
    assert HcpClient.objects.get(client_id=client_id).allowed_scopes == [READ, WRITE]


def test_only_a_superuser_changes_allowed_scopes(lili, client_id):
    r = _patch(lili, client_id, {"allowed_scopes": [READ]})
    assert r.status_code == 403
    assert HcpClient.objects.get(client_id=client_id).allowed_scopes == [READ, WRITE]


def test_narrowing_allowed_scopes_leaves_what_people_already_gave(admin, lili, client_id):
    """The API says so ("Narrowing allowed_scopes does not change grants people already
    gave"): the grant is the person's, so the registry cannot shrink it."""
    access = _tokens(_as(lili), client_id, scope=f"{READ} {WRITE}")["access_token"]
    assert _patch(admin, client_id, {"allowed_scopes": [READ]}).status_code == 200
    assert PersonGrant.objects.get().scopes == [READ, WRITE]
    assert _add(access).status_code == 201
