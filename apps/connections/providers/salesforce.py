"""Salesforce as a connection provider (OAuth 2.0 web-server flow + PKCE)."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .base import HTTP_TIMEOUT, Identity, ProviderError, TokenResponse

# Salesforce token responses carry no `expires_in`; the org's session policy
# decides (default 2h). Assume 2h and let the caller refresh on a 401.
ACCESS_TOKEN_LIFETIME = timedelta(hours=2)


def _error(resp: requests.Response, what: str) -> ProviderError:
    try:
        body = resp.json()
        detail = body.get("error_description") or body.get("error") or ""
    except ValueError:
        detail = ""
    return ProviderError(f"Salesforce {what} failed ({resp.status_code}): {detail}".rstrip(": "))


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
        resp = requests.post(
            f"{self._base}/services/oauth2/token",
            data={**data, "client_id": settings.SALESFORCE_CLIENT_ID,
                  "client_secret": settings.SALESFORCE_CLIENT_SECRET},
            headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT,
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        # A rejected grant can arrive as HTTP 200 with an `error` key.
        if resp.status_code != 200 or "error" in body or not body.get("access_token"):
            raise _error(resp, what)
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

    def identity(self, token: TokenResponse) -> Identity:
        resp = requests.get(
            token.raw_id, headers={"Authorization": f"Bearer {token.access_token}",
                                   "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            raise _error(resp, "identity lookup")
        body = resp.json()
        org, user = body.get("organization_id", ""), body.get("user_id", "")
        if not org or not user:
            raise ProviderError("Salesforce identity response had no organization_id/user_id")
        return Identity(account_id=f"{org}/{user}", label=body.get("username", ""))

    def revoke(self, token: str) -> None:
        resp = requests.post(
            f"{self._base}/services/oauth2/revoke", data={"token": token}, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            raise _error(resp, "revoke")
