"""The LIVE grant: a real ID-JAG from the host's probe endpoint, redeemed and used.

``check_client`` proves the host accepts canopy as its client; it cannot prove a
grant is issued, redeemed and honoured, because that needs an ID-JAG signed by
the host's key. A host with a probe identity (``canopy_sdk.host.ProbeIdentity``)
signs one on request — for its dedicated probe principal only — so this module
can walk the whole chain:

* ``request_probe(issuer, credentials)`` — discover ``canopy_probe_endpoint``
  from the host's RFC 8414 metadata and ask it for a probe ID-JAG, authenticated
  with ``private_key_jwt`` and a DPoP proof. Returns a ``ProbeGrant``.
* ``check_live_grant(issuer, resource, credentials)`` — that, then redeem the
  ID-JAG through the NORMAL jwt-bearer path. Returns the report and a
  ``LiveGrant`` (the token material) for the caller to exercise.
* ``check_probe_tool`` — (a) the probe's own tool, called with the token and a
  fresh proof, succeeds;
* ``check_out_of_scope_refused`` — (b) a tool outside the probe scope is not
  listed and is refused when called;
* ``check_requires_dpop`` — (c) the same call without a valid DPoP proof (none,
  a stranger's key, the token as a plain bearer) is refused.
* ``run_live`` — all of it as one ``Report``.

canopy-web does NOT use the MCP helpers here: it redeems with its own storage
and calls through its own gateway (the path a visitor's turn takes), which is
the point of probing. These are for a host's CI and for any other consumer.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import __version__, consumer, contract, fetch
from .checks import MCP_PROTOCOL_VERSION, Report, _parse_mcp

#: The name asked for when the host names no ``denied_tool``: no server offers
#: it, so a refusal proves only that unknown names are refused.
SYNTHETIC_DENIED_TOOL = "canopy_probe_out_of_scope"


class ProbeError(consumer.ConsumerError):
    """The probe could not be obtained. ``code`` is stable: ``no_probe_endpoint``
    (the host advertises none — not configured, which is not a failure of the
    chain), ``metadata_unreachable``, ``bad_probe_endpoint``,
    ``probe_unreachable``, ``probe_refused``, ``bad_probe_response``."""


@dataclass
class ProbeGrant:
    """What the host's probe endpoint answered."""

    endpoint: str
    id_jag: str = field(repr=False)
    subject: str
    scope: str
    resource: str
    tool: str
    arguments: dict
    denied_tool: str = ""
    page: str = ""


@dataclass
class LiveGrant:
    probe: ProbeGrant
    token: consumer.TokenResponse


def _post(post_form):
    return post_form or (lambda url, data, headers, what="": fetch.post_form(url, data, headers=headers))


def discover_probe_endpoint(issuer: str, *, fetch_json: Callable[[str], dict] | None = None) -> str:
    """The probe endpoint the host's RFC 8414 document names, vetted. Raises
    ``ProbeError('no_probe_endpoint')`` when it names none."""
    get = fetch_json or fetch.get_json
    issuer = contract.normalize_url(issuer)
    try:
        doc = get(contract.metadata_url(issuer))
    except fetch.FetchError as exc:
        raise ProbeError("metadata_unreachable", str(exc)) from exc
    try:
        consumer.validate_authorization_server_metadata(doc, issuer)
    except consumer.RedemptionRefused as exc:
        raise ProbeError(exc.code, exc.message) from exc
    endpoint = str(doc.get(contract.PROBE_ENDPOINT_METADATA_FIELD) or "").strip()
    if not endpoint:
        raise ProbeError("no_probe_endpoint",
                         f"the host's metadata names no {contract.PROBE_ENDPOINT_METADATA_FIELD}")
    return endpoint


