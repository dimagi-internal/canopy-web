"""Signing in to canopy's MCP server with OAuth — what MCP clients expect.

Claude Code, Claude Desktop and every other MCP client connect to an
OAuth-protected server the same way (MCP authorization spec): read the
server's protected-resource metadata, find its authorization server, register
themselves (RFC 7591), send the person's browser to the authorization endpoint,
and exchange the code (PKCE, RFC 7636) for tokens. This module is that
authorization server, for PEOPLE. It sits beside the jwt-bearer grant canopy
already serves to itself for embedded pages (`self_host.py`) and shares its
issuer, its metadata documents and its token endpoint.

**What a login yields is an ordinary personal access token.** The access token
is a `PersonalToken` that lives an hour, so `CanopyPATVerifier` and
`BearerTokenAuthMiddleware` accept it unchanged and every tool and route
behaves exactly as it does for a hand-minted token — there is no second
identity model to keep in step. Its `oauth_grant` ties it to the approval that
minted it.

**Registration is open; nothing is granted until a person approves.** A client
is public (no secret) and proves possession with PKCE, so registering buys it
only a name on the consent page and a pinned set of redirect URIs.

**Refresh tokens rotate.** Each use returns a new one and retires the old; the
retired one presented again means two parties hold the grant, and the grant is
revoked — the RFC 6819 / OAuth 2.1 reuse detection.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.utils import timezone

from apps.common import oauth_core
from apps.common.oauth_core import OAuthError

from .models import OAuthAuthorizationCode, OAuthClient, OAuthGrant, PersonalToken, hash_secret

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)

AUTHORIZATION_CODE = "authorization_code"
REFRESH_TOKEN = "refresh_token"

#: The scope a login grants: the person, as themselves — everything they can do
#: in the app. Any requested scope is recorded, and this is what is granted.
USER_SCOPE = "canopy"

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*$")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
#: Schemes a redirect may never use — each either runs in the page or reads the machine.
_FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob"}


# --- endpoints and metadata ---------------------------------------------------

def authorization_endpoint(issuer: str) -> str:
    return f"{issuer}/oauth/authorize"


def registration_endpoint(issuer: str) -> str:
    return f"{issuer}/oauth/register"


def token_endpoint(issuer: str) -> str:
    return f"{issuer}/oauth/token"


def as_metadata_fields(issuer: str) -> dict:
    """The RFC 8414 fields a person's login adds to the issuer's document."""
    return {
        "issuer": issuer,
        "authorization_endpoint": authorization_endpoint(issuer),
        "token_endpoint": token_endpoint(issuer),
        "registration_endpoint": registration_endpoint(issuer),
        "response_types_supported": ["code"],
        "grant_types_supported": [AUTHORIZATION_CODE, REFRESH_TOKEN],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [USER_SCOPE],
    }


def merge_metadata(base: dict, extra: dict) -> dict:
    """`base` with `extra` folded in: lists are unioned (order kept), scalars
    fill only what `base` does not already say."""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, list):
            have = list(out.get(key) or [])
            out[key] = have + [v for v in value if v not in have]
        else:
            out.setdefault(key, value)
    return out


# --- registration (RFC 7591) --------------------------------------------------

def redirect_uri_allowed(uri: str) -> bool:
    """https anywhere, http only to loopback (RFC 8252 §7.3), or a private-use
    scheme (`cursor://…`, RFC 8252 §7.1) — never a scheme that runs code."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return False
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if parts.fragment or not scheme:
        return False
    if scheme == "https":
        return bool(parts.netloc)
    if scheme == "http":
        return parts.hostname in _LOOPBACK_HOSTS
    return bool(_SCHEME.match(scheme)) and scheme not in _FORBIDDEN_SCHEMES


