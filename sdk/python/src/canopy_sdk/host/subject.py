"""The host's half of the subject statement: canopy's signed word that THIS request is an
agent's turn with one of the host's own contacts, and which one.

A host site is the system of record for the people it brings to canopy. When an agent
talks to one of them (a contact — someone the host vouches for, with no canopy account and
no passkey, so no act-as-you grant), the agent may still ask the host about that one
person, through canopy's gateway, if the agent's owner marked its entry for the site
``contact_scoped``. There is no grant and no access token: canopy sends

    Authorization: CanopySubject <jws>     # typ canopy-subject+jwt, signed by canopy's client key
    DPoP: <proof>                          # htm, htu, iat, jti; no ath
    Canopy-Execution: <jws>                # as on every gateway call, now with ``sub``

and the host decides, on every call, what that person's agent may see:

    from canopy_sdk.host import SubjectRefused, verify_subject

    try:
        subject = verify_subject(request, config=config, client_keys=keys, replay_store=jtis,
                                 rpc=(method, params))
    except SubjectRefused as exc:
        ...                                  # exc.code: no_statement, unbound, replayed, ...
    if subject.sub != f"personalid:{worker.username}":
        ...                                  # THE HOST AUTHORIZES EVERY CALL AGAINST subject.sub

A Django host calls ``canopy_sdk.django.execution.verify_subject(request)``, which wires its
``HostConfig``, its jti table and a cached key resolver.

What is checked, all fail-closed:

* exactly one ``Authorization: CanopySubject``; ``typ: canopy-subject+jwt``; the signature,
  against the key canopy's client metadata names (``kid``);
* ``iss`` is the canopy this host trusts, ``aud`` is this host's MCP resource;
* it lives at most 60 seconds;
* exactly one ``DPoP`` proof, for THIS request (``htm``, ``htu`` = the host's resource,
  fresh ``iat``), carrying no ``ath``, signed by the key ``cnf.jkt`` names;
* with ``rpc``, the statement's ``rpc`` claim is this very JSON-RPC request;
* the statement's ``jti`` and the proof's ``jti`` are each spent once (``replay_store``).

``sub`` is the host's own id for the person, as the host told canopy (the visitor
assertion's ``sub``). canopy takes it from the turn's initiator, never from anything the
agent sent. What the statement cannot prove: anything about the person beyond what the
host itself asserted when they arrived.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import decode, unverified_header, verify_dpop_proof
from ..keys import public_key_for
from ..stores import MemoryJtiStore, Replayed
from .client_keys import ClientKeyResolver, MetadataError
from .execution import _get, _headers

log = logging.getLogger("canopy_sdk.host")


class SubjectRefused(Exception):
    """The subject statement is missing or cannot be believed. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Subject:
    """What canopy stated about this request, verified: an agent's turn with ONE contact."""

    #: The host site the contact came through (its host-site name in canopy).
    site: str
    #: The host's own id for the person. Authorize every call against it.
    sub: str
    #: The agent's slug, canopy's turn and conversation ids, and the entry it runs in.
    agent: str
    turn: str
    session: str
    capability: str
    claims: dict = field(repr=False, default_factory=dict)

    @property
    def jkt(self) -> str:
        """The DPoP key proved on this request: pass it to ``verify_execution(jkt=...)``,
        since no ``DPoPGate`` records a proof that rides no access token."""
        cnf = self.claims.get("cnf")
        return str(cnf.get("jkt") or "") if isinstance(cnf, Mapping) else ""

    def audit(self) -> dict:
        return {"actor": "agent", "subject": self.sub, "site": self.site, "agent": self.agent,
                "turn": self.turn, "session": self.session, "capability": self.capability}


#: Used when a caller passes none. The key resolver caches canopy's client metadata in
#: memory; the jti store is PROCESS-LOCAL — a host with more than one worker must pass a
#: shared one (``canopy_sdk.django.stores.DjangoJtiStore``), or a replay to another worker
#: would be believed.
_default_keys = ClientKeyResolver()
_default_jtis = MemoryJtiStore()


def _method(request, method: str | None) -> str:
    if method:
        return method
    value = getattr(request, "method", None)
    if isinstance(value, str) and value:
        return value
    scope = getattr(request, "scope", None)
    if isinstance(scope, Mapping) and scope.get("method"):
        return str(scope["method"])
    return "POST"   # a header mapping: MCP's Streamable HTTP request


