"""A Connected site's "Test connection": the SDK's conformance checks, run from canopy.

Every setting on a Connected site fails CLOSED and silently — a JWKS URL that
answers HTML, an issuer whose metadata names a different issuer, a host that
never allowlisted canopy's client id — and the only symptom is an agent that
cannot act for anybody. This runs, from canopy-web's own server, what canopy
would do with those settings, and says which step fails and why:

* **Signing keys** — the site's JWKS (`canopy_sdk.conformance.check_jwks`):
  reachable, public-only, asymmetric, `kid` = thumbprint. RSA is tolerated only
  for a site that signs visitor assertions alone; an ID-JAG key must be EdDSA
  or ES256. Pasted PEMs are checked for the same key types.
* **Discovery** — when the site issues host grants: its RFC 8414 and RFC 9728
  documents (`check_metadata`): the issuer names itself, the token endpoint is
  https, the jwt-bearer grant / `private_key_jwt` / DPoP (EdDSA, ES256) are
  advertised, and the resource names its authorization server.
* **canopy as the site's client** (`check_client`) — the one thing provable
  WITHOUT the host's private key: the jwt-bearer grant, with canopy's real
  client assertion and DPoP proof, carrying an ID-JAG signed by a throwaway
  key. `invalid_grant` means the host authenticated canopy; `invalid_client`
  means it does not accept canopy (not allowlisted, or it cannot read canopy's
  keys). Spends nothing at the host.

What it cannot do is redeem a REAL grant: that needs an ID-JAG signed by the
host's key, which canopy never holds. A host with a probe identity signs one for
its dedicated probe principal, and `live_probe.py` walks that chain — Test
connection runs both.

Every request goes through `outbound.py` — https only, no private address
space, no redirects, bounded — because each URL here is one a tenant typed.
"""
from __future__ import annotations

from dataclasses import dataclass

from canopy_sdk import conformance, fetch

from . import client_identity, outbound, self_host

#: Plain-language names for the SDK's check ids. An unknown id (a newer SDK)
#: falls back to the id itself rather than disappearing.
LABELS = {
    "signing_keys_registered": "Signing keys registered",
    "jwks_reachable": "JWKS URL answers",
    "jwks_has_keys": "JWKS lists keys",
    "as_metadata_reachable": "Authorization server metadata (RFC 8414) answers",
    "as_metadata_issuer": "Metadata names the configured issuer",
    "token_endpoint_https": "Token endpoint is public https",
    "grant_type_jwt_bearer": "Advertises the jwt-bearer grant",
    "auth_method_private_key_jwt": "Advertises private_key_jwt client auth",
    "dpop_algs": "DPoP algorithms are EdDSA / ES256 only",
    "prm_reachable": "Protected resource metadata (RFC 9728) answers",
    "prm_resource": "Resource metadata names the configured MCP URL",
    "prm_authorization_server": "Resource metadata names the configured issuer",
    "client_token_endpoint": "Token endpoint discovered",
    "client_accepted": "Accepts canopy as its client",
    "client_refuses_foreign_grant": "Refuses a grant it did not sign",
    "host_grants": "Acts as the visitor (host grants)",
    "canopy_client_keys": "canopy's own client keys",
    "in_process": "Answered in-process",
}

_KEY_LABELS = {
    "is_object": "is a JSON object",
    "public_only": "carries no private members",
    "asymmetric": "is an asymmetric key",
    "signing": "is a signing key",
    "kid_is_thumbprint": "kid is its RFC 7638 thumbprint",
    "type": "is Ed25519, P-256 or (assertions only) RSA",
}


@dataclass
class Row:
    name: str
    status: str   # "pass" | "fail" | "skip"
    detail: str

    @property
    def label(self) -> str:
        if self.name in LABELS:
            return LABELS[self.name]
        # `key[0]_public_only`, `pasted_key[1]_type` — which key, and what of it.
        head, sep, tail = self.name.partition("]_")
        if sep and tail in _KEY_LABELS:
            which = head.replace("pasted_key[", "Pasted key ").replace("key[", "Key ")
            return f"{which}: {_KEY_LABELS[tail]}"
        return self.name


def _fetch_json(url: str) -> dict:
    try:
        return outbound.get_json(url, what="document")
    except outbound.OutboundError as exc:
        raise fetch.FetchError(str(exc)) from exc


def _post_form(url, data, headers, what="URL"):
    try:
        return outbound.post_form(url, data, headers=headers, what=what)
    except outbound.OutboundError as exc:
        raise fetch.FetchError(str(exc)) from exc


def _rows(report: conformance.Report) -> list[Row]:
    return [Row(c.name, "pass" if c.ok else "fail", c.detail) for c in report.checks]


def _pasted_key_rows(pems, *, allow_rsa: bool) -> list[Row]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    rows = []
    for i, pem in enumerate(p for p in pems or [] if isinstance(p, str) and p.strip()):
        name = f"pasted_key[{i}]_type"
        try:
            key = serialization.load_pem_public_key(pem.strip().encode())
        except Exception:  # noqa: BLE001 - the reason is the row
            rows.append(Row(name, "fail", "not a PEM public key"))
            continue
        if isinstance(key, ed25519.Ed25519PublicKey):
            rows.append(Row(name, "pass", "Ed25519 (EdDSA)"))
        elif isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
            rows.append(Row(name, "pass", "P-256 (ES256)"))
        elif isinstance(key, rsa.RSAPublicKey):
            rows.append(Row(name, "pass" if allow_rsa else "fail",
                            "RSA — fine for visitor assertions" if allow_rsa
                            else "RSA cannot sign an ID-JAG; use Ed25519 or P-256"))
        else:
            rows.append(Row(name, "fail", f"unsupported key type {type(key).__name__}"))
    return rows


def run(app) -> list[Row]:
    """Every check this site's settings allow, in the order canopy would meet them."""
    rows: list[Row] = []
    grants = app.issues_host_grants()

    if self_host.is_self_site(app) and self_host.configured():
        rows.append(Row("in_process", "pass",
                        "this site is canopy-web itself: its documents and token endpoint are "
                        "answered in-process, exactly as canopy's own redemption reaches them"))

    # --- signing keys -----------------------------------------------------------------
    if not app.jwks_url and not app.public_keys:
        rows.append(Row("signing_keys_registered", "fail",
                        "no JWKS URL and no pasted key: nothing this site signs can be verified"))
    if app.jwks_url:
        rows.extend(_rows(conformance.check_jwks(app.jwks_url, fetch_json=_fetch_json,
                                                 allow_rsa=not grants)))
    rows.extend(_pasted_key_rows(app.public_keys, allow_rsa=not grants))

    # --- host grants --------------------------------------------------------------------
    if not grants:
        if app.host_issuer or app.host_mcp_resource:
            rows.append(Row("host_grants", "fail",
                            "set BOTH the sign-in issuer and the MCP server, or neither"))
        else:
            rows.append(Row("host_grants", "skip",
                            "not configured: the agent cannot act as a visitor on this site"))
        return rows

    rows.extend(_rows(conformance.check_metadata(app.host_issuer, app.host_mcp_resource,
                                                 fetch_json=_fetch_json)))
    if not client_identity.configured():
        rows.append(Row("canopy_client_keys", "fail",
                        "this canopy has no OAuth client keys (CANOPY_OAUTH_CLIENT_KEY / "
                        "CANOPY_OAUTH_DPOP_KEY), so it cannot be any site's client"))
        return rows
    rows.extend(_rows(conformance.check_client(
        app.host_issuer, app.host_mcp_resource, client_identity.credentials(),
        fetch_json=_fetch_json, post_form=_post_form)))
    return rows
