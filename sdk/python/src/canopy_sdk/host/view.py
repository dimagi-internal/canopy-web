"""The host's half of ``Canopy-View``: canopy's signed word that THIS request is the
person's own MCP Apps View reading what the host keeps for them, no agent acting.

A host that gives a person's View more than it gives an agent (Connect Labs: the rows
of a sensitive result the person's cleared agent already received) must decide that on
a POSITIVE statement, never on a missing ``Canopy-Execution``: anything that can make
canopy issue a delegated request without a statement would otherwise read as the person.

    from canopy_sdk.host import ViewRefused, verify_view

    try:
        view = verify_view(request, config=config, client_keys=keys, replay_store=jtis,
                           uri=uri_being_read, subject=str(user.pk))
    except ViewRefused as exc:
        ...                                  # exc.code: no_statement, wrong_person, ...

canopy sends it only from a View's ``resources/read``, only when the principal behind
the View request is a person acting as themselves (never an agent's turn, a caller
token or an agent's login), and only for a conversation that person is in.

What is checked, all fail-closed:

* the request carries no ``Canopy-Execution`` (an agent's call is never a View);
* the signature, against the key canopy's client metadata names (``kid``), with
  ``typ: canopy-view+jwt``, ``iss`` the trusted canopy, ``aud`` this host's resource;
* ``actor`` is ``"view"``;
* it lives at most 60 seconds and its ``jti`` is spent once (on a full verify only);
* ``cnf.jkt`` is the DPoP key proved on THIS request and ``ath`` (required) matches the
  access token it carries;
* ``sub`` is the person the host is serving (``subject``, the delegated token's subject);
* ``uri`` names the resource being read (``same_resource``: scheme, authority and path;
  the query — a page of it — may differ);
* with ``rpc``, the statement's ``rpc`` claim is this very JSON-RPC request
  (``contract.rpc_digest`` of its method and params) — not another read riding its token.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import decode, unverified_header
from ..keys import public_key_for
from ..stores import Replayed
from .asgi import presented_dpop_jkt
from .client_keys import MetadataError
from .execution import _access_token, _get, _headers

log = logging.getLogger("canopy_sdk.host")


class ViewRefused(Exception):
    """The view statement is missing or cannot be believed. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class View:
    """What canopy stated about this request, verified: the person's own View."""

    #: The host's own id for the person (the delegated grant's subject).
    subject: str
    #: The resource the View is reading.
    uri: str
    #: canopy's id for the conversation the View is in.
    session: str
    #: Informational, for the audit: canopy's id for the viewer and the conversation's agent.
    viewer: str = ""
    agent: str = ""
    claims: dict = field(repr=False, default_factory=dict)

    def audit(self) -> dict:
        return {"actor": contract.VIEW_ACTOR, "subject": self.subject, "uri": self.uri,
                "session": self.session, "viewer": self.viewer, "agent": self.agent}


def same_resource(stated: str, actual: str) -> bool:
    """Whether ``stated`` names the resource ``actual`` reads: the same scheme (any case),
    authority and path. The query is not compared — it is a page of the same resource."""
    a, b = urlsplit(str(stated or "")), urlsplit(str(actual or ""))
    if not a.scheme or not (a.netloc or a.path):
        return False
    return (a.scheme.lower() == b.scheme.lower()
            and constant_time_equal(a.netloc, b.netloc)
            and constant_time_equal(a.path.rstrip("/"), b.path.rstrip("/")))


