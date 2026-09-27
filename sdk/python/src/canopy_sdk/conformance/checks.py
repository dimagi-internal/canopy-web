"""The checks behind ``canopy_sdk.conformance``."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import __version__, consumer, contract, fetch

MCP_PROTOCOL_VERSION = "2025-06-18"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def extend(self, other: Report) -> Report:
        self.checks.extend(other.checks)
        return self

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def __str__(self) -> str:
        lines = [f"{'PASS' if c.ok else 'FAIL'}  {c.name}" + (f" — {c.detail}" if c.detail else "")
                 for c in self.checks]
        return "\n".join(lines) or "(no checks ran)"

    def raise_for_failures(self) -> None:
        if not self.ok:
            raise AssertionError("canopy conformance failed:\n" + str(self))


# --- discovery ------------------------------------------------------------------------


def check_metadata(issuer: str, resource: str, *, fetch_json: Callable[[str], dict] | None = None) -> Report:
    """The host's RFC 8414 + RFC 9728 documents, as canopy reads them."""
    get = fetch_json or fetch.get_json
    report = Report()
    issuer = contract.normalize_url(issuer)

    url = contract.metadata_url(issuer)
    try:
        doc = get(url)
    except fetch.FetchError as exc:
        report.add("as_metadata_reachable", False, f"{url}: {exc}")
        doc = None
    if doc is not None:
        report.add("as_metadata_reachable", True, url)
        report.add("as_metadata_issuer", contract.normalize_url(str(doc.get("issuer") or "")) == issuer,
                   "the document must name the issuer canopy is configured with (RFC 9207)")
        endpoint = str(doc.get("token_endpoint") or "")
        try:
            fetch.vet_url(endpoint)
            report.add("token_endpoint_https", True, endpoint)
        except fetch.FetchError as exc:
            report.add("token_endpoint_https", False, f"{endpoint!r}: {exc}")
        report.add("grant_type_jwt_bearer", contract.JWT_BEARER_GRANT in (doc.get("grant_types_supported") or []),
                   "grant_types_supported must list the jwt-bearer grant")
        report.add("auth_method_private_key_jwt",
                   contract.TOKEN_ENDPOINT_AUTH_METHOD in (doc.get("token_endpoint_auth_methods_supported") or []),
                   "token_endpoint_auth_methods_supported must list private_key_jwt")
        algs = doc.get("dpop_signing_alg_values_supported") or []
        report.add("dpop_algs", bool(algs) and set(algs) <= set(contract.GRANT_ALGORITHMS),
                   f"dpop_signing_alg_values_supported must be a non-empty subset of "
                   f"{list(contract.GRANT_ALGORITHMS)} (got {algs})")

    prm_url = contract.protected_resource_metadata_url(resource)
    try:
        prm = get(prm_url)
    except fetch.FetchError as exc:
        report.add("prm_reachable", False, f"{prm_url}: {exc}")
        return report
    report.add("prm_reachable", True, prm_url)
    report.add("prm_resource", contract.normalize_url(str(prm.get("resource") or ""))
               == contract.normalize_url(resource), "the document must name this resource")
    servers = [contract.normalize_url(str(s)) for s in (prm.get("authorization_servers") or [])]
    report.add("prm_authorization_server", issuer in servers,
               "authorization_servers must include the issuer")
    return report


def check_jwks(jwks_url: str, *, fetch_json: Callable[[str], dict] | None = None,
               allow_rsa: bool = False) -> Report:
    """The host's published signing keys. RSA is tolerated only for a key that
    signs visitor ASSERTIONS alone (``allow_rsa``); an ID-JAG key must be EdDSA
    or ES256."""
    get = fetch_json or fetch.get_json
    report = Report()
    try:
        doc = get(jwks_url)
    except fetch.FetchError as exc:
        report.add("jwks_reachable", False, f"{jwks_url}: {exc}")
        return report
    report.add("jwks_reachable", True, jwks_url)
    keys = doc.get("keys")
    if not report.add("jwks_has_keys", isinstance(keys, list) and bool(keys), "keys must be a non-empty list"):
        return report
    for i, jwk in enumerate(keys):
        label = f"key[{i}]"
        if not isinstance(jwk, dict):
            report.add(f"{label}_is_object", False)
            continue
        report.add(f"{label}_public_only", not (contract.PRIVATE_JWK_MEMBERS & set(jwk)),
                   "a published key must carry no private members")
        kty = jwk.get("kty")
        allowed = {"OKP", "EC"} | ({"RSA"} if allow_rsa else set())
        report.add(f"{label}_asymmetric", kty in allowed, f"kty {kty!r}")
        report.add(f"{label}_signing", jwk.get("use") in (None, "sig"), "use must be sig")
        if kty in ("OKP", "EC"):
            try:
                ok = jwk.get("kid") == contract.jwk_thumbprint(jwk)
                report.add(f"{label}_kid_is_thumbprint", ok,
                           "kid should be the RFC 7638 thumbprint so a rotation has something to select on")
            except contract.ContractError as exc:
                report.add(f"{label}_kid_is_thumbprint", False, exc.message)
    return report