def verify_subject(request, *, config, client_keys=None, replay_store=None, method: str | None = None,
                   rpc: tuple[str, Mapping] | None = None, resource: str | None = None,
                   now: float | None = None) -> Subject:
    """Verify the subject statement on ``request`` (anything with ``.headers`` — a Django,
    Starlette or ASGI request — or a header mapping). ``config`` is the host's ``HostConfig``
    (``canopy_client_id``, ``resource``); ``client_keys`` a ``ClientKeyResolver``;
    ``replay_store`` a ``JtiStore`` (both default to process-local ones: pass a shared store
    in a multi-worker host). ``method`` is the HTTP method the DPoP proof must name (read off
    the request when it has one, else ``POST``). ``rpc`` — ``(method, params)`` of the
    JSON-RPC request in front of the host — requires the statement to be for exactly that
    request; a host should always pass it. ``resource`` is the MCP resource the statement must
    be for (its ``aud`` and the proof's ``htu``): a surface's ``mcp_resource`` when the host
    serves one (``canopy_sdk.site``), else ``config.resource``. Returns the ``Subject`` or
    raises ``SubjectRefused``."""
    try:
        headers = _headers(request)
    except Exception as exc:  # noqa: BLE001 — ExecutionRefused: no headers at all
        raise SubjectRefused("no_statement", "the request carries no headers") from exc
    auth = _get(headers, "Authorization") or ""
    scheme, _, raw = auth.partition(" ")
    raw = raw.strip()
    if scheme.lower() != contract.SUBJECT_AUTH_SCHEME.lower() or not raw or "," in raw or " " in raw:
        raise SubjectRefused("no_statement", f"send exactly one Authorization: {contract.SUBJECT_AUTH_SCHEME}")
    client_id = getattr(config, "canopy_client_id", "")
    resource = resource or getattr(config, "resource", "")
    if not client_id or not resource:
        raise SubjectRefused("not_configured", "this host trusts no canopy (canopy_client_id, resource)")
    keys = client_keys if client_keys is not None else _default_keys
    jtis = replay_store if replay_store is not None else _default_jtis
    try:
        header = unverified_header(raw)
        if header.get("typ") != contract.SUBJECT_TYP:
            raise ContractError("bad_typ", "not a canopy subject statement")
        jwk = keys.key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(raw, key, algorithms=[header["alg"]], audience=[resource],
                        required=contract.SUBJECT_REQUIRED_CLAIMS, leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, contract.SUBJECT_MAX_LIFETIME, what="a subject statement")
        jti = contract.check_jti(claims)
    except MetadataError as exc:
        log.warning("canopy client metadata unusable: %s", exc)
        raise SubjectRefused("client_metadata", "canopy's keys could not be read") from exc
    except ContractError as exc:
        raise SubjectRefused(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise SubjectRefused("bad_issuer", "the statement is not from the canopy this host trusts")
    for name in ("sub", "site", "agent", "turn", "session"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            raise SubjectRefused("missing_claim", f"the statement names no {name}")

    cnf = claims.get("cnf") if isinstance(claims.get("cnf"), Mapping) else {}
    bound = str(cnf.get("jkt") or "")
    proofs = headers.getlist(contract.DPOP_HEADER) if hasattr(headers, "getlist") else None
    if proofs is None:
        one = _get(headers, contract.DPOP_HEADER)
        proofs = [one] if one else []
    proofs = [p for p in proofs if p]
    if len(proofs) != 1 or "," in proofs[0]:
        raise SubjectRefused("no_proof", "send exactly one DPoP proof with a subject statement")
    try:
        jkt, proof_jti, proof_iat = verify_dpop_proof(proofs[0], htm=_method(request, method), htu=resource,
                                                      access_token=None, now=now)
    except ContractError as exc:
        raise SubjectRefused("bad_proof", exc.message) from exc
    if not bound or not constant_time_equal(bound, jkt):
        raise SubjectRefused("unbound", "the statement is not bound to the key proved on this request")
    if rpc is not None and not contract.rpc_matches(claims.get("rpc"), *rpc):
        raise SubjectRefused("wrong_request", "the statement is for a different request")

    try:
        jtis.consume([(f"subject:{client_id}", jti, int(claims["exp"]) + contract.LEEWAY_SECONDS),
                      (f"dpop:{jkt}", proof_jti, proof_iat + contract.DPOP_IAT_WINDOW_SECONDS)])
    except Replayed as exc:
        raise SubjectRefused("replayed", "this statement or proof has already been used") from exc
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
        log.warning("jti store failed; refusing the subject statement", exc_info=True)
        raise SubjectRefused("jti_store_error", "the statement could not be recorded as used") from exc

    return Subject(site=str(claims["site"]), sub=str(claims["sub"]), agent=str(claims["agent"]),
                   turn=str(claims["turn"]), session=str(claims["session"]),
                   capability=str(claims.get("capability") or ""), claims=dict(claims))
