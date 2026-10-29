"""The provider contract: one OAuth 2.0 authorization-code provider (with PKCE).

`Provider` implements everything the OAuth spec makes common — the authorize
URL, the code exchange, refresh, revocation, error parsing, the timeout on
every call — from a handful of class attributes. A provider module subclasses
it, fills in those attributes, implements `identity()` and overrides only
what its service does differently (see `salesforce.py`).

What a provider declares, and where providers differ:

- `name` (the URL and database key), `title` (what the UI calls it), `scopes`.
- `settings_prefix`: the provider's settings are `<PREFIX>_CLIENT_ID`,
  `<PREFIX>_CLIENT_SECRET` and `<PREFIX>_ACCESS_TOKEN_SECONDS`, plus any of its
  own (`SALESFORCE_LOGIN_URL`). Without a client id and secret it is "not set
  up" (`is_configured`), a state the UI shows rather than an error.
- Endpoints: `authorize_url_base()`, `token_url()`, `revoke_url()` ("" when the
  provider has no revocation endpoint).
- `authorize_params(owner_kind=…, owner=…)`: extra authorize-URL parameters,
  given whose connection it will be (the `User` or the `Agent`). Google:
  `access_type=offline`, `prompt=consent`, and an agent's `login_hint`;
  Salesforce, for an agent: `prompt=login consent`.
- `required_scopes`: scopes a grant must include to be stored; a consent screen
  that let the person untick one is refused (`insufficient_scope`). The granted
  scopes are read from the token response's `scope`.
- `client_auth`: how the client authenticates to the token and revocation
  endpoints, `"body"` (client_secret_post, the default) or `"basic"` (HTTP Basic).
- Expiry: a token response's `expires_in` when it sends one; otherwise
  `<PREFIX>_ACCESS_TOKEN_SECONDS`, else `default_access_token_seconds`.
- `rotates_refresh_token`: whether a refresh may hand back a NEW refresh token
  (Salesforce, Intacct) that replaces the old one. The service stores a
  returned refresh token either way; the flag documents the provider and is
  what the tests pin.
- `instance_url(body)`: the account's own API base, when the provider has one
  per account (Salesforce's `instance_url`); "" when the API host is fixed.
- `identity(token)`: who the account is — a stable `account_id` and a label.
- `pkce`: whether the provider takes a PKCE challenge (default yes).

Revocation follows RFC 7009: the token, `token_type_hint=refresh_token`, and the
client's authentication.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

# Every outbound provider call gets one, because a hung request blocks a worker.
HTTP_TIMEOUT = 15


class UnknownProvider(KeyError):  # noqa: N818 - name fixed by the interface
    """No provider registered under that name."""


class ProviderError(Exception):
    """The provider rejected a request (HTTP error, or HTTP 200 carrying `error`).

    `code` is the provider's own error string when it sent one (`invalid_grant`).
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ProviderUnavailable(ProviderError):  # noqa: N818 - a kind of ProviderError
    """The provider could not be reached or did not answer sensibly: a timeout, a
    dropped connection, a 5xx, a body that is not JSON.

    Distinct from a rejection because the remedy differs: this says nothing about
    the stored grant, so a caller must not treat it as "reconnect".
    """


@dataclass(frozen=True)
class TokenResponse:
    # Secrets stay out of repr, so a stray log of this object never prints them.
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)  # "" when the provider did not return one
    expires_at: datetime
    instance_url: str
    # What the provider says it granted (its `scope`), sorted; empty when it did not say.
    scopes: tuple[str, ...] = ()
    # How long this token lives, as the provider said (or the configured fallback).
    lifetime: timedelta = timedelta(0)
    # The provider's whole answer, for `identity()` (Salesforce reads its `id` URL).
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Identity:
    account_id: str  # stable: the same account always has the same id
    label: str  # display only, never used for authorization


