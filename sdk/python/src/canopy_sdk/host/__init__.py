"""What a HOST runs: the site that signs its visitor in and embeds a canopy agent.

* ``HostConfig`` — who the host is and which canopy it trusts.
* ``sign_visitor_assertion`` / ``issue_id_jag`` / ``arrival_payload`` /
  ``mint_contact_token`` — the arrival (host grant contract §1).
* ``PageTokens`` — the route → scopes registry and the server-signed page token.
* ``GrantHandler`` — the token endpoint's jwt-bearer grant (§2).
* ``ResourceVerifier`` + ``DPoPGate`` — the MCP side (§3).
* ``authorization_server_metadata`` / ``protected_resource_metadata`` — discovery.

Framework-free. ``canopy_sdk.django`` wires all of it into a Django project.
"""
from .asgi import DPoPGate, delegated_principal, presented_dpop_jkt
from .client_keys import ClientKeyResolver, MetadataError
from .config import HostConfig, HostNotConfigured
from .grant import GrantHandler, GrantRefused, GrantResult
from .metadata import authorization_server_metadata, protected_resource_metadata
from .pages import PageTokens
from .resource import DelegatedPrincipal, DPoPRefused, ResourceVerifier
from .signing import MintFailed, arrival_payload, issue_id_jag, mint_contact_token, sign_visitor_assertion

__all__ = [
    "ClientKeyResolver", "DPoPGate", "DPoPRefused", "DelegatedPrincipal", "GrantHandler",
    "GrantRefused", "GrantResult", "HostConfig", "HostNotConfigured", "MetadataError", "MintFailed",
    "PageTokens", "ResourceVerifier", "arrival_payload", "authorization_server_metadata",
    "delegated_principal", "issue_id_jag", "mint_contact_token", "presented_dpop_jkt",
    "protected_resource_metadata", "sign_visitor_assertion",
]