def request_probe(issuer: str, credentials: consumer.ClientCredentials, *,
                  fetch_json: Callable[[str], dict] | None = None, post_form: Callable | None = None,
                  vet: Callable[[str], object] | None = None) -> ProbeGrant:
    """Ask the host's probe endpoint for a probe ID-JAG, as canopy.

    ``vet(url)`` checks the endpoint before anything is sent to it (default:
    ``fetch.vet_url``; canopy-web passes its own outbound guard). The answer's
    ID-JAG is checked for SHAPE only (``typ``, the probe claim, that it names
    the subject and scope the answer does) — its signature is the redeemer's
    to verify, against the keys it has registered for this site."""
    import jwt

    issuer = contract.normalize_url(issuer)
    endpoint = discover_probe_endpoint(issuer, fetch_json=fetch_json)
    try:
        (vet or fetch.vet_url)(endpoint)
    except Exception as exc:  # noqa: BLE001 - FetchError, or the caller's guard
        raise ProbeError("bad_probe_endpoint", str(exc)) from exc
    form = {"client_id": credentials.client_id, "client_assertion_type": contract.CLIENT_ASSERTION_TYPE,
            "client_assertion": credentials.client_assertion(issuer)}
    try:
        status, body, _ = _post(post_form)(endpoint, form,
                                           headers={contract.DPOP_HEADER: credentials.dpop_proof("POST", endpoint)},
                                           what="probe endpoint")
    except Exception as exc:  # noqa: BLE001 - FetchError or the caller's transport error
        raise ProbeError("probe_unreachable", f"{type(exc).__name__}: {exc}") from exc
    body = body if isinstance(body, dict) else {}
    if status == 404:
        raise ProbeError("no_probe_endpoint", "the host's probe endpoint answered 404: no probe is configured")
    if status != 200:
        raise ProbeError("probe_refused", f"the probe endpoint refused ({status} {body.get('error') or 'error'})")
    id_jag = body.get("id_jag")
    if not isinstance(id_jag, str) or not id_jag:
        raise ProbeError("bad_probe_response", "the probe endpoint returned no id_jag")
    try:
        header = jwt.get_unverified_header(id_jag)
        claims = jwt.decode(id_jag, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001
        raise ProbeError("bad_probe_response", "the probe's id_jag is not a readable JWT") from exc
    if header.get("typ") != contract.ID_JAG_TYP:
        raise ProbeError("bad_probe_response", f"the probe's id_jag must have typ {contract.ID_JAG_TYP!r}")
    if claims.get(contract.PROBE_CLAIM) is not True:
        raise ProbeError("bad_probe_response", f"the probe's id_jag must carry {contract.PROBE_CLAIM}: true")
    subject, scope = str(body.get("subject") or ""), str(body.get("scope") or "")
    if str(claims.get("sub") or "") != subject or contract.scopes_of(claims.get("scope")) != [scope]:
        raise ProbeError("bad_probe_response", "the probe's id_jag names a different subject or scope "
                                               "than the probe endpoint said")
    tool = str(body.get("tool") or "")
    arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
    if not tool:
        raise ProbeError("bad_probe_response", "the probe endpoint named no tool")
    return ProbeGrant(endpoint=endpoint, id_jag=id_jag, subject=subject, scope=scope,
                      resource=str(body.get("resource") or ""), tool=tool, arguments=arguments,
                      denied_tool=str(body.get("denied_tool") or ""), page=str(body.get("page") or ""))


def check_live_grant(issuer: str, resource: str, credentials: consumer.ClientCredentials, *,
                     fetch_json: Callable[[str], dict] | None = None,
                     post_form: Callable | None = None) -> tuple[Report, LiveGrant | None]:
    """Probe, then redeem the probe's ID-JAG through the normal jwt-bearer path."""
    report = Report()
    try:
        probe = request_probe(issuer, credentials, fetch_json=fetch_json, post_form=post_form)
    except ProbeError as exc:
        report.add("probe_issued", False, f"{exc.code}: {exc.message}")
        return report, None
    report.add("probe_issued", True, f"subject {probe.subject!r}, scope {probe.scope!r}, tool {probe.tool!r}")
    report.add("probe_resource", contract.normalize_url(probe.resource) == contract.normalize_url(resource),
               "the probe must be for the configured MCP resource")
    try:
        token = consumer.redeem_id_jag(probe.id_jag, issuer=issuer, resource=resource, credentials=credentials,
                                       fetch_json=fetch_json, post_form=_post(post_form))
    except consumer.RedemptionRefused as exc:
        report.add("live_grant_redeemed", False, f"{exc.code}: {exc.message}")
        return report, None
    report.add("live_grant_redeemed", True, f"scope {token.scope!r}, expires_in {token.expires_in}")
    report.add("live_grant_scope", contract.scopes_of(token.scope) == [probe.scope],
               f"the token must carry exactly the probe scope (got {token.scope!r})")
    return report, LiveGrant(probe=probe, token=token)


# --- the MCP side ----------------------------------------------------------------------


class _Session:
    """One Streamable-HTTP MCP session with a DPoP-bound token."""

    def __init__(self, resource: str, live: LiveGrant, credentials: consumer.ClientCredentials, post):
        self.resource = resource
        self.token = live.token.access_token
        self.credentials = credentials
        self.post = post
        self.session = ""
        self.ids = 0

    def headers(self, *, proof: str | None = "fresh", scheme: str = "DPoP") -> dict:
        out = {"Accept": "application/json, text/event-stream", "Authorization": f"{scheme} {self.token}",
               "MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
        if proof == "fresh":
            proof = self.credentials.dpop_proof("POST", self.resource, access_token=self.token)
        if proof is not None:
            out[contract.DPOP_HEADER] = proof
        if self.session:
            out["Mcp-Session-Id"] = self.session
        return out

    def rpc(self, method: str, params: dict | None = None, **header_kwargs):
        self.ids += 1
        payload = {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}}
        status, raw, resp_headers = self.post(self.resource, payload, headers=self.headers(**header_kwargs))
        return status, _parse_mcp(raw, resp_headers), resp_headers

    def open(self) -> tuple[int, dict | None]:
        status, doc, resp_headers = self.rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "canopy-probe", "version": __version__}})
        self.session = next((v for k, v in resp_headers.items() if k.lower() == "mcp-session-id"), "")
        if status == 200:
            self.post(self.resource, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers=self.headers())
        return status, doc