# --- the grant ------------------------------------------------------------------------------


def check_grant(issuer: str, resource: str, *, id_jag: str, credentials: consumer.ClientCredentials,
                fetch_json: Callable[[str], dict] | None = None,
                post_form: Callable | None = None) -> tuple[Report, consumer.TokenResponse | None]:
    """Redeem ``id_jag`` as canopy would, then replay it (which must be refused).

    ``credentials.client_id`` must be a client the host accepts, and its CIMD
    must be reachable BY THE HOST. Returns the report and, on success, the
    token (for ``check_mcp``)."""
    report = Report()
    post = post_form or (lambda url, data, headers, what="": fetch.post_form(url, data, headers=headers))
    try:
        token = consumer.redeem_id_jag(id_jag, issuer=issuer, resource=resource, credentials=credentials,
                                       fetch_json=fetch_json, post_form=post)
    except consumer.RedemptionRefused as exc:
        report.add("grant_redeemed", False, f"{exc.code}: {exc.message}")
        return report, None
    report.add("grant_redeemed", True, f"scope {token.scope!r}, expires_in {token.expires_in}")
    report.add("grant_scope_present", bool(token.scope), "the token response should name its scope")

    try:
        consumer.redeem_id_jag(id_jag, issuer=issuer, resource=resource, credentials=credentials,
                               fetch_json=fetch_json, post_form=post)
        report.add("grant_single_use", False, "a replayed ID-JAG was redeemed a second time")
    except consumer.RedemptionRefused as exc:
        report.add("grant_single_use", exc.code == "redeem_refused", f"{exc.code}: {exc.message}")
    return report, token


def check_client(issuer: str, resource: str, credentials: consumer.ClientCredentials, *,
                 fetch_json: Callable[[str], dict] | None = None,
                 post_form: Callable | None = None) -> Report:
    """Whether the host accepts ``credentials`` as its client — with no grant to spend.

    For an operator who holds canopy's client keys but not the host's signing
    key (so ``check_grant`` is out of reach). It sends the jwt-bearer grant
    exactly as canopy would — the real ``private_key_jwt`` client assertion and
    a real DPoP proof — carrying an ID-JAG signed by a THROWAWAY key the host
    cannot know. A conforming host authenticates the client first (which means
    fetching canopy's metadata document and JWKS) and only then looks at the
    grant, so:

    * ``invalid_grant`` — the client got through; the host refused the grant, as
      it must;
    * ``invalid_client`` — the host does not accept this client (not
      allowlisted, or it could not read or match canopy's keys);
    * a 200 — the host accepted a grant signed by a key it never issued, which
      is the failure this check exists to find.

    Nothing is spent at the host: every ``jti`` is consumed only after every
    check passed, and this grant fails one.
    """
    from ..jose import new_jti, sign
    from ..keys import generate_private_key, public_jwk

    get = fetch_json or fetch.get_json
    post = post_form or (lambda url, data, headers, what="": fetch.post_form(url, data, headers=headers))
    report = Report()
    issuer = contract.normalize_url(issuer)
    try:
        doc = get(contract.metadata_url(issuer))
        endpoint = consumer.validate_authorization_server_metadata(doc, issuer)
        fetch.vet_url(endpoint)
    except (fetch.FetchError, consumer.RedemptionRefused) as exc:
        report.add("client_token_endpoint", False, f"the token endpoint could not be discovered: {exc}")
        return report
    report.add("client_token_endpoint", True, endpoint)

    throwaway = generate_private_key("EdDSA")
    now = int(time.time())
    probe = sign({
        "iss": issuer, "aud": issuer, "sub": "canopy-conformance-probe",
        "client_id": credentials.client_id, "resource": resource, "scope": "",
        "iat": now, "exp": now + 60, "jti": new_jti(),
    }, throwaway, headers={"typ": contract.ID_JAG_TYP, "kid": public_jwk(throwaway)["kid"]})
    try:
        status, body = consumer.request_token(
            post, endpoint, consumer.redemption_form(probe, client_id=credentials.client_id, resource=resource),
            audience=issuer, client_assertion=credentials.client_assertion, dpop_proof=credentials.dpop_proof)
    except fetch.FetchError as exc:
        report.add("client_accepted", False, f"the token endpoint could not be reached: {exc}")
        return report
    error = str((body or {}).get("error") or "") if isinstance(body, dict) else ""
    if status == 200:
        report.add("client_accepted", True, "canopy authenticated")
        report.add("client_refuses_foreign_grant", False,
                   "the host issued a token for an ID-JAG signed by a key it never held")
    elif error == "invalid_grant":
        report.add("client_accepted", True, "canopy authenticated; the probe grant was refused, as it must be")
        report.add("client_refuses_foreign_grant", True, "invalid_grant")
    elif error == "invalid_client":
        report.add("client_accepted", False,
                   "invalid_client: the host does not accept this client_id, or could not read or "
                   "match its keys")
    else:
        report.add("client_accepted", False, f"HTTP {status} {error or 'no OAuth error code'}")
    return report


