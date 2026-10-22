"""Discovery (Appendix C): the conformance level and known gaps."""
from __future__ import annotations

from ..models import PersonFact
from .core import HCP_VERSION, HELD_CATEGORIES, PROBLEM_BASE
from .relevance import minimization_method

# --- discovery (Appendix C) ------------------------------------------------------


#: What this instance claims, from the self-audit in docs/architecture/
#: hcp-conformance.md. Not Interop while any Interop MUST is a known gap.
CONFORMANCE_LEVEL = "HCP-v1-Core"
KNOWN_GAPS = [
    "2.1.1: Tier 1 entries are not validated against the published JSON Schema, which "
    "Appendix A says will be published at https://hcp.me/schemas/v1 and is not yet",
]


def discovery(base_url: str) -> dict:
    from .. import hcp_oauth

    return {
        "hcp_version": HCP_VERSION,
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "revocation_endpoint": f"{base_url}/oauth/revoke",
        "preferences_endpoint": f"{base_url}/v1/preferences",
        "grants_endpoint": f"{base_url}/v1/grants",
        "audit_endpoint": f"{base_url}/v1/audit",
        "export_endpoint": f"{base_url}/v1/export",
        "mcp_manifest_endpoint": f"{base_url}/.well-known/mcp-manifest",
        "mcp_endpoint": base_url.rsplit("/api/", 1)[0] + "/api/mcp/",
        "supported_categories": list(HELD_CATEGORIES),
        "supported_transports": ["mcp", "rest"],
        "conformance_level": CONFORMANCE_LEVEL,
        "authorization_profile": "oauth2",
        "envelope_form": "grouped",
        "minimization_method": minimization_method(),
        "audit_durability": {"default": "synchronous"},
        "pagination": {"audit": "cursor"},
        "problem_type_base": PROBLEM_BASE,
        "rate_limits": {"documentation_url": f"{base_url}/.well-known/hcp-configuration",
                        "oauth_clients": f"{hcp_oauth.RATE_LIMIT_PER_MINUTE} requests a minute per "
                                         "client on /v1/; 429 with Retry-After",
                        "never_limited": "DELETE /v1/grants/{grantId}"},
        "canopy": {
            "service": {
                "audience": hcp_oauth.audience(),
                "client_registration": "closed: clients are registered by canopy's administrators",
                "who_may_authorize": ("Dimagi accounts only" if hcp_oauth.audience() == "internal"
                                      else "anyone who can sign in to canopy"),
                "oauth_metadata": f"{base_url}/.well-known/oauth-authorization-server",
                "transports_for_oauth_clients": ["rest"],
            },
            "conformance": {"target": "HCP-v1-Interop", "known_gaps": KNOWN_GAPS},
            "grants": "OAuth clients: authorization code + PKCE S256, temporary by default, "
                      "persistence a separate act. canopy's own agents: per agent, only by the "
                      "person's act in a session (non-OAuth channel, 4.1.6) — 'for this session' "
                      "(temporary, ends with the session, at most 24 h) or 'always for this "
                      "agent' (persistent), nothing preselected; bounded by the person's "
                      "settings; revocable; never presumed",
            "tier2": "not supported",
            "custom_categories": f"{PersonFact.CUSTOM_PREFIX}<name>",
        },
    }
