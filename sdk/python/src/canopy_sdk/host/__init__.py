"""What a HOST runs: the site that signs its visitor in and embeds a canopy agent.

* ``HostConfig`` — who the host is and which canopy it trusts.
* ``sign_visitor_assertion`` / ``issue_id_jag`` / ``arrival_payload`` /
  ``mint_contact_token`` — the arrival (host grant contract §1).
* ``PageRegistry`` / ``PageTokens`` — the page → scopes registry, in its two modes:
  a page KEY the browser names (an SPA), or a server-signed page token (a
  server-rendered page). ``registry.scopes_for(value, user_id)`` either way.
* ``GrantHandler`` — the token endpoint's jwt-bearer grant (§2).
* ``ResourceVerifier`` + ``DPoPGate`` — the MCP side (§3).
* ``ProbeIdentity`` + ``ProbeHandler`` — canopy's live probe: a real ID-JAG for
  one fixed, low-privilege principal, so the whole chain is exercised on a
  schedule without a visitor.
* ``authorization_server_metadata`` / ``protected_resource_metadata`` — discovery.

Framework-free. ``canopy_sdk.django`` wires all of it into a Django project.
"""
from .asgi import DPoPGate, delegated_principal, presented_dpop_jkt
from .client_keys import ClientKeyResolver, MetadataError
from .config import HostConfig, HostNotConfigured, ProbeIdentity
from .grant import GrantHandler, GrantRefused, GrantResult
from .metadata import authorization_server_metadata, protected_resource_metadata
from .pages import PageRegistry, PageTokens
from .probe import ProbeDisabled, ProbeHandler, ProbeResult
from .resource import DelegatedPrincipal, DPoPRefused, ResourceVerifier
from .signing import MintFailed, arrival_payload, issue_id_jag, mint_contact_token, sign_visitor_assertion

__all__ = [
    "ClientKeyResolver", "DPoPGate", "DPoPRefused", "DelegatedPrincipal", "GrantHandler",
    "GrantRefused", "GrantResult", "HostConfig", "HostNotConfigured", "MetadataError", "MintFailed",
    "PageRegistry", "PageTokens", "ProbeDisabled", "ProbeHandler", "ProbeIdentity", "ProbeResult",
    "ResourceVerifier", "arrival_payload", "authorization_server_metadata",
    "delegated_principal", "issue_id_jag", "mint_contact_token", "presented_dpop_jkt",
    "protected_resource_metadata", "sign_visitor_assertion",
]