# --- MCP ----------------------------------------------------------------------------------------


def _parse_mcp(raw: bytes, headers: dict) -> dict | None:
    """A JSON-RPC response from a JSON or SSE body."""
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    doc = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(doc, dict) and ("result" in doc or "error" in doc):
                    return doc
        return None
    try:
        doc = json.loads(text or "null")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def check_mcp(resource: str, access_token: str, credentials: consumer.ClientCredentials, *,
              post_json: Callable | None = None) -> Report:
    """One DPoP-authenticated MCP session: ``initialize``, ``tools/list``. Then:
    the token as a plain ``Bearer`` must be refused, and so must a replayed
    proof."""
    post = post_json or fetch.post_json
    report = Report()

    def headers(proof: str | None, *, scheme: str = "DPoP", session: str = "") -> dict:
        out = {"Accept": "application/json, text/event-stream",
               "Authorization": f"{scheme} {access_token}",
               "MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
        if proof is not None:
            out[contract.DPOP_HEADER] = proof
        if session:
            out["Mcp-Session-Id"] = session
        return out

    def proof() -> str:
        return credentials.dpop_proof("POST", resource, access_token=access_token)

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "canopy-conformance", "version": __version__}}}
    first_proof = proof()
    try:
        status, raw, resp_headers = post(resource, init, headers=headers(first_proof))
    except fetch.FetchError as exc:
        report.add("mcp_initialize", False, str(exc))
        return report
    doc = _parse_mcp(raw, resp_headers)
    if not report.add("mcp_initialize", status == 200 and bool(doc) and "result" in doc,
                      f"HTTP {status}"):
        return report
    session = next((v for k, v in resp_headers.items() if k.lower() == "mcp-session-id"), "")

    post(resource, {"jsonrpc": "2.0", "method": "notifications/initialized"},
         headers=headers(proof(), session=session))
    status, raw, resp_headers = post(resource, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                     headers=headers(proof(), session=session))
    doc = _parse_mcp(raw, resp_headers)
    tools = ((doc or {}).get("result") or {}).get("tools") if doc else None
    report.add("mcp_tools_list", status == 200 and isinstance(tools, list),
               f"HTTP {status}, {len(tools or [])} tools visible to this token")

    status, _, _ = post(resource, init, headers=headers(None, scheme="Bearer"))
    report.add("mcp_refuses_bound_token_as_bearer", status == 401, f"HTTP {status}")
    status, _, _ = post(resource, init, headers=headers(first_proof))
    report.add("mcp_refuses_replayed_proof", status == 401, f"HTTP {status}")
    return report


def run(issuer: str, resource: str, *, jwks_url: str = "", id_jag: str = "",
        credentials: consumer.ClientCredentials | None = None,
        fetch_json: Callable[[str], dict] | None = None, post_form: Callable | None = None,
        post_json: Callable | None = None) -> Report:
    """Every check the arguments allow: with ``credentials`` but no ``id_jag``,
    ``check_client``; with both, the grant and MCP round trip."""
    report = check_metadata(issuer, resource, fetch_json=fetch_json)
    if jwks_url:
        report.extend(check_jwks(jwks_url, fetch_json=fetch_json))
    if credentials is not None and not id_jag:
        report.extend(check_client(issuer, resource, credentials, fetch_json=fetch_json, post_form=post_form))
    if id_jag and credentials is not None:
        grant_report, token = check_grant(issuer, resource, id_jag=id_jag, credentials=credentials,
                                          fetch_json=fetch_json, post_form=post_form)
        report.extend(grant_report)
        if token is not None:
            report.extend(check_mcp(resource, token.access_token, credentials, post_json=post_json))
    return report