def _call_failed(status: int, doc: dict | None) -> bool:
    """A tools/call the server refused: an HTTP error, a JSON-RPC error, or a
    result flagged ``isError``."""
    if status != 200 or not doc:
        return True
    if "error" in doc:
        return True
    return bool((doc.get("result") or {}).get("isError"))


def check_probe_tool(resource: str, live: LiveGrant, credentials: consumer.ClientCredentials, *,
                     post_json: Callable | None = None) -> Report:
    """(a) The probe's tool, with its arguments, succeeds as the probe principal."""
    report = Report()
    mcp = _Session(resource, live, credentials, post_json or fetch.post_json)
    try:
        status, doc = mcp.open()
        if not report.add("probe_mcp_initialize", status == 200 and bool(doc) and "result" in doc,
                          f"HTTP {status}"):
            return report
        status, doc, _ = mcp.rpc("tools/call", {"name": live.probe.tool, "arguments": live.probe.arguments})
    except fetch.FetchError as exc:
        report.add("probe_tool_succeeds", False, str(exc))
        return report
    ok = not _call_failed(status, doc)
    report.add("probe_tool_succeeds", ok,
               f"{live.probe.tool}: HTTP {status}" + ("" if ok else f" — {_why(doc)}"))
    return report


def check_out_of_scope_refused(resource: str, live: LiveGrant, credentials: consumer.ClientCredentials, *,
                               post_json: Callable | None = None) -> Report:
    """(b) A tool outside the probe scope is neither listed nor callable."""
    report = Report()
    denied = live.probe.denied_tool or SYNTHETIC_DENIED_TOOL
    mcp = _Session(resource, live, credentials, post_json or fetch.post_json)
    try:
        status, doc = mcp.open()
        if status != 200:
            report.add("out_of_scope_refused", False, f"initialize answered HTTP {status}")
            return report
        status, doc, _ = mcp.rpc("tools/list")
        names = [t.get("name") for t in (((doc or {}).get("result") or {}).get("tools") or [])
                 if isinstance(t, dict)]
        report.add("out_of_scope_not_listed", denied not in names,
                   f"{denied} must not be listed to a {live.probe.scope} token")
        status, doc, _ = mcp.rpc("tools/call", {"name": denied, "arguments": {}})
    except fetch.FetchError as exc:
        report.add("out_of_scope_refused", False, str(exc))
        return report
    detail = f"{denied}: HTTP {status}"
    if not live.probe.denied_tool:
        detail += " (the host names no denied_tool; a name no server offers was asked for)"
    report.add("out_of_scope_refused", _call_failed(status, doc), detail)
    return report


def check_requires_dpop(resource: str, live: LiveGrant, credentials: consumer.ClientCredentials, *,
                        post_json: Callable | None = None) -> Report:
    """(c) The probe's call without a valid DPoP proof is refused: with no proof,
    with a proof by a key the token is not bound to, and as a plain bearer."""
    from ..keys import generate_private_key

    report = Report()
    post = post_json or fetch.post_json
    mcp = _Session(resource, live, credentials, post)
    call = {"jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": live.probe.tool, "arguments": live.probe.arguments}}
    stranger = consumer.dpop_proof(generate_private_key("EdDSA"), "POST", resource,
                                   access_token=live.token.access_token)
    for name, headers in (
            ("dpop_required_no_proof", mcp.headers(proof=None)),
            ("dpop_required_wrong_key", mcp.headers(proof=stranger)),
            ("dpop_required_not_bearer", mcp.headers(proof=None, scheme="Bearer"))):
        try:
            status, raw, resp_headers = post(resource, call, headers=headers)
        except fetch.FetchError as exc:
            report.add(name, False, str(exc))
            continue
        # 401 is the contract; any refusal is acceptable, a result is not.
        report.add(name, _call_failed(status, _parse_mcp(raw, resp_headers)), f"HTTP {status}")
    return report


def run_live(issuer: str, resource: str, credentials: consumer.ClientCredentials, *,
             fetch_json: Callable[[str], dict] | None = None, post_form: Callable | None = None,
             post_json: Callable | None = None) -> Report:
    """The whole live chain as one report: probe → redeem → (a) → (b) → (c)."""
    report, live = check_live_grant(issuer, resource, credentials, fetch_json=fetch_json, post_form=post_form)
    if live is None:
        return report
    report.extend(check_probe_tool(resource, live, credentials, post_json=post_json))
    report.extend(check_out_of_scope_refused(resource, live, credentials, post_json=post_json))
    report.extend(check_requires_dpop(resource, live, credentials, post_json=post_json))
    return report


def _why(doc: dict | None) -> str:
    if not doc:
        return "no JSON-RPC answer"
    if "error" in doc:
        return str((doc["error"] or {}).get("message") or "error")[:200]
    content = ((doc.get("result") or {}).get("content") or [])
    text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
    return (text or json.dumps(doc.get("result"))[:200])[:200]