def register_client(metadata: dict) -> OAuthClient:
    if not isinstance(metadata, dict):
        raise OAuthError("invalid_client_metadata", "The body must be a JSON object.")
    uris = metadata.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10:
        raise OAuthError("invalid_redirect_uri", "redirect_uris must list 1 to 10 URIs.")
    for uri in uris:
        if not redirect_uri_allowed(uri):
            raise OAuthError("invalid_redirect_uri",
                             f"{uri!r} is not allowed: use https, http to localhost, "
                             "or an app's private-use scheme.")
    grant_types = metadata.get("grant_types") or [AUTHORIZATION_CODE]
    if not set(grant_types) <= {AUTHORIZATION_CODE, REFRESH_TOKEN}:
        raise OAuthError("invalid_client_metadata",
                         "Only the authorization_code and refresh_token grants are served.")
    name = str(metadata.get("client_name") or "")[:200]
    return OAuthClient.objects.create(
        client_id=f"mcp_{secrets.token_urlsafe(24)}", client_name=name, redirect_uris=list(uris),
    )


def client_registration_response(client: OAuthClient) -> dict:
    return {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": [AUTHORIZATION_CODE, REFRESH_TOKEN],
        "response_types": ["code"],
        # A public client: it holds no secret and proves itself with PKCE.
        "token_endpoint_auth_method": "none",
    }


# --- authorization ------------------------------------------------------------

def _redirect_matches(registered: str, presented: str) -> bool:
    """Exact match — except a loopback redirect, whose PORT the client picks at
    run time (RFC 8252 §7.3), so only scheme, host and path must agree."""
    if registered == presented:
        return True
    a, b = urlsplit(registered), urlsplit(presented)
    return (a.scheme == b.scheme == "http" and a.hostname in _LOOPBACK_HOSTS
            and a.hostname == b.hostname and a.path == b.path and a.query == b.query)


@dataclass
class AuthorizeRequest:
    client: OAuthClient
    redirect_uri: str
    state: str
    code_challenge: str
    scope: str
    #: Whether the request itself named `redirect_uri` (it may be left out when
    #: the client registered exactly one). If it did, the token request must
    #: name it again (RFC 6749 §4.1.3).
    redirect_uri_presented: bool = True

    def redirect(self, **params) -> str:
        """`redirect_uri` with `params` (and the client's `state`) added to its query."""
        if self.state:
            params["state"] = self.state
        parts = urlsplit(self.redirect_uri)
        query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


class AuthorizeError(OAuthError):
    """An authorization error to send back to the client's (verified) redirect."""

    def __init__(self, request: AuthorizeRequest, code: str, description: str):
        super().__init__(code, description)
        self.request = request

    def redirect(self) -> str:
        return self.request.redirect(error=self.code, error_description=self.description)


class InvalidClientOrRedirect(Exception):
    """An authorize request that must NOT be redirected: the client or its
    redirect URI could not be trusted, so the error is shown to the person."""


def parse_authorize(params, *, resource: str) -> AuthorizeRequest:
    """Validate an authorization request. Raises `InvalidClientOrRedirect` when
    the client or redirect cannot be trusted (show it to the person), and
    `AuthorizeError` for everything after that (send it back to the client)."""
    client = OAuthClient.objects.filter(client_id=params.get("client_id") or "").first()
    if client is None:
        raise InvalidClientOrRedirect("This client is not registered with canopy.")
    redirect_uri = params.get("redirect_uri") or ""
    presented = bool(redirect_uri)
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
    if not any(_redirect_matches(r, redirect_uri) for r in client.redirect_uris):
        raise InvalidClientOrRedirect("The redirect address is not one this client registered.")
    req = AuthorizeRequest(client=client, redirect_uri=redirect_uri,
                           state=params.get("state") or "",
                           code_challenge=params.get("code_challenge") or "",
                           scope=(params.get("scope") or "")[:500],
                           redirect_uri_presented=presented)
    if params.get("response_type") != "code":
        raise AuthorizeError(req, "unsupported_response_type", "Only response_type=code is served.")
    pkce_problem = oauth_core.check_challenge(req.code_challenge, params.get("code_challenge_method") or "")
    if pkce_problem:
        raise AuthorizeError(req, "invalid_request", pkce_problem)
    target = params.get("resource")
    if target and target.rstrip("/") != resource.rstrip("/"):
        raise AuthorizeError(req, "invalid_target", f"This server issues tokens for {resource} only.")
    return req


def issue_code(req: AuthorizeRequest, user) -> str:
    raw = secrets.token_urlsafe(32)
    OAuthAuthorizationCode.objects.create(
        code_hash=hash_secret(raw), client=req.client, user=user,
        # What the token request is held to: "" when the request named none.
        redirect_uri=req.redirect_uri if req.redirect_uri_presented else "",
        code_challenge=req.code_challenge, scope=req.scope,
        expires_at=timezone.now() + CODE_TTL,
    )
    return raw


