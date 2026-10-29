"""The provider contract: what canopy needs from an OAuth provider, nothing more."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
    access_token: str
    refresh_token: str  # "" when the provider did not return one (refresh without rotation)
    expires_at: datetime | None
    instance_url: str
    raw_id: str  # the provider's identity pointer (Salesforce: the `id` URL)


@dataclass(frozen=True)
class Identity:
    account_id: str
    label: str


class Provider(Protocol):
    name: str
    title: str
    scopes: tuple[str, ...]
    rotates_refresh_token: bool

    def is_configured(self) -> bool: ...

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str,
                      extra: Mapping[str, str] | None = None) -> str:
        """`extra`: more query parameters for the provider (e.g. `prompt`)."""
        ...

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> TokenResponse: ...

    def refresh(self, refresh_token: str) -> TokenResponse: ...

    def identity(self, token: TokenResponse) -> Identity: ...

    def revoke(self, token: str) -> None: ...