def verify_view(request, *, config, client_keys, replay_store, uri: str, subject: str,
                jkt: str | None = None, access_token: str | None = None,
                rpc: tuple[str, Mapping] | None = None) -> View:
    """Verify the ``Canopy-View`` statement on ``request`` (anything with ``.headers``, or
    a header mapping) for a read of ``uri`` as ``subject``. ``config`` is the host's
    ``HostConfig``; ``client_keys`` a ``ClientKeyResolver``; ``replay_store`` a
    ``JtiStore``. ``jkt`` defaults to the key ``DPoPGate`` proved on this request,
    ``access_token`` to the one in ``Authorization``. ``rpc`` — ``(method, params)`` of
    the JSON-RPC request in front of the host — requires the statement to be for exactly
    that request (``contract.rpc_digest``); a host should always pass it. Returns the
    ``View`` or raises ``ViewRefused``."""
    headers = _headers_or_refuse(request)
    if _get(headers, contract.EXECUTION_HEADER):
        raise ViewRefused("agent_call", "an agent's call is never the person's own View")
    raw = _get(headers, contract.VIEW_HEADER)
    if not raw or "," in raw:
        raise ViewRefused("no_statement", f"send exactly one {contract.VIEW_HEADER}")
    client_id = getattr(config, "canopy_client_id", "")
    resource = getattr(config, "resource", "")
    if not client_id or not resource:
        raise ViewRefused("not_configured", "this host trusts no canopy (canopy_client_id, resource)")
    try:
        header = unverified_header(raw)
        if header.get("typ") != contract.VIEW_TYP:
            raise ContractError("bad_typ", "not a canopy view statement")
        jwk = client_keys.key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(raw, key, algorithms=[header["alg"]], audience=[resource],
                        required=contract.VIEW_REQUIRED_CLAIMS, leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, contract.VIEW_MAX_LIFETIME, what="a view statement")
        jti = contract.check_jti(claims)
    except MetadataError as exc:
        log.warning("canopy client metadata unusable: %s", exc)
        raise ViewRefused("client_metadata", "canopy's keys could not be read") from exc
    except ContractError as exc:
        raise ViewRefused(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise ViewRefused("bad_issuer", "the statement is not from the canopy this host trusts")
    if claims.get("actor") != contract.VIEW_ACTOR:
        raise ViewRefused("bad_actor", "the statement does not say the person's own View is reading")

    proved = jkt if jkt is not None else presented_dpop_jkt.get()
    cnf = claims.get("cnf") if isinstance(claims.get("cnf"), Mapping) else {}
    if not proved or not constant_time_equal(str(cnf.get("jkt") or ""), proved):
        raise ViewRefused("unbound", "the statement is not bound to the key proved on this request")
    token = access_token if access_token is not None else _access_token(headers)
    if not token or not constant_time_equal(str(claims.get("ath") or ""), contract.ath(token)):
        raise ViewRefused("unbound", "the statement is for a different access token")
    if not subject or not constant_time_equal(str(claims.get("sub") or ""), str(subject)):
        raise ViewRefused("wrong_person", "the statement is for another person")
    if not same_resource(str(claims.get("uri") or ""), uri):
        raise ViewRefused("wrong_resource", "the statement is for another resource")
    if rpc is not None and not constant_time_equal(str(claims.get("rpc") or ""),
                                                   contract.rpc_digest(*rpc)):
        raise ViewRefused("wrong_request", "the statement is for a different request")
    if not str(claims.get("session") or ""):
        raise ViewRefused("missing_claim", "the statement names no conversation")

    try:
        replay_store.consume([(f"view:{client_id}", jti, int(claims["exp"]) + contract.LEEWAY_SECONDS)])
    except Replayed as exc:
        raise ViewRefused("replayed", "this statement has already been used") from exc
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
        log.warning("jti store failed; refusing the view statement", exc_info=True)
        raise ViewRefused("jti_store_error", "the statement could not be recorded as used") from exc

    return View(subject=str(claims["sub"]), uri=str(claims["uri"]), session=str(claims["session"]),
                viewer=str(claims.get("viewer") or ""), agent=str(claims.get("agent") or ""),
                claims=dict(claims))


def _headers_or_refuse(request) -> Mapping:
    from .execution import ExecutionRefused

    try:
        return _headers(request)
    except ExecutionRefused as exc:
        raise ViewRefused(exc.code, exc.message) from exc
