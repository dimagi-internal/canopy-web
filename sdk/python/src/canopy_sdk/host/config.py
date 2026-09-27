"""A host's configuration: who it is, what it serves, which canopy it trusts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .. import contract
from ..keys import load_private_key, public_jwk


class HostNotConfigured(RuntimeError):
    """A piece of the host half is off because configuration is missing."""


def _frozen_scope_tools(value) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for scope, tools in (value or {}).items():
        if isinstance(tools, str):
            tools = [tools]
        out[str(scope)] = frozenset(str(t) for t in tools)
    return out


@dataclass(frozen=True)
class HostConfig:
    """Everything the host half needs. Nothing is read from settings here —
    ``canopy_sdk.django.conf`` builds one from Django settings, and any other
    framework builds one directly.

    Nothing assumes the host is not canopy itself: a canopy-web deployment that
    wants agents to call its OWN MCP as the visitor builds a ``HostConfig`` with
    its own issuer/resource and its own ``canopy_client_id``, exactly like any
    other site.
    """

    #: The host's signing key (PEM or loaded key; Ed25519 or P-256). Signs the
    #: visitor assertion AND the ID-JAG — canopy verifies both with the same key.
    signing_key: object
    #: Where the visitor assertion is addressed: the canopy deployment's base URL
    #: (e.g. ``https://labs.connect.dimagi.com/canopy``). Also where the host
    #: POSTs the arrival.
    canopy_base_url: str = ""
    #: The host's name as registered in canopy (the Connected site): the ``iss``
    #: of the visitor assertion.
    app_name: str = ""
    #: The host's OAuth issuer (RFC 8414). ``iss`` = ``aud`` of every ID-JAG.
    issuer: str = ""
    #: The host's MCP server URL (RFC 9728 ``resource``).
    resource: str = ""
    #: The host's token endpoint (where canopy redeems). Must match what the
    #: host's RFC 8414 metadata advertises.
    token_endpoint: str = ""
    #: The ONE client allowed to redeem grants: canopy's CIMD URL. Empty turns the
    #: grant off (assertions still work).
    canopy_client_id: str = ""
    #: What each scope unlocks at the host's MCP. THE only place a delegated
    #: token's reach is decided.
    scope_tools: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: Public keys (or private keys) a rotation retired but ID-JAGs signed with
    #: which may still be in flight. Verified by ``kid``.
    retired_keys: Sequence[object] = ()
    #: Override for the assertion's ``aud`` if canopy's audience differs from its
    #: base URL (canopy's ``EMBED_ASSERTION_AUDIENCE``).
    canopy_audience: str = ""
    assertion_ttl: int = 60
    id_jag_ttl: int = 120
    access_token_ttl: int = contract.ACCESS_TOKEN_MAX_LIFETIME

    def __post_init__(self):
        set_ = object.__setattr__
        set_(self, "signing_key", load_private_key(self.signing_key, what="the host signing key"))
        for name in ("canopy_base_url", "issuer", "resource", "token_endpoint",
                     "canopy_client_id", "canopy_audience"):
            set_(self, name, (getattr(self, name) or "").strip())
        set_(self, "canopy_base_url", contract.normalize_url(self.canopy_base_url))
        set_(self, "issuer", contract.normalize_url(self.issuer))
        set_(self, "scope_tools", _frozen_scope_tools(self.scope_tools))
        if not 0 < self.assertion_ttl <= contract.ASSERTION_MAX_LIFETIME:
            raise ValueError(f"assertion_ttl must be 1..{contract.ASSERTION_MAX_LIFETIME}s")
        if not 0 < self.id_jag_ttl <= contract.ID_JAG_MAX_LIFETIME:
            raise ValueError(f"id_jag_ttl must be 1..{contract.ID_JAG_MAX_LIFETIME}s")
        if not 0 < self.access_token_ttl <= contract.ACCESS_TOKEN_MAX_LIFETIME:
            raise ValueError(f"access_token_ttl must be 1..{contract.ACCESS_TOKEN_MAX_LIFETIME}s")

    # --- derived ---------------------------------------------------------------

    @property
    def assertions_enabled(self) -> bool:
        return bool(self.canopy_base_url and self.app_name)

    @property
    def grant_enabled(self) -> bool:
        """Every value the grant names must be present, or none of it runs."""
        return bool(self.canopy_client_id and self.issuer and self.resource and self.token_endpoint)

    @property
    def audience(self) -> str:
        return contract.normalize_url(self.canopy_audience or self.canopy_base_url)

    @property
    def public_jwk(self) -> dict:
        return {**public_jwk(self.signing_key), "use": "sig"}

    @property
    def kid(self) -> str:
        return self.public_jwk["kid"]

    def verification_keys(self) -> dict[str, object]:
        """``kid`` → public key for every key an ID-JAG this host issued may be
        signed with: the live one first, then retired ones."""
        out = {self.kid: self.signing_key.public_key()}
        for key in self.retired_keys:
            if isinstance(key, (str, bytes)):
                from cryptography.hazmat.primitives import serialization

                raw = key.encode() if isinstance(key, str) else key
                try:
                    key = serialization.load_pem_public_key(raw)
                except ValueError:
                    key = load_private_key(raw, what="a retired host key")
            pub = key.public_key() if hasattr(key, "private_bytes") else key
            out.setdefault(public_jwk(pub)["kid"], pub)
        return out

    def jwks(self) -> dict:
        """The host's published JWKS (what canopy verifies assertions and ID-JAGs
        with): the live key plus retired ones still in their window."""
        keys = []
        for pub in self.verification_keys().values():
            keys.append({**public_jwk(pub), "use": "sig"})
        return {"keys": keys}

    def tools_for_scopes(self, scopes) -> frozenset[str]:
        return contract.tools_for_scopes(scopes, self.scope_tools)
