"""Salesforce as a connection provider (OAuth 2.0 web-server flow + PKCE)."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlparse

import requests
from django.conf import settings
from django.utils import timezone

from .base import HTTP_TIMEOUT, Identity, ProviderError, ProviderUnavailable, TokenResponse

# Salesforce token responses carry no `expires_in`; the org's session policy
# decides (default 2h). Assume 2h and let the caller refresh on a 401.
ACCESS_TOKEN_LIFETIME = timedelta(hours=2)

# Where an identity URL may point. The URL comes back in the token response and
# the access token is sent to it as a bearer, so it is checked rather than trusted.
SALESFORCE_HOST_SUFFIXES = (".salesforce.com", ".force.com")


def _call(method, url: str, what: str, **kw) -> requests.Response:
    """One outbound call, with every transport failure turned into ProviderUnavailable."""
    try:
        return method(url, timeout=HTTP_TIMEOUT, **kw)
    except requests.RequestException as e:
        raise ProviderUnavailable(f"Salesforce {what} failed: {type(e).__name__}") from None


def _json(resp: requests.Response, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise ProviderUnavailable(f"Salesforce {what} returned a non-JSON body ({resp.status_code})") from None
    if not isinstance(body, dict):
        raise ProviderUnavailable(f"Salesforce {what} returned an unexpected body ({resp.status_code})")
    return body


def _error(resp: requests.Response, what: str) -> ProviderError:
    """A failed response as an exception: a 5xx or a non-JSON body is the provider
    being unavailable; anything else is a rejection, carrying Salesforce's code."""
    if resp.status_code >= 500:
        return ProviderUnavailable(f"Salesforce {what} failed ({resp.status_code})")
    try:
        body = _json(resp, what)
    except ProviderUnavailable as e:
        return e
    code = str(body.get("error") or "")
    detail = body.get("error_description") or code
    return ProviderError(f"Salesforce {what} failed ({resp.status_code}): {detail}".rstrip(": "), code=code)


class SalesforceProvider:
    name = "salesforce"
    title = "Salesforce"
    scopes = ("api", "refresh_token")
    rotates_refresh_token = True

    @property
    def _base(self) -> str:
        return (settings.SALESFORCE_LOGIN_URL or "https://login.salesforce.com").rstrip("/")

    def is_configured(self) -> bool:
        return bool(settings.SALESFORCE_CLIENT_ID and settings.SALESFORCE_CLIENT_SECRET)

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        query = urlencode({
            "response_type": "code",
            "client_id": settings.SALESFORCE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        return f"{self._base}/services/oauth2/authorize?{query}"

    def _token(self, data: dict, what: str) -> dict:
        resp = _call(
            requests.post, f"{self._base}/services/oauth2/token", what,
            data={**data, "client_id": settings.SALESFORCE_CLIENT_ID,
                  "client_secret": settings.SALESFORCE_CLIENT_SECRET},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise _error(resp, what)
        body = _json(resp, what)
        # A rejected grant can arrive as HTTP 200 with an `error` key.
        if "error" in body:
            raise _error(resp, what)
        if not body.get("access_token"):
            raise ProviderError(f"Salesforce {what} returned no access token")
        return body

    @staticmethod
    def _response(body: dict, fallback_refresh: str = "") -> TokenResponse:
        return TokenResponse(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token") or fallback_refresh,
            expires_at=timezone.now() + ACCESS_TOKEN_LIFETIME,
            instance_url=body.get("instance_url", ""),
            raw_id=body.get("id", ""),
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> TokenResponse:
        body = self._token({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": code_verifier,
        }, "code exchange")
        return self._response(body)

    def refresh(self, refresh_token: str) -> TokenResponse:
        body = self._token({"grant_type": "refresh_token", "refresh_token": refresh_token},
                           "refresh")
        # Salesforce may or may not rotate; callers store a new one when present.
        return self._response(body)

    def _identity_host_ok(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            return False
        if host.endswith(SALESFORCE_HOST_SUFFIXES):
            return True
        return host == (urlparse(self._base).hostname or "").lower()

    def identity(self, token: TokenResponse) -> Identity:
        # The bearer token goes only to a Salesforce host (or the configured login
        # host, for a My Domain / custom login URL), never wherever `id` points.
        if not self._identity_host_ok(token.raw_id):
            raise ProviderError("Salesforce identity URL is not a Salesforce host")
        resp = _call(
            requests.get, token.raw_id, "identity lookup",
            headers={"Authorization": f"Bearer {token.access_token}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise _error(resp, "identity lookup")
        body = _json(resp, "identity lookup")
        org, user = body.get("organization_id", ""), body.get("user_id", "")
        if not org or not user:
            raise ProviderError("Salesforce identity response had no organization_id/user_id")
        return Identity(account_id=f"{org}/{user}", label=body.get("username", ""))

    def revoke(self, token: str) -> None:
        resp = _call(requests.post, f"{self._base}/services/oauth2/revoke", "revoke",
                     data={"token": token})
        if resp.status_code != 200:
            raise _error(resp, "revoke")
