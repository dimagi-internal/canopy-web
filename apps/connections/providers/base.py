"""The provider contract: one OAuth 2.0 authorization-code provider (with PKCE).

`Provider` implements everything the OAuth spec makes common — the authorize
URL, the code exchange, refresh, revocation, error parsing, the timeout on
every call — from a handful of class attributes. A provider module subclasses
it, fills in those attributes, implements `identity()` and overrides only
what its service does differently (see `salesforce.py`).

A provider is a class; an instance is that provider bound to one workspace's
`ConnectionApp` (`providers.for_app(app)`), which supplies the client id, the
client secret, the provider's options and the extra scopes the workspace chose.
Nothing is read from Django settings.

What a provider declares, and where providers differ:

- `name` (the URL and database key), `title` (what the UI calls it), `scopes`
  (always asked for).
- `scope_choices`: extra scopes a workspace may add, scope -> human label
  ("Read mail"). Chosen ones are asked for and required on top of `scopes`.
- `option_fields` and `clean_options(raw)`: the provider's own settings
  (Salesforce: its login host and the access-token lifetime), validated when an
  admin saves the app. `locked_options` names the ones that identify the
  authorization server (Salesforce: `login_url`), locked like the client id
  while connections exist. Without a client id and secret the app is "not set up"
  (`is_configured`), a state the UI shows rather than an error.
- Endpoints: `authorize_url_base()`, `token_url()`, `revoke_url()` ("" when the
  provider has no revocation endpoint).
- `authorize_params(owner_kind=…, owner=…)`: extra authorize-URL parameters,
  given whose connection it will be (the `User` or the `Agent`). Google:
  `access_type=offline`, `prompt=consent`, and an agent's `login_hint`;
  Salesforce, for an agent: `prompt=login consent`.
- `required_scopes`: scopes a grant must include to be stored; a consent screen
  that let the person untick one is refused (`insufficient_scope`). The granted
  scopes are read from the token response's `scope`; `scope_implies` names a
  broader scope that satisfies narrower ones (Salesforce: `full` gives `api`).
- `check_owner(identity, owner_kind=…, owner=…)`: refuse an account that is not
  the one this owner must connect (raise `ProviderError(code="wrong_account")`);
  by default any account is accepted.
- `client_auth`: how the client authenticates to the token and revocation
  endpoints, `"body"` (client_secret_post, the default) or `"basic"` (HTTP Basic).
- Expiry: a token response's `expires_in` when it sends one; otherwise the
  app's `access_token_seconds` option, else `default_access_token_seconds`.
- `rotates_refresh_token`: whether a refresh may hand back a NEW refresh token
  (Salesforce, Intacct) that replaces the old one. The service stores a
  returned refresh token either way; the flag documents the provider and is
  what the tests pin.
- `instance_url(body)`: the account's own API base, when the provider has one
  per account (Salesforce's `instance_url`); "" when the API host is fixed.
- `identity(token)`: who the account is — a stable `account_id`, a label, and
  the verified `email` when the provider vouches for one (what `check_owner`
  compares; the label is display only).
- `pkce`: whether the provider takes a PKCE challenge (default yes).

Revocation follows RFC 7009: the token, its `token_type_hint` (a refresh token,
or the access token of a grant refused before it had one), and the client's
authentication.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlencode, urlparse

import requests
from django.utils import timezone

if TYPE_CHECKING:
    from ..models import ConnectionApp

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
    # The account's email when the provider vouches it is verified (Google), lowercased;
    # "" otherwise. What `check_owner` may authorize on, unlike `label`.
    email: str = ""
    # The organisation domain the provider vouches the account belongs to (Google's
    # `hd`: a Workspace account's domain; "" for a consumer account), lowercased.
    hosted_domain: str = ""


@dataclass(frozen=True)
class OptionField:
    """One provider option a workspace admin sets on its app."""

    name: str
    label: str
    kind: Literal["url", "int", "text"]
    required: bool = False
    default: Any = None
    help: str = ""  # one or two sentences the form shows under the field


class Provider:
    """One OAuth 2.0 authorization-code provider. Subclass per service; an
    instance is bound to one workspace's app (or to none: "not set up")."""

    name: ClassVar[str]
    title: ClassVar[str]
    scopes: ClassVar[tuple[str, ...]]
    required_scopes: ClassVar[tuple[str, ...]] = ()
    # Extra scopes a workspace may choose, scope -> what to call it in the UI.
    scope_choices: ClassVar[Mapping[str, str]] = {}
    option_fields: ClassVar[tuple[OptionField, ...]] = ()
    # Options that say WHICH authorization server the client belongs to: like the
    # client id, they cannot change while connections were made with the app.
    locked_options: ClassVar[tuple[str, ...]] = ()
    # A granted scope that includes others (Salesforce: `full` includes `api`), so a
    # grant naming the broader one is not refused for lacking the narrower.
    scope_implies: ClassVar[Mapping[str, tuple[str, ...]]] = {}
    client_auth: ClassVar[Literal["body", "basic"]] = "body"
    rotates_refresh_token: ClassVar[bool] = False
    pkce: ClassVar[bool] = True
    default_access_token_seconds: ClassVar[int] = 3600

    def __init__(self, app: ConnectionApp | None = None) -> None:
        self.app = app

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

    def check_owner(self, identity: Identity, *, owner_kind: Literal["user", "agent"], owner: Any) -> None:
        """Refuse an account this owner must not connect: raise
        `ProviderError(..., code="wrong_account")`. Any account by default."""

    # ---- the workspace's app ------------------------------------------------

    @property
    def client_id(self) -> str:
        return self.app.client_id if self.app is not None else ""

    @property
    def client_secret(self) -> str:
        return self.app.client_secret if self.app is not None else ""

    @property
    def options(self) -> Mapping[str, Any]:
        return (self.app.options or {}) if self.app is not None else {}

    def is_configured(self) -> bool:
        return bool(self.app is not None and self.client_id and self.client_secret)

    def requested_scopes(self) -> tuple[str, ...]:
        """What the authorize URL asks for: the provider's own scopes plus the app's chosen ones."""
        extra = self.app.extra_scopes if self.app is not None else ()
        return tuple(dict.fromkeys((*self.scopes, *extra)))

    def access_token_lifetime(self) -> timedelta:
        """How long a fresh access token lives when the token response does not
        say (`expires_in`): the app's `access_token_seconds` option, else the default."""
        seconds = self.options.get("access_token_seconds") or self.default_access_token_seconds
        return timedelta(seconds=int(seconds))

    @classmethod
    def clean_options(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        """`raw` as the options an app stores: every declared field, defaults
        filled, values checked by kind. Raises `ValueError(message)`, the message
        safe to show the admin. A provider adds its own checks by overriding and
        calling this first."""
        raw = dict(raw or {})
        fields = {f.name: f for f in cls.option_fields}
        unknown = sorted(set(raw) - set(fields))
        if unknown:
            raise ValueError(f"{cls.title} has no option {', '.join(unknown)}")
        cleaned: dict[str, Any] = {}
        for f in cls.option_fields:
            value = raw.get(f.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = f.default
            if value is None or value == "":
                if f.required:
                    raise ValueError(f"{f.label} is required")
                continue
            if f.kind == "int":
                if isinstance(value, bool):
                    raise ValueError(f"{f.label} must be a whole number")
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{f.label} must be a whole number") from None
            else:
                if not isinstance(value, str):
                    raise ValueError(f"{f.label} must be text")
                value = value.strip()
                if f.kind == "url":
                    parsed = urlparse(value)
                    if parsed.scheme != "https" or not parsed.hostname:
                        raise ValueError(f"{f.label} must be an https URL")
            cleaned[f.name] = value
        return cleaned

    # ---- the OAuth flow -----------------------------------------------------

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str, *,
                      owner_kind: Literal["user", "agent"] = "user", owner: Any = None) -> str:
        query = {
            **self.authorize_params(owner_kind=owner_kind, owner=owner),
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.requested_scopes()),
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
        """The required scopes a grant lacks: the provider's own required ones
        and every extra scope the app chose. An empty `granted` means the
        provider did not say, which is taken as what was asked for."""
        if not granted:
            return ()
        held = set(granted).union(*(self.scope_implies.get(s, ()) for s in granted))
        extra = self.app.extra_scopes if self.app is not None else ()
        return tuple(s for s in dict.fromkeys((*self.required_scopes, *extra)) if s not in held)

    def revoke(self, token: str, *, hint: Literal["refresh_token", "access_token"] = "refresh_token") -> None:
        """Revoke a token (RFC 7009) — a refresh token unless `hint` says it is an
        access token — with the client's authentication."""
        url = self.revoke_url()
        if not url:
            return
        data, auth = self._authenticated({"token": token, "token_type_hint": hint})
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
