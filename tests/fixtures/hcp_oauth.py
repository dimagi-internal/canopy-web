"""Plain helpers for the HCP service's OAuth tests: walk both consent acts, exchange a code,
call the API with a token. Shared by `test_hcp_service.py` and `test_hcp_oauth_security.py`;
the fixtures that use them (`admin`, `lili`, `client_id`) stay in each suite.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from django.test import Client

from tests.token_helpers import bearer_headers

REDIRECT = "https://app.example.org/callback"
READ = "hcp:general_preferences:read"
WRITE = "hcp:general_preferences:write"


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _params(client_id, challenge, scope=READ, **kw):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "scope": scope, "state": "s123", "code_challenge": challenge,
            "code_challenge_method": "S256", **kw}


def _consent(c, client_id, *, scope=READ, keep="no", workspaces=(), expiry="4h", pkce=None):
    """Walk both acts; return (verifier, code)."""
    verifier, challenge = pkce or _pkce()
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


def _search(access, query="short answers", categories=(  "general_preferences",)):
    return Client().post("/api/hcp/v1/preferences/search", data=json.dumps({
        "query": query, "categories": list(categories), "purpose": "tailor the plan"}),
        content_type="application/json", **bearer_headers(access))


def _personal(user, text="I prefer short answers.", category="general_preferences"):
    r = _as(user).post("/api/hcp/v1/preferences/add", data=json.dumps({
        "category": category, "preference": text, "declarationType": "user-declared",
        "sourceContext": "user-input"}), content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()["entry"]["id"]
