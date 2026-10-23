"""The host's half of ``Canopy-Execution``: verify canopy's signed statement about
the call in front of it, then decide whether to release sensitive data.

canopy's gateway adds ``Canopy-Execution: <JWT>`` to every request it makes to a
host's MCP server on an agent's turn (spec 2026-10-22-sensitive-data-agents-design
§6). It says which agent is calling, in which capability and whether its
interface marks it as handling sensitive data (`data: sensitive`, and who set that), on which runner (its flags in force, and who declared them), and in
which conversation with which requirements. ``Canopy-Actor`` only NAMES the agent
and must never be trusted; this is signed by canopy's published client key.

    from canopy_sdk.host import ExecutionRefused, verify_execution

    try:
        execution = verify_execution(request, config=config, client_keys=keys,
                                     replay_store=jtis)
    except ExecutionRefused as exc:
        ...                                  # exc.code: no_statement, bad_signature, replayed...
    refusal = execution.sensitive_clearance(agents={"labsdata"})
    if refusal:
        ...                                  # synthetic data only, and say why

A Django host calls ``canopy_sdk.django.verify_execution(request)``, which wires
its configured ``HostConfig``, its jti table and a cached key resolver.

What is checked, all fail-closed:

* the signature, against the key canopy's client metadata names (``kid``), with
  ``typ: canopy-execution+jwt``;
* ``iss`` is the canopy this host trusts, ``aud`` is this host's MCP resource;
* it lives at most 60 seconds and its ``jti`` is spent once;
* ``cnf.jkt`` is the DPoP key proved on THIS request (``DPoPGate`` records it in
  ``presented_dpop_jkt``), and ``ath`` matches the access token the request
  carries — so a statement lifted from one call cannot ride another.

What it does NOT prove, and the statement cannot: that the runner's Claude traffic
really uses a zero-data-retention key, or that the agent behaves as its author
declared. Both are attestations a person made and canopy recorded; the statement
says who made them and when.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import decode, unverified_header
from ..keys import public_key_for
from ..stores import Replayed
from .asgi import presented_dpop_jkt
from .client_keys import MetadataError

log = logging.getLogger("canopy_sdk.host")

SENSITIVE = "sensitive"
SENSITIVE_DATA = "sensitive_data"


class ExecutionRefused(Exception):
    """The statement is missing or cannot be believed. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Execution:
    """What canopy stated about this call, verified."""

    agent: str
    #: ``{data, capability, declared_by, declared_at}`` — the capability's
    #: ``data: sensitive`` setting and who last published the interface holding it — or
    #: None: the capability does not handle sensitive data.
    declaration: dict | None
    #: ``{id, name, flags: [{flag, declared_by, declared_at}]}`` — flags IN FORCE.
    runner: dict
    #: ``{data_mode, config_hash}``: what the runner last reported about itself.
    runner_posture: dict
    requirements: tuple[str, ...]
    turn: str
    session: str
    claims: dict = field(repr=False, default_factory=dict)
    #: No longer stated: canopy runs the agent's current code, no pinned commit.
    #: Kept, always "" from a current canopy, so a host that logs it still reads.
    agent_sha: str = ""

    @property
    def runner_flags(self) -> frozenset[str]:
        return frozenset(str(f.get("flag") or "") for f in self.runner.get("flags") or []
                         if isinstance(f, Mapping))

    def sensitive_clearance(self, *, agents: Iterable[str], require: Iterable[str] = (SENSITIVE_DATA,)) -> str | None:
        """None when this call may be given real (sensitive) data; else the refusal code.

        The conjunction of the spec's rule plus the host's own allowlist: the agent
        is one the host accepts the claim from; its interface marks the capability
        as handling sensitive data (``data: sensitive``); the runner is a
        sensitive-data runner — it carries every required flag, declared by a
        person and in force, and reports ``data_mode: sensitive_data`` itself;
        and the conversation has required them (from its first turn — canopy
        never drops a requirement).
        """
        need = frozenset(require)
        if self.agent not in set(agents):
            return "agent_not_allowed"
        decl = self.declaration
        if not isinstance(decl, Mapping) or decl.get("data") != SENSITIVE:
            return "undeclared"
        if not need <= self.runner_flags:
            return "runner_flags"
        if SENSITIVE_DATA in need and self.runner_posture.get("data_mode") != SENSITIVE_DATA:
            return "runner_posture"
        if not need <= frozenset(self.requirements):
            return "requirements"
        return None


def _headers(request) -> Mapping:
    if isinstance(request, Mapping):
        return request
    headers = getattr(request, "headers", None)
    if headers is None:
        raise ExecutionRefused("no_statement", "the request carries no headers")
    return headers


