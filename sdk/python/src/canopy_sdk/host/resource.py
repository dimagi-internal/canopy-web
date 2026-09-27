"""The host's MCP side: verifying a DPoP-bound access token and limiting it to
the tools its scopes map to.

Two pieces, because hosts already have a bearer-token verifier and should keep
it:

* ``check_proof`` — for ``Authorization: DPoP <token>``: the proof matches this
  request (method, the public MCP URL, the token's hash), is fresh, and is used
  once. Returns the proving key's thumbprint. The ASGI ``DPoPGate`` runs this and
  hands the rest of the stack a plain ``Bearer`` plus the thumbprint.
* ``resolve`` — for the host's token verifier: a delegated token is live only if
  it is known, unexpired, its subject still active, AND presented with a proof by
  the key it is bound to. A bound token sent as a plain bearer (no key proved) is
  refused exactly like an unknown one — that is what makes a token copied out of
  a log useless.

Tokens WITHOUT a binding (a host's PATs, ordinary OAuth sign-ins) never reach
this module; ``resolve`` returns ``None`` for anything it did not issue, and the
host's existing verifier carries on.

Extracted from connect-labs ``connect_labs/mcp/delegation.py``
(``resolve_delegated_token``, ``_check_mcp_proof``, ``allowed_tools``).
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import verify_dpop_proof
from ..stores import JtiStore, Replayed, TokenStore
from .config import HostConfig

log = logging.getLogger("canopy_sdk.host")


class DPoPRefused(ContractError):
    """A missing, malformed, stale, replayed or mismatched DPoP proof. Answer
    401 with ``WWW-Authenticate: DPoP error="invalid_dpop_proof"``."""


@dataclass(frozen=True)
class DelegatedPrincipal:
    """Who a delegated MCP request acts for, and what it may reach."""

    subject: str
    client_id: str
    actor: str
    scopes: tuple[str, ...]
    cnf_jkt: str
    expires_at: float
    allowed_tools: frozenset[str]

    def allows(self, tool: str) -> bool:
        return tool in self.allowed_tools

    def filter_tools(self, tools: Iterable):
        """Keep only the tools this principal may use. Accepts names or objects
        with a ``name`` attribute (or ``"name"`` key)."""
        out = []
        for tool in tools:
            name = tool if isinstance(tool, str) else (
                tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None))
            if name in self.allowed_tools:
                out.append(tool)
        return out

    def claims(self) -> dict:
        """The token's claims in RFC 8693 / RFC 9449 shape, for an audit row or
        an access-token object: ``cnf.jkt`` and ``act.sub``."""
        return {
            "sub": self.subject,
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
            "auth_method": "delegated",
            "cnf": {"jkt": self.cnf_jkt},
            "act": {"sub": self.actor},
            "exp": int(self.expires_at),
        }


class ResourceVerifier:
    """Verifies delegated requests at the host's MCP (``config.resource``)."""

    #: Probability that a proof check also prunes the replay store.
    PRUNE_PROBABILITY = 0.01

    def __init__(self, config: HostConfig, *, token_store: TokenStore, replay_store: JtiStore,
                 subject_active: Callable[[str], object] | None = None):
        self.config = config
        self.token_store = token_store
        self.replay_store = replay_store
        self.subject_active = subject_active

    def check_proof(self, proofs, method: str, access_token: str, *, now: float | None = None) -> str:
        """Verify the request's DPoP proof and spend its ``jti``. Returns the
        proving key's thumbprint. ``proofs`` is the list of ``DPoP`` header values
        (exactly one is required) or a single string."""
        if isinstance(proofs, str):
            proofs = [proofs]
        proofs = [p for p in (proofs or []) if p is not None]
        if not access_token or len(proofs) != 1 or "," in proofs[0]:
            raise DPoPRefused("no_proof", "send exactly one DPoP proof with a DPoP-bound token")
        try:
            jkt, jti, iat = verify_dpop_proof(proofs[0], htm=method, htu=self.config.resource,
                                              access_token=access_token, now=now)
        except ContractError as exc:
            raise DPoPRefused(exc.code, exc.message) from exc
        try:
            self.replay_store.consume([(f"dpop:{jkt}", jti, iat + contract.DPOP_IAT_WINDOW_SECONDS)])
        except Replayed as exc:
            raise DPoPRefused("replayed", "this proof has already been used") from exc
        except Exception as exc:  # noqa: BLE001 - a replay store that cannot answer fails CLOSED
            log.warning("DPoP replay store failed; refusing", exc_info=True)
            raise DPoPRefused("replay_store_error", "the proof could not be recorded as used") from exc
        if random.random() < self.PRUNE_PROBABILITY:  # noqa: S311 -- housekeeping, not security
            try:
                self.replay_store.prune()
            except Exception:  # noqa: BLE001
                log.warning("pruning used DPoP jtis failed", exc_info=True)
        return jkt

    def resolve(self, raw: str, presented_jkt: str | None, *, now: float | None = None) -> DelegatedPrincipal | None:
        """The principal for a live delegated token, else ``None``."""
        if not raw:
            return None
        token = self.token_store.get(contract.token_checksum(raw))
        if token is None:
            return None
        if token.expired(time.time() if now is None else now):
            return None
        if self.subject_active is not None:
            try:
                if not self.subject_active(token.subject):
                    return None
            except Exception:  # noqa: BLE001
                return None
        if presented_jkt is None:
            log.warning("delegated token presented without a DPoP proof (sub=%s)", token.subject)
            return None
        if not constant_time_equal(presented_jkt, token.cnf_jkt):
            log.warning("delegated token presented with a proof by the wrong key (sub=%s)", token.subject)
            return None
        return DelegatedPrincipal(
            subject=token.subject, client_id=token.client_id, actor=token.actor,
            scopes=tuple(token.scopes), cnf_jkt=token.cnf_jkt, expires_at=token.expires_at,
            allowed_tools=self.config.tools_for_scopes(token.scopes),
        )

    def authenticate(self, authorization: str | None, proofs, method: str, *,
                     now: float | None = None) -> DelegatedPrincipal | None:
        """Both steps for one request. ``None`` means "not a DPoP request — let
        the host's own verifier decide". A DPoP request that fails raises
        ``DPoPRefused`` (bad proof) or returns no principal as ``ContractError
        ('invalid_token')``."""
        if not authorization or authorization[:5].lower() != "dpop ":
            return None
        token = authorization[5:].strip()
        jkt = self.check_proof(proofs, method, token, now=now)
        principal = self.resolve(token, jkt, now=now)
        if principal is None:
            raise ContractError("invalid_token", "the access token is not valid here")
        return principal
