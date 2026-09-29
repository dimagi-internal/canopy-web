"""What a host advertises: the fields the grant adds to its RFC 8414
authorization-server metadata and its RFC 9728 protected-resource metadata.

canopy discovers the token endpoint from the RFC 8414 document and refuses
unless its ``issuer`` is the configured one (RFC 9207's concern). A host that
already serves these documents merges the fields in; one that does not serves
them as-is. Advertised only while the grant is on: a server should not list a
way in it does not offer.
"""
from __future__ import annotations

from .. import contract
from .config import HostConfig


def _union(existing, *extra) -> list:
    out = list(existing or [])
    for value in extra:
        if value not in out:
            out.append(value)
    return out


def authorization_server_metadata(config: HostConfig, base: dict | None = None) -> dict:
    doc = dict(base or {})
    doc.setdefault("issuer", config.issuer)
    if config.token_endpoint:
        doc.setdefault("token_endpoint", config.token_endpoint)
    if not config.grant_enabled:
        return doc
    doc["grant_types_supported"] = _union(doc.get("grant_types_supported"), contract.JWT_BEARER_GRANT)
    doc["token_endpoint_auth_methods_supported"] = _union(
        doc.get("token_endpoint_auth_methods_supported"), contract.TOKEN_ENDPOINT_AUTH_METHOD)
    doc["token_endpoint_auth_signing_alg_values_supported"] = list(contract.GRANT_ALGORITHMS)
    doc["dpop_signing_alg_values_supported"] = list(contract.GRANT_ALGORITHMS)
    doc["scopes_supported"] = _union(doc.get("scopes_supported"), *sorted(config.scope_tools))
    if config.probe_enabled:
        # A namespaced extension field (RFC 8414 §2 allows them): where canopy
        # asks for a probe ID-JAG. Absent while no probe identity is set.
        doc[contract.PROBE_ENDPOINT_METADATA_FIELD] = config.probe.endpoint
    else:
        doc.pop(contract.PROBE_ENDPOINT_METADATA_FIELD, None)
    return doc


def protected_resource_metadata(config: HostConfig, base: dict | None = None) -> dict:
    doc = dict(base or {})
    doc.setdefault("resource", config.resource)
    doc["authorization_servers"] = _union(doc.get("authorization_servers"), config.issuer)
    doc.setdefault("bearer_methods_supported", ["header"])
    if config.grant_enabled:
        doc["dpop_signing_alg_values_supported"] = list(contract.GRANT_ALGORITHMS)
        doc["scopes_supported"] = _union(doc.get("scopes_supported"), *sorted(config.scope_tools))
    return doc
