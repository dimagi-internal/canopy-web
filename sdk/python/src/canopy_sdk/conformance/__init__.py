"""Conformance checks a host (or canopy) runs against a LIVE host.

Every network call happens only inside an explicit ``check_*`` / ``run`` call,
through a transport you can replace (``fetch_json``, ``post_form``,
``post_json``); nothing runs on import.

* ``check_metadata(issuer, resource)`` — the RFC 8414 and RFC 9728 documents
  advertise what canopy needs (the jwt-bearer grant, ``private_key_jwt``, DPoP
  with EdDSA/ES256 only, the issuer and resource named consistently).
* ``check_jwks(url)`` — the host's published signing keys are public,
  asymmetric, and carry thumbprint ``kid``s.
* ``check_grant(...)`` — redeem an ID-JAG the way canopy does, then prove a
  replay of it is refused. Needs a client the host accepts (its CIMD must be
  reachable by the host) and an ID-JAG (from a host test endpoint, or signed
  with a test credential — ``canopy_sdk.host.issue_id_jag``).
* ``check_client(...)`` — with canopy's client keys and NO grant: does the
  host accept canopy as its client? (``invalid_grant`` for a throwaway-signed
  ID-JAG means yes.) What an operator without the host's key can still prove.
* ``check_mcp(...)`` — one DPoP-authenticated MCP call (``initialize`` +
  ``tools/list``), plus proof that the token is refused as a plain bearer and
  that a replayed proof is refused.
* ``run(...)`` — whichever of those the arguments allow, as one ``Report``.

For a host's own CI, ``canopy_sdk.conformance.pytest_plugin`` provides fixtures
(opt in with ``pytest_plugins = ["canopy_sdk.conformance.pytest_plugin"]``).
"""
from .checks import Check, Report, check_client, check_grant, check_jwks, check_mcp, check_metadata, run

__all__ = ["Check", "Report", "check_client", "check_grant", "check_jwks", "check_mcp", "check_metadata", "run"]
