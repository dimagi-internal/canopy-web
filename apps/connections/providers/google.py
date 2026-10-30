"""Google (Gmail, Calendar): OAuth 2.0 web-server flow with PKCE, against a
workspace's own Google Cloud OAuth client.

What differs from the generic provider: a refresh token is only issued with
`access_type=offline`, and only re-issued on a later connect with
`prompt=consent`; the refresh response omits `refresh_token` (it does not
rotate, so the stored one is kept); the token response carries `expires_in`;
the API host is fixed (no per-account `instance_url`); and identity is the
OpenID Connect userinfo (`sub`, a verified `email`).

Any Google account may connect by default — a Workspace account of any
organisation or a personal @gmail.com one. A workspace narrows that with the
`allowed_domains` option; an agent must connect its own Google account
(`Agent.google_identity()`) when it has one.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import requests

from .base import Identity, OptionField, Provider, ProviderError, TokenResponse

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_SCOPE = "https://www.googleapis.com/auth/"
USERINFO_EMAIL = f"{_SCOPE}userinfo.email"

# One DNS name: dot-separated labels of letters, digits and inner hyphens, a
# letter-led last label, at most 253 characters.
_DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _domains(value: Any) -> tuple[str, ...]:
    """The `allowed_domains` option as a tuple of lowercase domains (empty: any)."""
    return tuple(d for d in re.split(r"[\s,]+", str(value or "").lower()) if d)


class GoogleProvider(Provider):
    name = "google"
    title = "Google"
    scopes = ("openid", USERINFO_EMAIL)
    # Without both, the userinfo lookup cannot say whose account this is.
    required_scopes = ("openid", USERINFO_EMAIL)
    scope_choices = {
        f"{_SCOPE}gmail.readonly": "Read mail",
        f"{_SCOPE}gmail.compose": "Write drafts",
        f"{_SCOPE}gmail.send": "Send mail",
        f"{_SCOPE}gmail.modify": "Label and archive mail",
        f"{_SCOPE}calendar.readonly": "Read calendars",
        f"{_SCOPE}calendar.events": "Edit calendar events",
    }
    rotates_refresh_token = False
    option_fields = (
        # Empty (the default): any Google account, including personal @gmail.com
        # ones and other organisations' Workspace accounts.
        OptionField("allowed_domains", "Allowed email domains (comma-separated, e.g. dimagi.com, "
                    "dimagi-ai.com; blank allows any Google account)", "text"),
    )
    # No locked options: the authorization server is always Google's, so nothing
    # here says which server a grant belongs to. `allowed_domains` may change while
    # connections exist; it gates NEW connects only — a connection made before a
    # domain was dropped keeps working until someone disconnects it.
    locked_options = ()

    @classmethod
    def clean_options(cls, raw):
        cleaned = super().clean_options(raw)
        domains = tuple(dict.fromkeys(_domains(cleaned.get("allowed_domains"))))
        bad = [d for d in domains if not _DOMAIN.match(d)]
        if bad:
            raise ValueError(f"Not a domain: {', '.join(bad)}")
        if domains:
            cleaned["allowed_domains"] = ", ".join(domains)
        else:
            cleaned.pop("allowed_domains", None)
        return cleaned

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return _domains(self.options.get("allowed_domains"))

    def authorize_url_base(self) -> str:
        return AUTHORIZE_URL

    def token_url(self) -> str:
        return TOKEN_URL

    def revoke_url(self) -> str:
        # Google revokes by `token` alone and ignores the client credentials the
        # base sends with it (RFC 7009 shape), which is harmless.
        return REVOKE_URL

    def authorize_params(self, *, owner_kind, owner) -> Mapping[str, str]:
        # `offline` is what makes Google issue a refresh token; `consent` makes it
        # issue one again on a reconnect (it otherwise only does on the first grant).
        params = {"access_type": "offline", "prompt": "consent"}
        if owner_kind == "agent":
            # The admin's browser is usually signed in to the admin's own Google
            # account: make Google offer the account choice, pre-filled with the agent's.
            params["prompt"] = "consent select_account"
            hint = owner.google_identity() if owner is not None else ""
            if hint:
                params["login_hint"] = hint
        # `hd` only narrows the account chooser, and only to one domain; the domain
        # rule itself is enforced by `check_owner` on the verified email.
        if len(self.allowed_domains) == 1:
            params["hd"] = self.allowed_domains[0]
        return params

    def identity(self, token: TokenResponse) -> Identity:
        resp = self._call(
            requests.get, USERINFO_URL, "identity lookup",
            headers={"Authorization": f"Bearer {token.access_token}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise self._error(resp, "identity lookup")
        body = self._json(resp, "identity lookup")
        sub, email = str(body.get("sub") or ""), str(body.get("email") or "").strip().lower()
        if not sub or not email:
            raise ProviderError("Google identity response had no sub/email")
        # A boolean in the userinfo response; older Google responses sent the string.
        if body.get("email_verified") not in (True, "true"):
            raise ProviderError(f"Google has not verified {email}")
        return Identity(account_id=sub, label=email, email=email)

    def check_owner(self, identity: Identity, *, owner_kind, owner) -> None:
        email = identity.email
        allowed = self.allowed_domains
        if allowed and email.rpartition("@")[2] not in allowed:
            raise ProviderError(f"{email} is not in {', '.join(allowed)}", code="wrong_account")
        if owner_kind == "agent" and owner is not None:
            expected = owner.google_identity()
            if expected and email != expected.lower():
                raise ProviderError(f"signed in as {email}, not {expected}", code="wrong_account")