class Provider:
    """One OAuth 2.0 authorization-code provider. Subclass per service."""

    name: ClassVar[str]
    title: ClassVar[str]
    settings_prefix: ClassVar[str]
    scopes: ClassVar[tuple[str, ...]]
    required_scopes: ClassVar[tuple[str, ...]] = ()
    client_auth: ClassVar[Literal["body", "basic"]] = "body"
    rotates_refresh_token: ClassVar[bool] = False
    pkce: ClassVar[bool] = True
    default_access_token_seconds: ClassVar[int] = 3600

    # ---- what a provider module fills in ------------------------------------

    def authorize_url_base(self) -> str:
        raise NotImplementedError

    def token_url(self) -> str:
        raise NotImplementedError

    def revoke_url(self) -> str:
        return ""

    def authorize_params(self, *, owner_kind: Literal["user", "agent"], owner: Any) -> Mapping[str, str]:
        """`owner` is the `User` or `Agent` the connection will belong to."""
        return {}

    def instance_url(self, body: Mapping[str, Any]) -> str:
        return ""

    def identity(self, token: TokenResponse) -> Identity:
        raise NotImplementedError

    # ---- settings -----------------------------------------------------------

    def setting(self, key: str, default: Any = "") -> Any:
        """`<PREFIX>_<KEY>` from Django settings."""
        return getattr(settings, f"{self.settings_prefix}_{key}", default)

    @property
    def client_id(self) -> str:
        return self.setting("CLIENT_ID")

    @property
    def client_secret(self) -> str:
        return self.setting("CLIENT_SECRET")

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def access_token_lifetime(self) -> timedelta:
        """How long a fresh access token lives when the token response does not
        say (`expires_in`): `<PREFIX>_ACCESS_TOKEN_SECONDS`, else the default."""
        seconds = self.setting("ACCESS_TOKEN_SECONDS", None) or self.default_access_token_seconds
        return timedelta(seconds=int(seconds))

    # ---- the OAuth flow -----------------------------------------------------

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str, *,
                      owner_kind: Literal["user", "agent"] = "user", owner: Any = None) -> str:
        query = {
            **self.authorize_params(owner_kind=owner_kind, owner=owner),
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.scopes),
        }
        if self.pkce:
            query |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        return f"{self.authorize_url_base()}?{urlencode(query)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> TokenResponse:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if self.pkce:
            data["code_verifier"] = code_verifier
        return self._token_response(self._token(data, "code exchange"))

    def refresh(self, refresh_token: str) -> TokenResponse:
        """A new access token. `refresh_token` on the answer is "" unless the
        provider rotated it; the caller keeps the old one then."""
        body = self._token({"grant_type": "refresh_token", "refresh_token": refresh_token}, "refresh")
        return self._token_response(body)

    def missing_scopes(self, granted: tuple[str, ...]) -> tuple[str, ...]:
        """The required scopes a grant lacks. An empty `granted` means the provider
        did not say, which is taken as what was asked for."""
        if not granted:
            return ()
        return tuple(s for s in self.required_scopes if s not in granted)

    def revoke(self, token: str) -> None:
        """Revoke a refresh token (RFC 7009), with the client's authentication."""
        url = self.revoke_url()
        if not url:
            return
        data, auth = self._authenticated({"token": token, "token_type_hint": "refresh_token"})
        resp = self._call(requests.post, url, "revoke", data=data, **auth)
        if resp.status_code != 200:
            raise self._error(resp, "revoke")

    # ---- shared plumbing ----------------------------------------------------

    def _token_response(self, body: Mapping[str, Any]) -> TokenResponse:
        try:
            lifetime = timedelta(seconds=int(body["expires_in"]))
        except (KeyError, TypeError, ValueError):
            lifetime = self.access_token_lifetime()
        return TokenResponse(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token") or "",
            expires_at=timezone.now() + lifetime,
            instance_url=self.instance_url(body),
            scopes=tuple(sorted(set(str(body.get("scope") or "").split()))),
            lifetime=lifetime,
            raw=body,
        )

    def _authenticated(self, data: dict) -> tuple[dict, dict]:
        """`data` plus the client's credentials, as `client_auth` says: in the body,
        or as HTTP Basic (then `requests`' `auth=`)."""
        if self.client_auth == "basic":
            return data, {"auth": (self.client_id, self.client_secret)}
        return {**data, "client_id": self.client_id, "client_secret": self.client_secret}, {}

    def _token(self, data: dict, what: str) -> dict:
        data, auth = self._authenticated(data)
        resp = self._call(requests.post, self.token_url(), what, data=data,
                          headers={"Accept": "application/json"}, **auth)
        if resp.status_code != 200:
            raise self._error(resp, what)
        body = self._json(resp, what)
        # A rejected grant can arrive as HTTP 200 with an `error` key.
        if "error" in body:
            raise self._error(resp, what)
        if not body.get("access_token"):
            raise ProviderError(f"{self.title} {what} returned no access token")
        return body

    def _call(self, method, url: str, what: str, **kw) -> requests.Response:
        """One outbound call, with every transport failure turned into ProviderUnavailable."""
        try:
            return method(url, timeout=HTTP_TIMEOUT, **kw)
        except requests.RequestException as e:
            raise ProviderUnavailable(f"{self.title} {what} failed: {type(e).__name__}") from None

    def _json(self, resp: requests.Response, what: str) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise ProviderUnavailable(
                f"{self.title} {what} returned a non-JSON body ({resp.status_code})") from None
        if not isinstance(body, dict):
            raise ProviderUnavailable(f"{self.title} {what} returned an unexpected body ({resp.status_code})")
        return body

    def _error(self, resp: requests.Response, what: str) -> ProviderError:
        """A failed response as an exception: a 5xx or a non-JSON body is the provider
        being unavailable; anything else is a rejection, carrying the provider's code."""
        if resp.status_code >= 500:
            return ProviderUnavailable(f"{self.title} {what} failed ({resp.status_code})")
        try:
            body = self._json(resp, what)
        except ProviderUnavailable as e:
            return e
        code = str(body.get("error") or "")
        detail = body.get("error_description") or code
        return ProviderError(f"{self.title} {what} failed ({resp.status_code}): {detail}".rstrip(": "),
                             code=code)
