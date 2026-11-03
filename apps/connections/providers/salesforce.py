"""Salesforce: OAuth 2.0 web-server flow with PKCE, against a Connected App.

What differs from the generic provider: the endpoints hang off the app's login
host (its `login_url` option: production, a sandbox, or a My Domain); the token
response carries no `expires_in` (the Connected App's session timeout decides,
so the app's `access_token_seconds` must be set to it, never longer); the
refresh token may rotate; each account has its own API host (`instance_url`);
and identity is read from the `id` URL the token response names.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlsplit

import requests

from .base import Identity, OptionField, Provider, ProviderError, TokenResponse

# Where an identity URL may point. The URL comes back in the token response and
# the access token is sent to it as a bearer, so it is checked rather than trusted.
SALESFORCE_HOST_SUFFIXES = (".salesforce.com", ".force.com")

DEFAULT_LOGIN_URL = "https://login.salesforce.com"
# Where the login host may be: the client secret is sent there, so only Salesforce's
# own login hosts or a My Domain.
LOGIN_HOSTS = ("login.salesforce.com", "test.salesforce.com")
MY_DOMAIN_SUFFIX = ".my.salesforce.com"
# One or more DNS labels before the suffix: nothing that could smuggle a port, a
# userinfo or a path. The same rule every machine applies to a sealed host.
_LABELS = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MAX_HOST = 253
# The access-token lifetime an app may declare: above the runner's re-stage margin
# is advisable (see docs/architecture/deploy.md), never longer than a day.
MIN_ACCESS_TOKEN_SECONDS, MAX_ACCESS_TOKEN_SECONDS = 300, 86400


def _my_domain(host: str) -> bool:
    return (host.endswith(MY_DOMAIN_SUFFIX) and len(host) <= _MAX_HOST
            and _LABELS.fullmatch(host[:-len(MY_DOMAIN_SUFFIX)]) is not None)


def _login_host_ok(host: str) -> bool:
    return host in LOGIN_HOSTS or _my_domain(host)


def _instance_url(url: str) -> tuple[str, str]:
    """`(the url as sealed, its host)`: `https://<lower-case My Domain>` and nothing
    more, or `""` for the url when a machine would refuse it."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", ""
    bare = parts.netloc.lower() == host and parts.path in ("", "/") and not parts.query and not parts.fragment
    return (f"https://{host}" if parts.scheme == "https" and bare and _my_domain(host) else ""), host


class SalesforceProvider(Provider):
    name = "salesforce"
    title = "Salesforce"
    # `id` lets the identity URL (who signed in) answer; `api` alone may not.
    scopes = ("api", "refresh_token", "id")
    # Salesforce names what it granted in `scope` ("refresh_token api", plus any
    # the Connected App adds); without both, canopy can neither call nor refresh.
    required_scopes = ("api", "refresh_token")
    # A Connected App set to `full` reports "full refresh_token": `full` includes `api`.
    scope_implies = {"full": ("api",)}
    rotates_refresh_token = True
    server_use_default = True
    server_calls = True
    default_access_token_seconds = 1800
    option_fields = (
        OptionField("login_url", "Login URL", "url", required=True, default=DEFAULT_LOGIN_URL),
        # Salesforce never says how long a token lives: set this to the Connected
        # App's session timeout, never longer.
        OptionField("access_token_seconds", "Access token lifetime (seconds)", "int",
                    default=default_access_token_seconds,
                    help="Salesforce does not say how long a token lives: set this to the Connected "
                         "App's session timeout, never longer."),
    )

    # A grant's refresh token is only good at the login host that issued it.
    locked_options = ("login_url",)

    @classmethod
    def clean_options(cls, raw):
        cleaned = super().clean_options(raw)
        host = (urlparse(cleaned["login_url"]).hostname or "").lower()
        if not _login_host_ok(host):
            raise ValueError("Login URL must be login.salesforce.com, test.salesforce.com "
                             "or a My Domain (….my.salesforce.com)")
        cleaned["login_url"] = f"https://{host}"
        seconds = cleaned["access_token_seconds"]
        if not MIN_ACCESS_TOKEN_SECONDS <= seconds <= MAX_ACCESS_TOKEN_SECONDS:
            raise ValueError(f"Access token lifetime must be between {MIN_ACCESS_TOKEN_SECONDS} "
                             f"and {MAX_ACCESS_TOKEN_SECONDS} seconds")
        return cleaned

    @property
    def _base(self) -> str:
        return (self.options.get("login_url") or DEFAULT_LOGIN_URL).rstrip("/")

    def authorize_url_base(self) -> str:
        return f"{self._base}/services/oauth2/authorize"

    def token_url(self) -> str:
        return f"{self._base}/services/oauth2/token"

    def revoke_url(self) -> str:
        return f"{self._base}/services/oauth2/revoke"

    def authorize_params(self, *, owner_kind, owner) -> Mapping[str, str]:
        # An agent's connection must be the AGENT's Salesforce user, not whoever is
        # signed in to Salesforce in the admin's browser: `login` makes Salesforce ask
        # for credentials even with a live session, `consent` shows what is granted.
        # (Salesforce has no use for the owner itself.)
        return {"prompt": "login consent"} if owner_kind == "agent" else {}

    def instance_url(self, body: Mapping[str, Any]) -> str:
        return str(body.get("instance_url") or "")

    def sealed_hosts(self, token: TokenResponse) -> tuple[str, str]:
        # Through `_base`, so a missing `login_url` seals the default rather than "".
        login_host = (urlparse(self._base).hostname or "").lower()
        if not _login_host_ok(login_host):
            raise ProviderError(f"Salesforce login host {login_host!r} is not a Salesforce login host or a My Domain",
                                code="bad_login_host")
        instance_url, host = _instance_url(token.instance_url)
        if not instance_url:
            raise ProviderError(f"Salesforce instance URL (host {host!r}) is not https://<My Domain>{MY_DOMAIN_SUFFIX}",
                                code="bad_instance_url")
        return login_host, instance_url

    def _identity_host_ok(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            return False
        # Every login host an app may save is itself a Salesforce host.
        return host.endswith(SALESFORCE_HOST_SUFFIXES)

    def identity(self, token: TokenResponse) -> Identity:
        # The bearer token goes only to a Salesforce host, never wherever `id` points.
        id_url = str(token.raw.get("id") or "")
        if not self._identity_host_ok(id_url):
            raise ProviderError("Salesforce identity URL is not a Salesforce host")
        resp = self._call(
            requests.get, id_url, "identity lookup",
            headers={"Authorization": f"Bearer {token.access_token}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise self._error(resp, "identity lookup")
        body = self._json(resp, "identity lookup")
        org, user = body.get("organization_id", ""), body.get("user_id", "")
        if not org or not user:
            raise ProviderError("Salesforce identity response had no organization_id/user_id")
        return Identity(account_id=f"{org}/{user}", label=body.get("username", ""))
