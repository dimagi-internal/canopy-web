"""The host's token endpoint logic: redeem an ID-JAG for a DPoP-bound access token.

RFC 7523 jwt-bearer + ``private_key_jwt`` + DPoP (RFC 9449), exactly as the host
grant contract v1 pins it. Framework-free: a view passes the form and the
``DPoP`` header in and gets either a ``GrantResult`` (persisted already, through
the ``TokenStore``) or a ``GrantRefused`` carrying the RFC 6749 error to send.

Extracted from connect-labs ``connect_labs/mcp/delegation.py::_redeem`` (PR
#2060), in the same order of checks, with the same refusal for each:

1. the client is the ONE configured canopy client, authenticated by
   ``private_key_jwt`` against the keys its metadata document names;
2. the request proves possession of a DPoP key — and that key is NOT the client
   key (the contract keeps the two apart);
3. the ID-JAG was signed by THIS host, for this client, for this MCP, is short,
   and names a subject the host still recognises;
4. the requested resource and scope are within the grant;
5. every statement works once — and is consumed only after every check passed,
   so a request that fails (say) on its proof does not burn the grant it carried.

Never logs a token, an assertion or a proof — only the reason code.
"""
from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import decode, unverified_header, verify_dpop_proof
from ..keys import alg_for, public_key_for
from ..stores import IssuedToken, JtiStore, Replayed, TokenStore
from .client_keys import ClientKeyResolver, MetadataError
from .config import HostConfig

log = logging.getLogger("canopy_sdk.host")


class GrantRefused(Exception):
    """An RFC 6749 error for the token endpoint. ``reason`` names the failed
    check (safe to log); ``description`` is safe to send."""

    def __init__(self, code: str, description: str, status: int = 400, reason: str = ""):
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status
        self.reason = reason or code

    def body(self) -> dict:
        return {"error": self.code, "error_description": self.description}

    def headers(self) -> dict:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        if self.code == "invalid_dpop_proof":
            headers["WWW-Authenticate"] = (
                f'DPoP error="invalid_dpop_proof", algs="{" ".join(contract.GRANT_ALGORITHMS)}"')
        return headers


@dataclass
class GrantResult:
    #: The raw access token — returned to canopy ONCE, never stored.
    access_token: str
    token: IssuedToken

    def body(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": contract.TOKEN_TYPE_DPOP,
            "expires_in": int(round(self.token.expires_at - self.token.created_at)),
            "scope": self.token.scope,
        }

    @staticmethod
    def headers() -> dict:
        return {"Cache-Control": "no-store", "Pragma": "no-cache"}


