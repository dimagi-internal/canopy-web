"""The provider registry: every provider canopy can connect to, by `name`.

A provider is a class registered here; it is used bound to one workspace's
`ConnectionApp` (`for_app(app)`), which holds the OAuth client a workspace admin
configured. Nothing about a provider lives in Django settings.

ADDING A PROVIDER (Sage Intacct next) is one module here, and nothing else —
services, views, routes, the frontend, the CLI and the runner all read
providers from this registry. There is no settings step:

1. `providers/<name>.py`: a `Provider` subclass (`base.py` says what to fill in;
   `salesforce.py` and `google.py` are the worked examples) with `identity()`
   implemented, and `required_scopes`, `scope_choices`, `option_fields` (+
   `clean_options`), `locked_options`, `client_auth`,
   `authorize_params(owner_kind, owner)` (e.g. Google's agent `login_hint`) and
   `check_owner` where the provider needs them.
2. List the class in `CLASSES` below.
3. Each workspace that wants it configures the provider in its Settings: an
   admin creates an OAuth client at the provider, registers canopy's callback
   for that workspace, `/auth/connections/<provider>/<workspace>/callback/`,
   and saves the client id and secret (and any options) as the workspace's app.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    Identity,
    OptionField,
    Provider,
    ProviderError,
    ProviderUnavailable,
    TokenResponse,
    UnknownProvider,
)
from .google import GoogleProvider
from .salesforce import SalesforceProvider

if TYPE_CHECKING:
    from ..models import ConnectionApp

CLASSES: dict[str, type[Provider]] = {c.name: c for c in (SalesforceProvider, GoogleProvider)}


def get_class(name: str) -> type[Provider]:
    try:
        return CLASSES[name]
    except KeyError:
        raise UnknownProvider(name) from None


def for_app(app: ConnectionApp) -> Provider:
    """The app's provider, bound to its credentials and options."""
    return get_class(app.provider)(app)


__all__ = [
    "CLASSES", "for_app", "get_class", "Identity", "OptionField", "Provider", "ProviderError",
    "ProviderUnavailable", "TokenResponse", "UnknownProvider",
]
