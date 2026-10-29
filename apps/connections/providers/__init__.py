"""The provider registry: every provider canopy can connect to, by `name`.

ADDING A PROVIDER (Google, Sage Intacct next) is one module here plus settings,
and nothing else — services, views, routes, the frontend, the CLI and the
runner all read providers from this registry or from `GET /api/connections/`:

1. `providers/<name>.py`: a `Provider` subclass (`base.py` says what to fill in;
   `salesforce.py` is the worked example) with `identity()` implemented.
2. List an instance in `ALL` below.
3. Settings `<PREFIX>_CLIENT_ID`, `<PREFIX>_CLIENT_SECRET` and
   `<PREFIX>_ACCESS_TOKEN_SECONDS` (plus the provider's own) via `env(...)` in
   `config/settings/base.py`, and in `.env.example`.
4. Register the callback `/auth/connections/<name>/callback/` with the provider,
   and add the deploy wiring (`docs/architecture/deploy.md`).
"""
from __future__ import annotations

from .base import Identity, Provider, ProviderError, ProviderUnavailable, TokenResponse, UnknownProvider
from .salesforce import SalesforceProvider

ALL: dict[str, Provider] = {p.name: p for p in (SalesforceProvider(),)}


def get(name: str) -> Provider:
    try:
        return ALL[name]
    except KeyError:
        raise UnknownProvider(name) from None


__all__ = [
    "ALL", "get", "Identity", "Provider", "ProviderError", "ProviderUnavailable", "TokenResponse",
    "UnknownProvider",
]