def _get(headers: Mapping, name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def _access_token(headers: Mapping) -> str | None:
    auth = _get(headers, "Authorization") or ""
    scheme, _, token = auth.partition(" ")
    return token.strip() if scheme.lower() in ("bearer", "dpop") and token.strip() else None


def verify_execution(request, *, config, client_keys, replay_store, jkt: str | None = None,
                     access_token: str | None = None, rpc: tuple[str, Mapping] | None = None,
                     now: float | None = None) -> Execution:
    """Verify the ``Canopy-Execution`` statement on ``request`` (anything with
    ``.headers``, or a header mapping). ``config`` is the host's ``HostConfig``
    (``canopy_client_id``, ``resource``); ``client_keys`` a ``ClientKeyResolver``;
    ``replay_store`` a ``JtiStore``. ``jkt`` defaults to the key ``DPoPGate``
    proved on this request; ``access_token`` to the one in ``Authorization``.
    ``rpc`` — ``(method, params)`` of the JSON-RPC request in front of the host —
    requires the statement to be for exactly that request (its ``rpc`` claim,
    ``contract.rpc_digest``); a host should always pass it.
    Returns the ``Execution``, or raises ``ExecutionRefused``."""
    headers = _headers(request)
    raw = _get(headers, contract.EXECUTION_HEADER)
    if not raw or "," in raw:
        raise ExecutionRefused("no_statement", f"send exactly one {contract.EXECUTION_HEADER}")
    client_id = getattr(config, "canopy_client_id", "")
    resource = getattr(config, "resource", "")
    if not client_id or not resource:
        raise ExecutionRefused("not_configured", "this host trusts no canopy (canopy_client_id, resource)")
    try:
        header = unverified_header(raw)
        if header.get("typ") != contract.EXECUTION_TYP:
            raise ContractError("bad_typ", "not a canopy execution statement")
        jwk = client_keys.key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(raw, key, algorithms=[header["alg"]], audience=[resource],
                        required=contract.EXECUTION_REQUIRED_CLAIMS,
                        leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, contract.EXECUTION_MAX_LIFETIME, what="an execution statement")
        jti = contract.check_jti(claims)
    except MetadataError as exc:
        log.warning("canopy client metadata unusable: %s", exc)
        raise ExecutionRefused("client_metadata", "canopy's keys could not be read") from exc
    except ContractError as exc:
        raise ExecutionRefused(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise ExecutionRefused("bad_issuer", "the statement is not from the canopy this host trusts")

    proved = jkt if jkt is not None else presented_dpop_jkt.get()
    cnf = claims.get("cnf") if isinstance(claims.get("cnf"), Mapping) else {}
    if not proved or not constant_time_equal(str(cnf.get("jkt") or ""), proved):
        raise ExecutionRefused("unbound", "the statement is not bound to the key proved on this request")
    token = access_token if access_token is not None else _access_token(headers)
    if "ath" in claims and (not token or not constant_time_equal(str(claims["ath"]),
                                                                  contract.ath(token))):
        raise ExecutionRefused("unbound", "the statement is for a different access token")
    if rpc is not None and not constant_time_equal(str(claims.get("rpc") or ""),
                                                   contract.rpc_digest(*rpc)):
        raise ExecutionRefused("wrong_request", "the statement is for a different request")

    try:
        replay_store.consume([(f"execution:{client_id}", jti, int(claims["exp"]) + contract.LEEWAY_SECONDS)])
    except Replayed as exc:
        raise ExecutionRefused("replayed", "this statement has already been used") from exc
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
        log.warning("jti store failed; refusing the execution statement", exc_info=True)
        raise ExecutionRefused("jti_store_error", "the statement could not be recorded as used") from exc

    runner = claims.get("runner") if isinstance(claims.get("runner"), Mapping) else {}
    posture = claims.get("runner_posture") if isinstance(claims.get("runner_posture"), Mapping) else {}
    decl = claims.get("declaration") if isinstance(claims.get("declaration"), Mapping) else None
    reqs = claims.get("requirements") if isinstance(claims.get("requirements"), list) else []
    return Execution(
        agent=str(claims.get("agent") or ""), agent_sha=str(claims.get("agent_sha") or ""),
        declaration=dict(decl) if decl is not None else None, runner=dict(runner),
        runner_posture=dict(posture), requirements=tuple(str(r) for r in reqs),
        turn=str(claims.get("turn") or ""), session=str(claims.get("session") or ""),
        claims=dict(claims))
