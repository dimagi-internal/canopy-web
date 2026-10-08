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
class ProbeIdentity:
    """Who canopy's LIVE PROBE acts as at this host, and the one call it makes.

    The probe (``canopy_sdk.host.ProbeHandler``) lets canopy prove, on a
    schedule and with nobody at a keyboard, that a real grant can be issued,
    redeemed and USED against this host — the part conformance cannot reach
    without a visitor. So it needs a principal of its own:

    * ``subject`` — the host's own id for a DEDICATED, low-privilege principal
      (never a real person's account). The only subject the probe ever issues
      for, whatever the request says.
    * ``scope`` — one READ-ONLY scope (``<x>:read``) from ``scope_tools``.
    * ``tool`` + ``arguments`` — the one call canopy makes with the token; the
      tool must be one ``scope`` unlocks, and the call should be meaningful for
      the principal (it must succeed).
    * ``denied_tool`` — optional: a real tool OUTSIDE ``scope``, which canopy
      asks for and your MCP must refuse. Without one canopy asks for a name no
      server offers, which proves less.
    * ``endpoint`` — the probe endpoint's public URL (a DPoP proof's ``htu``).
    * ``page`` — optional: the page key the probe stands in for, for your audit.
    """

    endpoint: str
    subject: str
    scope: str
    tool: str
    arguments: Mapping = field(default_factory=dict)
    denied_tool: str = ""
    page: str = ""

    def __post_init__(self):
        set_ = object.__setattr__
        for name in ("endpoint", "subject", "scope", "tool", "denied_tool", "page"):
            set_(self, name, str(getattr(self, name) or "").strip())
        if not isinstance(self.arguments, Mapping):
            raise ValueError("the probe's arguments must be a mapping")
        set_(self, "arguments", dict(self.arguments))
        for name in ("endpoint", "subject", "scope", "tool"):
            if not getattr(self, name):
                raise ValueError(f"the probe identity needs a {name}")
        if not self.scope.endswith(":read"):
            raise ValueError(f"the probe scope must be read-only (<x>:read), not {self.scope!r}")

    @classmethod
    def from_mapping(cls, value: Mapping) -> ProbeIdentity:
        """From ``{"ENDPOINT", "SUBJECT", "SCOPE", "TOOL", "ARGUMENTS",
        "DENIED_TOOL", "PAGE"}`` (the Django setting's shape; lower-case keys
        work too)."""
        get = {str(k).lower(): v for k, v in (value or {}).items()}.get
        return cls(endpoint=get("endpoint") or "", subject=get("subject") or "",
                   scope=get("scope") or "", tool=get("tool") or "",
                   arguments=get("arguments") or {}, denied_tool=get("denied_tool") or "",
                   page=get("page") or "")


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
    #: canopy's live probe (``ProbeIdentity``). ``None`` — the default — turns
    #: the probe endpoint off (404) and keeps it out of the metadata.
    probe: ProbeIdentity | None = None
    #: Runner flags every conversation of this host's visitors must run under
    #: (``canopy_sdk.contract.RUNNER_FLAGS``); sent in every visitor assertion.
    runner_requirements: Sequence[str] = ()

    def __post_init__(self):
        set_ = object.__setattr__
        set_(self, "signing_key", load_private_key(self.signing_key, what="the host signing key"))
        for name in ("canopy_base_url", "issuer", "resource", "token_endpoint",
                     "canopy_client_id", "canopy_audience"):
            set_(self, name, (getattr(self, name) or "").strip())
        set_(self, "canopy_base_url", contract.normalize_url(self.canopy_base_url))
        set_(self, "issuer", contract.normalize_url(self.issuer))
        set_(self, "scope_tools", _frozen_scope_tools(self.scope_tools))
        set_(self, "runner_requirements",
             contract.parse_runner_requirements(list(self.runner_requirements)))
        if not 0 < self.assertion_ttl <= contract.ASSERTION_MAX_LIFETIME:
            raise ValueError(f"assertion_ttl must be 1..{contract.ASSERTION_MAX_LIFETIME}s")
        if not 0 < self.id_jag_ttl <= contract.ID_JAG_MAX_LIFETIME:
            raise ValueError(f"id_jag_ttl must be 1..{contract.ID_JAG_MAX_LIFETIME}s")
        if not 0 < self.access_token_ttl <= contract.ACCESS_TOKEN_MAX_LIFETIME:
            raise ValueError(f"access_token_ttl must be 1..{contract.ACCESS_TOKEN_MAX_LIFETIME}s")
        if isinstance(self.probe, Mapping):
            set_(self, "probe", ProbeIdentity.from_mapping(self.probe))
        if self.probe is not None:
            probe = self.probe
            unlocked = self.scope_tools.get(probe.scope)
            if unlocked is None:
                raise ValueError(f"the probe scope {probe.scope!r} is not in scope_tools")
            if probe.tool not in unlocked:
                raise ValueError(f"the probe tool {probe.tool!r} is not one {probe.scope!r} unlocks")
            if probe.denied_tool and probe.denied_tool in unlocked:
                raise ValueError(f"the probe's denied_tool {probe.denied_tool!r} is inside its own scope")

    # --- derived ---------------------------------------------------------------

    @property
    def assertions_enabled(self) -> bool:
        return bool(self.canopy_base_url and self.app_name)

    @property
    def grant_enabled(self) -> bool:
        """Every value the grant names must be present, or none of it runs."""
        return bool(self.canopy_client_id and self.issuer and self.resource and self.token_endpoint)

    @property
    def probe_enabled(self) -> bool:
        """The probe runs only on top of a working grant."""
        return self.grant_enabled and self.probe is not None

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
