"""The provider registry. Add a provider by writing one module and listing it here."""
from __future__ import annotations

from .base import Identity, Provider, ProviderError, TokenResponse, UnknownProvider
from .salesforce import SalesforceProvider

ALL: dict[str, Provider] = {p.name: p for p in (SalesforceProvider(),)}


def get(name: str) -> Provider:
    try:
        return ALL[name]
    except KeyError:
        raise UnknownProvider(name) from None


__all__ = ["ALL", "get", "Identity", "Provider", "ProviderError", "TokenResponse", "UnknownProvider"]