# --- the token endpoint -------------------------------------------------------

def _mint(grant: OAuthGrant) -> dict:
    """A fresh access token and a rotated refresh token for `grant`."""
    access_raw, _ = PersonalToken.create_for_user(
        user=grant.user, label=f"MCP: {grant.client.display_name}"[:200],
        ttl=ACCESS_TOKEN_TTL, oauth_grant=grant,
    )
    refresh_raw = secrets.token_urlsafe(32)
    now = timezone.now()
    grant.previous_refresh_hash = grant.refresh_token_hash
    grant.refresh_token_hash = hash_secret(refresh_raw)
    grant.refresh_expires_at = now + REFRESH_TOKEN_TTL
    grant.last_used_at = now
    grant.save(update_fields=["previous_refresh_hash", "refresh_token_hash",
                              "refresh_expires_at", "last_used_at"])
    return {
        "access_token": access_raw,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "refresh_token": refresh_raw,
        "scope": USER_SCOPE,
    }


def _client(form) -> OAuthClient:
    client = OAuthClient.objects.filter(client_id=form.get("client_id") or "").first()
    if client is None:
        raise OAuthError("invalid_client", "Unknown client_id.", status=401)
    return client


def exchange_code(form) -> dict:
    client = _client(form)

    def use(code: OAuthAuthorizationCode) -> dict:
        oauth_core.check_redirect_uri(form.get("redirect_uri") or "", code.redirect_uri)
        oauth_core.verify_pkce(form.get("code_verifier") or "", code.code_challenge)
        if not code.user.is_active:
            raise OAuthError("invalid_grant", "The account that approved this is inactive.")
        grant = OAuthGrant.objects.create(client=client, user=code.user, scope=code.scope)
        code.used_at = timezone.now()
        code.grant = grant
        code.save(update_fields=["used_at", "grant"])
        return _mint(grant)

    def revoke(code: OAuthAuthorizationCode) -> None:
        if code.grant_id is not None:
            _revoke(OAuthGrant.objects.get(pk=code.grant_id))

    return oauth_core.redeem_once(
        form.get("code") or "", client_pk=client.pk, what="authorization code",
        find=lambda raw: OAuthAuthorizationCode.objects.filter(code_hash=hash_secret(raw)).first(),
        is_spent=lambda code: code.used_at is not None,
        expires_at=lambda code: code.expires_at, use=use, revoke=revoke)


def _revoke(grant: OAuthGrant) -> None:
    if grant.revoked_at is None:
        grant.revoke()


def refresh(form) -> dict:
    client = _client(form)
    presented = hash_secret(form.get("refresh_token") or "")

    def find(raw: str) -> OAuthGrant | None:
        # The grant's current refresh token — or the one it replaced, presented again.
        return (OAuthGrant.objects.filter(refresh_token_hash=presented).first()
                or OAuthGrant.objects.filter(previous_refresh_hash=presented).first())

    def use(grant: OAuthGrant) -> dict:
        if grant.revoked_at is not None or not grant.user.is_active:
            raise OAuthError("invalid_grant", "This connection was revoked.")
        # The previous access token is superseded; revoke it rather than let
        # hourly refreshes pile up live tokens.
        grant.access_tokens.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        return _mint(grant)  # rotates: the presented token becomes the previous one

    return oauth_core.redeem_once(
        form.get("refresh_token") or "", client_pk=client.pk, what="refresh token",
        find=find, is_spent=lambda grant: grant.refresh_token_hash != presented,
        expires_at=lambda grant: grant.refresh_expires_at, use=use, revoke=_revoke)


def token_request(form) -> dict:
    grant_type = form.get("grant_type")
    if grant_type == AUTHORIZATION_CODE:
        return exchange_code(form)
    if grant_type == REFRESH_TOKEN:
        return refresh(form)
    raise OAuthError("unsupported_grant_type", f"grant_type {grant_type!r} is not served.")


# --- connected apps -------------------------------------------------------------

def grants_for(user):
    return (OAuthGrant.objects.filter(user=user, revoked_at__isnull=True)
            .select_related("client").order_by("-created_at"))