class GrantHandler:
    """The jwt-bearer grant for one host.

    ``subject_active(sub)`` — optional — is asked whether the ID-JAG's subject is
    still a live account here (connect-labs: an active user). A falsy answer (or
    an exception) refuses the grant.
    """

    def __init__(self, config: HostConfig, *, jti_store: JtiStore, token_store: TokenStore,
                 client_keys: ClientKeyResolver | None = None,
                 subject_active: Callable[[str], object] | None = None):
        self.config = config
        self.jti_store = jti_store
        self.token_store = token_store
        self.client_keys = client_keys or ClientKeyResolver()
        self.subject_active = subject_active

    # --- entry point -----------------------------------------------------------

    def handle(self, form: Mapping[str, str], dpop: str | None) -> GrantResult:
        """Redeem. ``form`` is the POSTed form; ``dpop`` the raw ``DPoP`` header
        (``None`` when absent). Raises ``GrantRefused``."""
        if not self.config.grant_enabled:
            raise GrantRefused("unsupported_grant_type",
                               "This server does not accept the jwt-bearer grant.")
        try:
            return self._redeem(form, dpop)
        except GrantRefused as refused:
            log.warning("delegated grant refused: %s (%s)", refused.reason, refused.code)
            raise

    def _redeem(self, params: Mapping[str, str], proofs: str | None) -> GrantResult:
        client_id = self.config.canopy_client_id

        if params.get("grant_type") != contract.JWT_BEARER_GRANT:
            raise GrantRefused("unsupported_grant_type", "Only the jwt-bearer grant is handled here.")

        # --- 1. The client: only canopy, authenticated by private_key_jwt. ------
        if not constant_time_equal(params.get("client_id", ""), client_id):
            raise GrantRefused("invalid_client", "This client may not use this grant.", 401, "unknown_client")
        if params.get("client_assertion_type") != contract.CLIENT_ASSERTION_TYPE \
                or not params.get("client_assertion"):
            raise GrantRefused("invalid_client", "Authenticate with private_key_jwt.", 401, "no_client_assertion")
        client_claims, client_jkt = self._verify_client_assertion(params["client_assertion"], client_id)

        # --- 2. Possession of the DPoP key the token will be bound to. ----------
        if not proofs or "," in proofs:
            raise GrantRefused("invalid_dpop_proof", "Send exactly one DPoP proof.", reason="no_dpop_proof")
        try:
            jkt, proof_jti, proof_iat = verify_dpop_proof(proofs, htm="POST", htu=self.config.token_endpoint)
        except ContractError as exc:
            raise GrantRefused("invalid_dpop_proof", f"The DPoP proof was refused: {exc.message}.",
                               reason=exc.code) from exc
        if constant_time_equal(jkt, client_jkt):
            # The client key authenticates canopy; the DPoP key is what a stolen
            # access token would need too. One key doing both jobs defeats that.
            raise GrantRefused("invalid_dpop_proof", "The DPoP key must not be the client's key.",
                               reason="dpop_is_client_key")

        # --- 3. The grant itself: signed by this host, for canopy, for this MCP. -
        if params.get("assertion") is None:
            raise GrantRefused("invalid_request", "The assertion parameter is required.")
        grant = self._verify_id_jag(params["assertion"], client_id)

        requested_resource = params.get("resource")
        if requested_resource is not None and not constant_time_equal(
                contract.normalize_url(requested_resource), contract.normalize_url(self.config.resource)):
            raise GrantRefused("invalid_target", "Tokens are issued only for this server's MCP endpoint.")

        granted = [s for s in contract.scopes_of(grant.get("scope", "")) if s in self.config.scope_tools]
        requested = params.get("scope")
        if requested is not None:
            wanted = requested.split()
            if not wanted or not set(wanted) <= set(granted):
                raise GrantRefused("invalid_scope", "The requested scope is not within the grant.")
            granted = [s for s in granted if s in wanted]
        if not granted:
            raise GrantRefused("invalid_scope", "The grant carries no scope this server offers.")

        # --- 4. Every statement works once. Consumed only after all checks pass. -
        try:
            self.jti_store.consume([
                ("client", client_claims["jti"], int(client_claims["exp"])),
                ("idjag", grant["jti"], int(grant["exp"])),
                (f"dpop:{jkt}", proof_jti, proof_iat + contract.DPOP_IAT_WINDOW_SECONDS),
            ])
        except Replayed as exc:
            raise GrantRefused("invalid_grant", "This grant, client assertion or proof has already been used.",
                               reason="replayed") from exc
        except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
            log.warning("jti store failed; refusing the grant", exc_info=True)
            raise GrantRefused("invalid_grant", "The grant could not be recorded as used.",
                               reason="jti_store_error") from exc

        return self._issue(grant, client_id, granted, jkt)

    # --- the two JWTs ----------------------------------------------------------------

    def _verify_client_assertion(self, assertion: str, client_id: str,
                                 audience: list[str] | None = None) -> tuple[dict, str]:
        """canopy's ``private_key_jwt``, against the keys its metadata names.
        ``audience`` defaults to this host's issuer or token endpoint; the probe
        endpoint passes its own URL instead of the token endpoint."""
        try:
            header = unverified_header(assertion)
        except ContractError as exc:
            raise GrantRefused("invalid_client", "The client assertion is not usable.", 401, exc.code) from exc
        try:
            jwk = self.client_keys.key_for(client_id, header.get("kid"))
            key = public_key_for(jwk, header["alg"])
            claims = decode(
                assertion, key, algorithms=[header["alg"]],
                audience=audience or [self.config.issuer, self.config.token_endpoint],
                required=contract.CLIENT_ASSERTION_REQUIRED_CLAIMS,
                leeway=contract.LEEWAY_SECONDS,
            )
        except MetadataError as exc:
            log.warning("canopy client metadata unusable: %s", exc)
            raise GrantRefused("invalid_client", "The client's keys could not be read.", 401,
                               "client_metadata") from exc
        except ContractError as exc:
            raise GrantRefused("invalid_client", "The client assertion was refused.", 401,
                               f"client_{exc.code}") from exc
        if not (constant_time_equal(claims["iss"], client_id) and constant_time_equal(claims["sub"], client_id)):
            raise GrantRefused("invalid_client", "The client assertion must be issued by the client, about itself.",
                               401, "client_iss_sub")
        if contract.lifetime(claims) > contract.CLIENT_ASSERTION_MAX_LIFETIME:
            raise GrantRefused("invalid_client", "The client assertion may live at most 60 seconds.", 401,
                               "client_lifetime")
        try:
            contract.check_jti(claims)
        except ContractError as exc:
            raise GrantRefused("invalid_client", "The client assertion needs a jti.", 401, "client_jti") from exc
        return claims, contract.jwk_thumbprint(jwk)

    def _verify_id_jag(self, assertion: str, client_id: str) -> dict:
        """An ID-JAG this host itself signed, for a subject it still recognises."""
        config = self.config
        try:
            header = unverified_header(assertion)
            if header.get("typ") != contract.ID_JAG_TYP:
                raise ContractError("bad_typ", "the assertion is not an ID-JAG")
            keys = config.verification_keys()
            kid = header.get("kid")
            match = next((k for k in keys if constant_time_equal(kid, k)), None)
            if match is None:
                raise ContractError("unknown_key", "the assertion names a key this server does not hold")
            key = keys[match]
            if header["alg"] != alg_for(key):
                raise ContractError("bad_alg", "the assertion is not signed with this server's algorithm")
            grant = decode(
                assertion, key, algorithms=[header["alg"]],
                audience=[config.issuer, config.issuer + "/"],
                required=contract.HOST_ID_JAG_REQUIRED_CLAIMS,
                leeway=contract.LEEWAY_SECONDS,
            )
            contract.check_jti(grant)
        except ContractError as exc:
            raise GrantRefused("invalid_grant", f"The grant was refused: {exc.message}.",
                               reason=f"idjag_{exc.code}") from exc

        if not constant_time_equal(contract.normalize_url(str(grant["iss"])), config.issuer):
            raise GrantRefused("invalid_grant", "The grant was issued by someone else.", reason="idjag_iss")
        if not constant_time_equal(grant["client_id"], client_id):
            raise GrantRefused("invalid_grant", "The grant was issued to a different client.",
                               reason="idjag_client_id")
        if not constant_time_equal(contract.normalize_url(str(grant["resource"])),
                                   contract.normalize_url(config.resource)):
            raise GrantRefused("invalid_grant", "The grant is for a different resource.", reason="idjag_resource")
        if contract.lifetime(grant) > contract.ID_JAG_MAX_LIFETIME:
            raise GrantRefused("invalid_grant", "The grant may live at most 300 seconds.", reason="idjag_lifetime")

        subject = grant.get("sub")
        if not isinstance(subject, str) or not subject:
            raise GrantRefused("invalid_grant", "The grant names no subject.", reason="idjag_sub")
        if self.subject_active is not None:
            try:
                active = self.subject_active(subject)
            except Exception:  # noqa: BLE001 - an unanswerable subject is not an active one
                active = False
            if not active:
                raise GrantRefused("invalid_grant", "The grant's subject is not an active user.",
                                   reason="idjag_sub")
        return grant

    # --- issuing ---------------------------------------------------------------------

    def _issue(self, grant: dict, client_id: str, scopes: list[str], jkt: str) -> GrantResult:
        raw = secrets.token_urlsafe(32)
        now = time.time()
        token = IssuedToken(
            token_checksum=contract.token_checksum(raw),
            subject=str(grant["sub"]),
            client_id=client_id,
            actor=client_id,
            scopes=tuple(scopes),
            cnf_jkt=jkt,
            grant_jti=str(grant["jti"])[:64],
            expires_at=now + self.config.access_token_ttl,
            created_at=now,
        )
        self.token_store.save(token)
        self._prune()
        # `probe=True` marks canopy's live probe (an ID-JAG carrying
        # `canopy_probe: true`, which only this host's probe endpoint signs), so
        # an audit can tell it from a real visitor.
        log.info("delegated token issued: sub=%s client=%s scope=%s probe=%s", token.subject, client_id,
                 token.scope, grant.get(contract.PROBE_CLAIM) is True)
        return GrantResult(access_token=raw, token=token)

    def _prune(self) -> None:
        try:
            self.token_store.prune()
            self.jti_store.prune()
        except Exception:  # noqa: BLE001 -- housekeeping must never fail a grant
            log.warning("pruning delegated tokens failed", exc_info=True)
