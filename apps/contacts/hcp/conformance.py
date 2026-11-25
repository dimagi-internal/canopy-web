"""Discovery (Appendix C): the conformance level and known gaps."""
from __future__ import annotations

from ..models import PersonFact
from . import disclosure, purposes
from .core import HCP_VERSION, HELD_CATEGORIES, PROBLEM_BASE, REGISTERED_CATEGORIES
from .entries import CONFLICT_DETECTION
from .relevance import minimization_method
from .v2 import ENTRY_SCHEMA, HCP_VERSION_V2

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
            # HCP v2 (2.0-draft-3): same URL with `HCP-Version: 2.0` returns the v2 document.
            "v2": {"discovery": "GET this URL with the header HCP-Version: 2.0",
                   "entries_endpoint": f"{base_url}/v2/entries", "status": "no v2 conformance claimed",
                   "oauth_clients": V2_OAUTH_CLIENTS},
            # HCP v2 6.1.5.2: the purposes a grant may carry, each in words.
            "purposes": purposes.describe(purposes.PURPOSES),
            # HCP v2 15.2: how this instance finds conflicts between entries.
            "conflict_detection": CONFLICT_DETECTION,
            "custom_categories": f"{PersonFact.CUSTOM_PREFIX}<name>",
        },
    }


#: An HCP service access token (`hcpat_`, an OAuth client of the HCP service) is confined to
#: `/api/hcp/v1/` by the bearer middleware, so v2 is not offered to OAuth clients.
V2_OAUTH_CLIENTS = ("not served: an OAuth client's access token opens /api/hcp/v1/ only; "
                    "use the v1 endpoints in the v1 discovery document")

#: What canopy states it supports of v2 (draft 3). `conformsTo` is deliberately empty: canopy models v2
#: and serves its HTTP binding, but has not audited itself against every v2 requirement (18), so it
#: claims no v2 identifier. v1 discovery still claims Core, not Interop.
V2_CONFORMS_TO: list[str] = []


def discovery_v2(base_url: str) -> dict:
    """The v2 discovery document (19), served at the same URL as v1's when the request names
    `HCP-Version: 2.0` (3.3: never silently serve a different version). `base_url` is `<origin>/api/hcp`:
    v2's endpoints are `<base>/v2/...` as the HTTP binding lays them out (9.2)."""
    return {
        "hcp_version": HCP_VERSION_V2,
        "provider": base_url,
        "provider_operator": {"id": "canopy.dimagi.com", "name": "Dimagi (canopy)"},
        "conformsTo": list(V2_CONFORMS_TO),
        "bindings": ["http"],
        "entries_endpoint": f"{base_url}/v2/entries",
        "grants_endpoint": f"{base_url}/v2/grants",
        "audit_endpoint": f"{base_url}/v2/audit",
        "export_endpoint": f"{base_url}/v2/export",
        "rules_endpoint": f"{base_url}/v2/rules",
        "directives_endpoint": f"{base_url}/v2/directives",
        "disclosures_endpoint": f"{base_url}/v2/disclosures",
        "supported_categories": [
            {"id": c, "name": c.replace("_", " ").capitalize(), "sensitivity": disclosure.SENSITIVITY[c],
             "registry": "1"} for c in HELD_CATEGORIES] + [
            {"id": PersonFact.CUSTOM_PREFIX + "<name>", "name": "Custom", "sensitivity": "medium",
             "definer": "canopy"}],
        "category_registry": "1",
        "accepts_entry_schemas": [ENTRY_SCHEMA],
        "minimization_method": minimization_method(),
        "conflict_detection": CONFLICT_DETECTION,
        "audit_durability": {"default": "synchronous"},
        "audit_integrity": "append-only",
        "storage_encryption": "server",
        "pagination": {"audit": "cursor"},
        "problem_type_base": PROBLEM_BASE,
        "canopy": {
            "status": "draft: canopy models HCP v2 (2.0-draft-3) and serves this HTTP binding; it claims no v2 "
                      "conformance identifier yet (`conformsTo` is empty)",
            # Who this binding serves, said plainly: an OAuth client's token opens v1 only.
            "context_clients": "canopy's own agents, each under the grant the person gave it, for the person who "
                               "started the turn it names",
            "management_clients": "the person themself: their browser session or a personal access token",
            "oauth_clients": V2_OAUTH_CLIENTS,
            "purposes": purposes.describe(purposes.PURPOSES),
            "not_built": ["MCP binding for v2", "v2 for OAuth clients of the HCP service", "VC evidence", "delegation", "onward sharing", "import",
                          "end-to-end encrypted storage", "category registry beyond the work categories",
                          f"registered categories not held: {sorted(set(REGISTERED_CATEGORIES) - set(HELD_CATEGORIES))}"],
        },
    }
