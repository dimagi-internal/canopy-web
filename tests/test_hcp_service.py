"""The HCP service: OAuth 2.0 for apps canopy does not operate (apps/contacts/hcp_oauth.py).

Each test pins a gate the spec or the owner sets:

* authorization code + PKCE S256, exact redirect URIs, scopes never beyond the
  client's registration and never wildcards (4.1.1, 4.1.7);
* two acts — allow, then a separate keep-access question; temporary is the
  default, persistence never pre-selected (4.1.4, 4.1.6);
* revocation kills every token at once and owes a signed, retried webhook
  notification (4.2.2, 4.2.3);
* the person's own record/use switches bound every client, whatever it was granted;
* internal only, for now (HCP_SERVICE_AUDIENCE);
* an access token opens /api/hcp/v1/ and nothing else, and never the person's own
  audit log, grants list or export.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.contacts import hcp, hcp_oauth
from apps.contacts import services as contacts
from apps.contacts.models import (HcpRevocationDelivery, HcpToken, Person, PersonAuditEvent,
                                  PersonFact, PersonGrant)
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

REDIRECT = "https://app.example.org/callback"
READ = "hcp:general_preferences:read"
WRITE = "hcp:general_preferences:write"


@pytest.fixture(autouse=True)
def _on(agent_memory_on, settings):
    # Production's login gate ON (the test settings turn it off): every public HCP
    # path, and the bearer-only /api/hcp/v1/, must work through it.
    settings.REQUIRE_AUTH = True
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com,dimagi-ai.com"
    settings.HCP_SERVICE_AUDIENCE = "internal"
    from django.core.cache import cache

    cache.clear()          # the per-client rate counter and pending approvals live here
    yield


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def admin():
    return User.objects.create_superuser("root", "root@dimagi.com", "pw")


@pytest.fixture()
def lili():
    return User.objects.create_user("lili", "lili@dimagi.com", "pw")


@pytest.fixture()
def client_id(admin):
    r = _as(admin).post("/api/hcp-admin/clients", data=json.dumps({
        "name": "Recipe Planner", "operator": "Example Co", "redirect_uris": [REDIRECT],
        "allowed_scopes": [READ, WRITE], "webhook_url": "https://app.example.org/hcp-hook",
    }), content_type="application/json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["webhook_secret"].startswith("hcpwh_")
    return body["client_id"]


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _params(client_id, challenge, scope=READ, **kw):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "scope": scope, "state": "s123", "code_challenge": challenge,
            "code_challenge_method": "S256", **kw}


def _consent(c, client_id, *, scope=READ, keep="no", workspaces=(), expiry="4h"):
    """Walk both acts; return (verifier, code)."""
    verifier, challenge = _pkce()
    p = _params(client_id, challenge, scope=scope)
    r = c.get("/api/hcp/oauth/authorize", p)
    assert r.status_code == 200, r.content
    assert b"Recipe Planner" in r.content and b"Example Co" in r.content
    r = c.post("/api/hcp/oauth/authorize", {**p, "step": "allow", "expiry": expiry,
                                            "workspace": list(workspaces)})
    assert r.status_code == 200 and b"keep this access" in r.content
    nonce = re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()
    r = c.post("/api/hcp/oauth/authorize", {"step": "keep", "nonce": nonce, "keep": keep})
    assert r.status_code == 302, r.content
    q = parse_qs(urlsplit(r["Location"]).query)
    assert q["state"] == ["s123"]
    return verifier, q["code"][0]


def _token(client_id, verifier, code, redirect=REDIRECT):
    return Client().post("/api/hcp/oauth/token", {
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": redirect, "code_verifier": verifier})


def _tokens(c, client_id, **kw):
    verifier, code = _consent(c, client_id, **kw)
    r = _token(client_id, verifier, code)
    assert r.status_code == 200, r.content
    return r.json()


def _bearer(access) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _search(access, query="short answers", categories=(  "general_preferences",)):
    return Client().post("/api/hcp/v1/preferences/search", data=json.dumps({
        "query": query, "categories": list(categories), "purpose": "tailor the plan"}),
        content_type="application/json", **_bearer(access))


def _personal(user, text="I prefer short answers.", category="general_preferences"):
    r = _as(user).post("/api/hcp/v1/preferences/add", data=json.dumps({
        "category": category, "preference": text, "declarationType": "user-declared",
        "sourceContext": "user-input"}), content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()["entry"]["id"]


# --- discovery ---------------------------------------------------------------------


def test_discovery_says_oauth2_and_where_everything_is():
    for path in ("/api/hcp/.well-known/hcp-configuration", "/.well-known/hcp-configuration"):
        doc = Client().get(path).json()
        assert doc["authorization_profile"] == "oauth2"
        assert doc["conformance_level"] == "HCP-v1-Core"
        assert doc["authorization_endpoint"].endswith("/api/hcp/oauth/authorize")
        assert doc["token_endpoint"].endswith("/api/hcp/oauth/token")
        assert doc["revocation_endpoint"].endswith("/api/hcp/oauth/revoke")
        assert doc["canopy"]["service"]["audience"] == "internal"
        assert doc["canopy"]["conformance"]["known_gaps"]
    for path in ("/api/hcp/.well-known/oauth-authorization-server",
                 "/.well-known/oauth-authorization-server/api/hcp"):
        meta = Client().get(path).json()
        assert meta["issuer"].endswith("/api/hcp")
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert READ in meta["scopes_supported"]
    manifest = Client().get("/api/hcp/.well-known/mcp-manifest").json()
    assert manifest["scopeFormat"] == "hcp:{category}:{action}"


# --- the flow ----------------------------------------------------------------------


def test_the_whole_flow_reads_a_personal_entry(lili, client_id):
    entry = _personal(lili)
    tokens = _tokens(_as(lili), client_id)
    assert tokens["hcp_grant_type"] == "temporary" and tokens["token_type"] == "Bearer"
    r = _search(tokens["access_token"])
    assert r.status_code == 200, r.content
    [hit] = r.json()["entries"]
    assert hit["id"] == entry
    assert "capturedBy" not in hit["record"]["provenance"]                       # 4.4.4
    grant = PersonGrant.objects.get(hcp_client__client_id=client_id)
    assert grant.grant_type == "temporary"
    assert dt.timedelta(hours=3, minutes=59) < grant.expires_at - timezone.now() <= dt.timedelta(hours=4)
    issued = PersonAuditEvent.objects.get(event_type="grant.issued", grant_id=grant.grant_id)
    assert "type=temporary" in issued.detail and "modality=oauth2-authorization-code" in issued.detail
    assert "restrictions=" in issued.detail and issued.actor_type == "user"
    read = PersonAuditEvent.objects.filter(event_type="preference.read").last()
    assert read.actor_id == f"client:{client_id}" and read.actor_type == "agent"


def test_keeping_access_is_its_own_act_and_makes_it_persistent(lili, client_id):
    _tokens(_as(lili), client_id, keep="yes")
    grant = PersonGrant.objects.get(hcp_client__client_id=client_id)
    assert grant.grant_type == "persistent" and grant.expires_at is None


def test_the_keep_question_preselects_nothing(lili, client_id):
    verifier, challenge = _pkce()
    p = _params(client_id, challenge)
    c = _as(lili)
    c.get("/api/hcp/oauth/authorize", p)
    r = c.post("/api/hcp/oauth/authorize", {**p, "step": "allow"})
    assert b"checked" not in r.content and b"selected" not in r.content


def test_a_client_writes_a_personal_entry(lili, client_id):
    access = _tokens(_as(lili), client_id, scope=f"{READ} {WRITE}")["access_token"]
    r = Client().post("/api/hcp/v1/preferences/add", data=json.dumps({
        "category": "general_preferences", "preference": "Vegetarian on weekdays.",
        "declarationType": "user-declared", "sourceContext": "conversation-id:abc"}),
        content_type="application/json", **_bearer(access))
    assert r.status_code == 201, r.content
    fact = PersonFact.objects.get(statement="Vegetarian on weekdays.")
    assert fact.workspace_id is None and fact.captured_by == f"client:{client_id}"


def test_a_client_cannot_pass_itself_off_as_canopy(lili, client_id):
    access = _tokens(_as(lili), client_id, scope=f"{READ} {WRITE}")["access_token"]
    r = Client().post("/api/hcp/v1/preferences", data=json.dumps({
        "claim": {"subject": {"preference": "Likes tea."}},
        "record": {"category": "general_preferences", "declarationType": "user-declared",
                   "provenance": {"source": "x", "capturedBy": "agent:ace"}}}),
        content_type="application/json", **_bearer(access))
    assert r.status_code == 201, r.content
    assert PersonFact.objects.get(statement="Likes tea.").captured_by == f"client:{client_id}/agent:ace"


# --- PKCE, redirect, scopes, consent -----------------------------------------------------


def test_pkce_is_required(lili, client_id):
    c = _as(lili)
    _, challenge = _pkce()
    for bad in ({"code_challenge": ""}, {"code_challenge_method": "plain"}):
        r = c.get("/api/hcp/oauth/authorize", {**_params(client_id, challenge), **bad})
        assert r.status_code == 302
        assert "error=invalid_request" in r["Location"]


def test_a_wrong_verifier_gets_no_token(lili, client_id):
    _, code = _consent(_as(lili), client_id)
    r = _token(client_id, secrets.token_urlsafe(48), code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = _token(client_id, "", code)
    assert r.json()["error"] in ("invalid_request", "invalid_grant")


def test_a_redirect_uri_not_registered_is_never_redirected_to(lili, client_id):
    _, challenge = _pkce()
    r = _as(lili).get("/api/hcp/oauth/authorize",
                      _params(client_id, challenge, redirect_uri="https://evil.example/cb"))
    assert r.status_code == 400 and "Location" not in r
    # …and the token request must name the same redirect as the authorization.
    verifier, code = _consent(_as(lili), client_id)
    assert _token(client_id, verifier, code, redirect="https://app.example.org/other").json()[
        "error"] == "invalid_grant"


def test_a_client_cannot_ask_beyond_its_registration_or_with_a_wildcard(lili, client_id):
    _, challenge = _pkce()
    for scope in ("hcp:work_context:read", "hcp:general_preferences:*", "hcp:*:read", "openid"):
        r = _as(lili).get("/api/hcp/oauth/authorize", _params(client_id, challenge, scope=scope))
        assert r.status_code == 302 and "error=invalid_scope" in r["Location"], scope


def test_denying_creates_no_grant(lili, client_id):
    _, challenge = _pkce()
    p = _params(client_id, challenge)
    c = _as(lili)
    c.get("/api/hcp/oauth/authorize", p)
    r = c.post("/api/hcp/oauth/authorize", {**p, "step": "deny"})
    assert r.status_code == 302 and "error=access_denied" in r["Location"]
    assert not PersonGrant.objects.exists()


def test_a_code_works_once(lili, client_id):
    verifier, code = _consent(_as(lili), client_id)
    assert _token(client_id, verifier, code).status_code == 200
    r = _token(client_id, verifier, code)
    assert r.json()["error"] == "invalid_grant"
    assert PersonGrant.objects.get().status == "revoked"        # a replayed code kills the grant


def test_the_keep_answer_cannot_be_completed_by_someone_else(lili, client_id):
    verifier, challenge = _pkce()
    p = _params(client_id, challenge)
    c = _as(lili)
    r = c.post("/api/hcp/oauth/authorize", {**p, "step": "allow"})
    nonce = re.search(rb'name="nonce" value="([^"]+)"', r.content).group(1).decode()
    other = User.objects.create_user("bob", "bob@dimagi.com", "pw")
    r = _as(other).post("/api/hcp/oauth/authorize", {"step": "keep", "nonce": nonce, "keep": "yes"})
    assert r.status_code == 400 and not PersonGrant.objects.exists()


# --- revocation ----------------------------------------------------------------------------


def test_revoking_kills_every_token_and_notifies_the_webhook(lili, client_id, django_capture_on_commit_callbacks):
    tokens = _tokens(_as(lili), client_id)
    grant = PersonGrant.objects.get()
    with django_capture_on_commit_callbacks(execute=False):
        r = _as(lili).delete(f"/api/hcp/v1/grants/{hcp.entry_urn(grant.grant_id)}")
    assert r.status_code == 200
    assert _search(tokens["access_token"]).status_code == 401
    r = Client().post("/api/hcp/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                               "refresh_token": tokens["refresh_token"]})
    assert r.json()["error"] == "invalid_grant"
    assert not HcpToken.objects.filter(grant=grant, revoked_at__isnull=True).exists()

    d = HcpRevocationDelivery.objects.get()
    seen = []

    def post(url, body, headers):
        seen.append((url, body, headers))
        return 204

    hcp_oauth.attempt(d, post=post)
    url, body, headers = seen[0]
    assert url == "https://app.example.org/hcp-hook"
    secret = hcp_oauth.webhook_secret(d.client)
    expected = hmac.new(secret.encode(), f"{headers['HCP-Timestamp']}.".encode() + body,
                        hashlib.sha256).hexdigest()
    assert hmac.compare_digest(headers["HCP-Signature"], expected)
    assert json.loads(body)["grantId"] == hcp.entry_urn(grant.grant_id)
    assert PersonAuditEvent.objects.filter(event_type="revocation.notified").count() == 1


def test_a_failing_webhook_is_retried_then_abandoned(lili, client_id, django_capture_on_commit_callbacks):
    _tokens(_as(lili), client_id)
    with django_capture_on_commit_callbacks(execute=False):
        hcp_oauth.revoke(PersonGrant.objects.get(), actor=hcp.SYSTEM)
    d = HcpRevocationDelivery.objects.get()
    ids, now = set(), timezone.now()
    first = now

    def post(url, body, headers):
        ids.add(headers["HCP-Delivery-Id"])
        return 500

    for _ in range(10):
        d.refresh_from_db()
        if d.abandoned_at:
            break
        hcp_oauth.attempt(d, now=now, post=post)
        d.refresh_from_db()
        now = d.next_attempt_at
    assert d.abandoned_at is not None and d.attempts >= 5
    assert d.abandoned_at - first >= dt.timedelta(hours=1)                         # 4.2.3
    assert len(ids) == d.attempts                                                  # a fresh id each time
    [ev] = PersonAuditEvent.objects.filter(event_type="revocation.notified")
    assert "abandoned" in ev.detail


def test_a_rotated_refresh_token_presented_again_revokes_the_grant(lili, client_id):
    tokens = _tokens(_as(lili), client_id)
    refresh = {"grant_type": "refresh_token", "client_id": client_id,
               "refresh_token": tokens["refresh_token"]}
    r = Client().post("/api/hcp/oauth/token", refresh)
    assert r.status_code == 200 and r.json()["refresh_token"] != tokens["refresh_token"]
    assert Client().post("/api/hcp/oauth/token", refresh).json()["error"] == "invalid_grant"
    assert PersonGrant.objects.get().status == "revoked"


def test_a_client_revoking_its_token_ends_the_grant(lili, client_id):
    tokens = _tokens(_as(lili), client_id)
    r = Client().post("/api/hcp/oauth/revoke", {"client_id": client_id, "token": tokens["access_token"]})
    assert r.status_code == 200
    assert PersonGrant.objects.get().status == "revoked"
    assert _search(tokens["access_token"]).status_code == 401


def test_a_temporary_grant_is_not_refreshed_past_its_expiry(lili, client_id):
    tokens = _tokens(_as(lili), client_id, expiry="1h")
    PersonGrant.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    r = Client().post("/api/hcp/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                               "refresh_token": tokens["refresh_token"]})
    assert r.json()["error"] == "invalid_grant"
    assert PersonGrant.objects.get().status == "expired"
    assert PersonAuditEvent.objects.filter(event_type="grant.expired").exists()


def test_disabling_a_client_stops_its_tokens(admin, lili, client_id):
    access = _tokens(_as(lili), client_id)["access_token"]
    r = _as(admin).patch(f"/api/hcp-admin/clients/{client_id}", data=json.dumps({"disabled": True}),
                         content_type="application/json")
    assert r.status_code == 200
    assert _search(access).status_code == 401


# --- the person's own switches bound every client ----------------------------------------


def test_use_unavailable_refuses_a_read_even_with_a_read_grant(lili, client_id):
    _personal(lili)
    access = _tokens(_as(lili), client_id)["access_token"]
    Person.objects.filter(user=lili).update(hcp_use_available=False)
    r = _search(access)
    assert r.status_code == 403 and r.json()["type"].endswith("scope-denied")


def test_record_off_refuses_a_write(lili, client_id):
    access = _tokens(_as(lili), client_id, scope=f"{READ} {WRITE}")["access_token"]
    Person.objects.filter(user=lili).update(hcp_record_default=False)
    r = Client().post("/api/hcp/v1/preferences/add", data=json.dumps({
        "category": "general_preferences", "preference": "x y z", "declarationType": "user-declared",
        "sourceContext": "c"}), content_type="application/json", **_bearer(access))
    assert r.status_code == 403


def test_the_consent_screen_says_when_a_switch_will_refuse(lili, client_id):
    person = contacts.person_for(user=lili)
    Person.objects.filter(pk=person.pk).update(hcp_use_available=False)
    _, challenge = _pkce()
    r = _as(lili).get("/api/hcp/oauth/authorize", _params(client_id, challenge))
    assert b"which it is NOT now" in r.content


# --- reach: personal, plus workspaces the person ticks --------------------------------------


@pytest.fixture()
def ws_entry(lili):
    owner = User.objects.create_user("own", "own@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    from apps.contacts import people

    people.record_fact(person=contacts.person_for(user=lili), workspace=ws, kind="preference",
                       statement="Prefers short answers in Connect.", category="general_preferences")
    return ws


def test_workspace_entries_are_reached_only_when_ticked(lili, client_id, ws_entry):
    access = _tokens(_as(lili), client_id)["access_token"]
    assert _search(access).json()["entries"] == []
    access = _tokens(_as(lili), client_id, workspaces=["connect"])["access_token"]
    [hit] = _search(access).json()["entries"]
    assert hit["claim"]["subject"]["preference"] == "Prefers short answers in Connect."


# --- what a token opens --------------------------------------------------------------------


def test_a_token_opens_hcp_v1_only_and_never_the_persons_own_routes(lili, client_id):
    access = _tokens(_as(lili), client_id)["access_token"]
    assert Client().get("/api/people/me/", **_bearer(access)).status_code == 401
    assert Client().get("/api/workspaces/", **_bearer(access)).status_code == 401
    for path in ("/api/hcp/v1/audit", "/api/hcp/v1/export"):
        assert Client().get(path, **_bearer(access)).status_code == 403, path
    grants = Client().get("/api/hcp/v1/grants", **_bearer(access)).json()["grants"]
    assert len(grants) == 1 and grants[0]["client"]["id"] == client_id          # its own only (4.1.5)


def test_idempotency_works_for_a_client(lili, client_id):
    access = _tokens(_as(lili), client_id, scope=f"{READ} {WRITE}")["access_token"]
    body = json.dumps({"category": "general_preferences", "preference": "Tea not coffee.",
                       "declarationType": "user-declared", "sourceContext": "c"})
    for _ in range(2):
        r = Client().post("/api/hcp/v1/preferences/add", data=body, content_type="application/json",
                          HTTP_IDEMPOTENCY_KEY="k1", **_bearer(access))
        assert r.status_code == 201
    assert PersonFact.objects.filter(statement="Tea not coffee.").count() == 1


# --- internal only, for now ------------------------------------------------------------------


def test_internal_refuses_an_outside_address(client_id, settings):
    outsider = User.objects.create_user("x", "x@gmail.com", "pw")
    _, challenge = _pkce()
    r = _as(outsider).get("/api/hcp/oauth/authorize", _params(client_id, challenge))
    assert r.status_code == 403 and b"Dimagi accounts only" in r.content
    settings.HCP_SERVICE_AUDIENCE = "public"
    r = _as(outsider).get("/api/hcp/oauth/authorize", _params(client_id, challenge))
    assert r.status_code == 200


def test_anonymous_is_sent_to_sign_in(client_id):
    _, challenge = _pkce()
    r = Client().get("/api/hcp/oauth/authorize", _params(client_id, challenge))
    assert r.status_code == 302 and "login" in r["Location"].lower()


# --- the registry ------------------------------------------------------------------------------


def test_only_a_superuser_registers_a_client(lili):
    r = _as(lili).post("/api/hcp-admin/clients", data=json.dumps({
        "name": "X", "operator": "Y", "redirect_uris": [REDIRECT], "allowed_scopes": [READ]}),
        content_type="application/json")
    assert r.status_code == 403
    assert _as(lili).get("/api/hcp-admin/clients").status_code == 403


def test_registration_refuses_bad_redirects_and_scopes(admin):
    for uris, scopes in (([" http://app.example.org/cb"], [READ]), (["https://a.example/cb#x"], [READ]),
                         ([REDIRECT], ["hcp:*:read"]), ([REDIRECT], ["hcp:health_context:read"])):
        r = _as(admin).post("/api/hcp-admin/clients", data=json.dumps({
            "name": "X", "operator": "Y", "redirect_uris": uris, "allowed_scopes": scopes}),
            content_type="application/json")
        assert r.status_code == 422, (uris, scopes)


def test_a_client_is_rate_limited_with_retry_after(lili, client_id, monkeypatch):
    monkeypatch.setattr(hcp_oauth, "RATE_LIMIT_PER_MINUTE", 2)
    access = _tokens(_as(lili), client_id)["access_token"]
    codes = [_search(access).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    r = _search(access)
    assert r["Retry-After"] and r.json()["type"].endswith("rate-limited")
