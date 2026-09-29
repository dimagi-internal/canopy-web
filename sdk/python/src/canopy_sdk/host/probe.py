"""The host's PROBE endpoint: a real ID-JAG for a dedicated probe principal.

Conformance (``canopy_sdk.conformance``) proves a host's documents, keys and
client authentication — and stops exactly where a visitor would start: a real
grant needs an ID-JAG signed by the host's key, which canopy never holds. So
nobody learned that the grant chain was broken until a visitor asked an agent
for something and the agent could not do it.

The probe closes that gap without a visitor. A host that configures a
``ProbeIdentity`` answers canopy — and ONLY canopy — with a real ID-JAG for one
fixed, low-privilege principal and one read-only scope. canopy then redeems it
through the NORMAL jwt-bearer path and makes one real MCP call with it. Every
step a visitor's grant takes, the probe takes too.

What the endpoint enforces, in order:

1. the probe is configured (else ``ProbeDisabled`` — a view answers 404);
2. the request names no principal, scope, resource or tool of its own
   (``PROBE_FORBIDDEN_FIELDS``, and a ``scope``/``resource`` other than the
   probe's): the identity is the host's to fix, and a request that tries to
   choose is refused rather than quietly overruled;
3. the client is the ONE configured canopy client, authenticated by
   ``private_key_jwt`` exactly as at the token endpoint (``aud`` = the issuer or
   this endpoint);
4. the request carries a DPoP proof for this endpoint, by a key that is NOT the
   client key;
5. the probe principal is still active here;
6. every statement works once — consumed only after every check passed.

The answer is an ID-JAG with ``canopy_probe: true`` (so a host's audit can tell
probe traffic from a visitor's), a lifetime of at most 300s and a single-use
``jti`` (spent when canopy redeems it), plus the tool and arguments canopy
should call.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import verify_dpop_proof
from ..stores import JtiStore, Replayed
from .client_keys import ClientKeyResolver
from .config import HostConfig, HostNotConfigured
from .grant import GrantHandler, GrantRefused
from .signing import issue_id_jag

log = logging.getLogger("canopy_sdk.host")


class ProbeDisabled(HostNotConfigured):
    """No probe identity here (or the grant itself is off). A view answers 404:
    a server does not advertise a way in it does not offer."""


@dataclass
class ProbeResult:
    """A probe ID-JAG and what canopy should do with it."""

    id_jag: str
    subject: str
    scope: str
    resource: str
    tool: str
    arguments: dict
    denied_tool: str
    page: str
    expires_in: int

    def __repr__(self) -> str:  # never print the grant
        return f"ProbeResult(subject={self.subject!r}, scope={self.scope!r}, tool={self.tool!r})"

    def body(self) -> dict:
        return {
            "id_jag": self.id_jag,
            "subject": self.subject,
            "scope": self.scope,
            "resource": self.resource,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "denied_tool": self.denied_tool,
            "page": self.page,
            "expires_in": self.expires_in,
        }

    @staticmethod
    def headers() -> dict:
        return {"Cache-Control": "no-store", "Pragma": "no-cache"}


class ProbeHandler:
    """The probe endpoint for one host. Framework-free: a view passes the POSTed
    form and the ``DPoP`` header in and gets a ``ProbeResult``, or a
    ``GrantRefused`` carrying the RFC 6749 error to send, or ``ProbeDisabled``."""

    def __init__(self, config: HostConfig, *, jti_store: JtiStore,
                 client_keys: ClientKeyResolver | None = None,
                 subject_active: Callable[[str], object] | None = None):
        self.config = config
        self.jti_store = jti_store
        self.subject_active = subject_active
        # The client assertion is verified by the SAME code the token endpoint
        # runs — one implementation of "this request is canopy".
        self._grant = GrantHandler(config, jti_store=jti_store, token_store=_NoTokens(),
                                   client_keys=client_keys, subject_active=subject_active)

    def handle(self, form: Mapping[str, str], dpop: str | None) -> ProbeResult:
        if not self.config.probe_enabled:
            raise ProbeDisabled("no probe identity is configured here")
        try:
            return self._issue(form, dpop)
        except GrantRefused as refused:
            log.warning("canopy probe refused: %s (%s)", refused.reason, refused.code)
            raise

    def _issue(self, params: Mapping[str, str], proofs: str | None) -> ProbeResult:
        config = self.config
        probe = config.probe
        client_id = config.canopy_client_id

        # --- 1. The request may not choose who or what. ---------------------------
        chose = sorted(k for k in contract.PROBE_FORBIDDEN_FIELDS if k in params)
        if chose:
            raise GrantRefused("invalid_request",
                               "The probe's principal and tool are fixed by this server.",
                               reason="probe_names_" + chose[0])
        scope = params.get("scope")
        if scope is not None and scope.split() != [probe.scope]:
            raise GrantRefused("invalid_scope", "The probe carries only this server's probe scope.",
                               reason="probe_scope")
        resource = params.get("resource")
        if resource is not None and not constant_time_equal(
                contract.normalize_url(resource), contract.normalize_url(config.resource)):
            raise GrantRefused("invalid_target", "The probe is only for this server's MCP endpoint.",
                               reason="probe_resource")

        # --- 2. The client: only canopy, by private_key_jwt. -------------------------
        if not constant_time_equal(params.get("client_id", ""), client_id):
            raise GrantRefused("invalid_client", "This client may not probe this server.", 401, "unknown_client")
        if params.get("client_assertion_type") != contract.CLIENT_ASSERTION_TYPE \
                or not params.get("client_assertion"):
            raise GrantRefused("invalid_client", "Authenticate with private_key_jwt.", 401, "no_client_assertion")
        client_claims, client_jkt = self._grant._verify_client_assertion(
            params["client_assertion"], client_id, audience=[config.issuer, probe.endpoint])

        # --- 3. A DPoP proof for THIS endpoint, by a key that is not the client's. --
        if not proofs or "," in proofs:
            raise GrantRefused("invalid_dpop_proof", "Send exactly one DPoP proof.", reason="no_dpop_proof")
        try:
            jkt, proof_jti, proof_iat = verify_dpop_proof(proofs, htm="POST", htu=probe.endpoint)
        except ContractError as exc:
            raise GrantRefused("invalid_dpop_proof", f"The DPoP proof was refused: {exc.message}.",
                               reason=exc.code) from exc
        if constant_time_equal(jkt, client_jkt):
            raise GrantRefused("invalid_dpop_proof", "The DPoP key must not be the client's key.",
                               reason="dpop_is_client_key")

        # --- 4. The probe principal must still be a live account here. -------------
        if self.subject_active is not None:
            try:
                active = self.subject_active(probe.subject)
            except Exception:  # noqa: BLE001 - an unanswerable subject is not an active one
                active = False
            if not active:
                raise GrantRefused("invalid_grant", "The probe principal is not an active account here.",
                                   reason="probe_subject_inactive")

        # --- 5. Every statement works once, consumed after every check passed. -------
        try:
            self.jti_store.consume([
                ("client", client_claims["jti"], int(client_claims["exp"])),
                (f"dpop:{jkt}", proof_jti, proof_iat + contract.DPOP_IAT_WINDOW_SECONDS),
            ])
        except Replayed as exc:
            raise GrantRefused("invalid_grant", "This client assertion or proof has already been used.",
                               reason="replayed") from exc
        except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
            log.warning("jti store failed; refusing the probe", exc_info=True)
            raise GrantRefused("invalid_grant", "The request could not be recorded as used.",
                               reason="jti_store_error") from exc

        id_jag = issue_id_jag(config, probe.subject, [probe.scope],
                              extra_claims={contract.PROBE_CLAIM: True})
        log.info("canopy probe: ID-JAG issued for the probe principal %s (scope %s, tool %s)",
                 probe.subject, probe.scope, probe.tool)
        return ProbeResult(
            id_jag=id_jag, subject=probe.subject, scope=probe.scope, resource=config.resource,
            tool=probe.tool, arguments=dict(probe.arguments), denied_tool=probe.denied_tool,
            page=probe.page, expires_in=config.id_jag_ttl,
        )


class _NoTokens:
    """The probe issues no access token; its borrowed ``GrantHandler`` is used
    only to authenticate the client, so a save here would be a bug."""

    def save(self, token):  # pragma: no cover - unreachable by construction
        raise AssertionError("the probe endpoint issues no access tokens")

    def get(self, token_checksum):  # pragma: no cover
        return None

    def prune(self):  # pragma: no cover
        return None


__all__ = ["ProbeDisabled", "ProbeHandler", "ProbeResult"]
