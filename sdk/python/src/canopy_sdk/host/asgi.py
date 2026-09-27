"""An ASGI wrapper for the host's MCP endpoint: verify DPoP, then hand the app a
plain bearer.

MCP servers (FastMCP, the MCP SDK) only understand ``Authorization: Bearer``.
For ``Authorization: DPoP <token>`` this checks the proof against the request
(method, the public MCP URL, the token's hash, freshness, single-use ``jti``),
records the proving key's thumbprint in ``presented_dpop_jkt`` for the host's
token verifier, and rewrites the header to ``Bearer``. A bad proof is refused
here with RFC 9449's ``invalid_dpop_proof``.

A request with an ordinary ``Bearer`` header passes through untouched with no
key presented — which is what makes a DPoP-bound token useless as a plain
bearer: ``ResourceVerifier.resolve`` refuses a bound token when no key was
proved.

With ``require_principal=True`` the gate also resolves the token itself and
publishes the ``DelegatedPrincipal`` in ``delegated_principal`` — for a host
whose MCP stack has no token verifier of its own to plug ``resolve`` into.

Extracted from connect-labs ``connect_labs/mcp/delegation.py::DPoPGate``.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging

from .. import contract
from ..contract import ContractError
from .resource import DelegatedPrincipal, ResourceVerifier

log = logging.getLogger("canopy_sdk.host")

#: The DPoP key thumbprint proved on THIS request. ``None``: no proof.
presented_dpop_jkt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "canopy_presented_dpop_jkt", default=None)
#: Set only with ``require_principal=True``.
delegated_principal: contextvars.ContextVar[DelegatedPrincipal | None] = contextvars.ContextVar(
    "canopy_delegated_principal", default=None)


def _default_run_sync(fn, *args):
    return asyncio.to_thread(fn, *args)


class DPoPGate:
    """``DPoPGate(app, verifier)``. ``run_sync(fn, *args)`` runs the (possibly
    blocking) store calls; Django hosts pass ``sync_to_async(...,
    thread_sensitive=True)`` via ``canopy_sdk.django.asgi.dpop_gate``."""

    def __init__(self, app, verifier, *, require_principal: bool = False, run_sync=None):
        self.app = app
        #: A ``ResourceVerifier``, or a zero-argument factory returning one (built
        #: per request, so configuration changes apply without a rebuild).
        self._verifier = verifier
        self.require_principal = require_principal
        self.run_sync = run_sync or _default_run_sync

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        authorization = [value for key, value in headers if key.lower() == b"authorization"]
        if len(authorization) != 1 or authorization[0][:5].lower() != b"dpop ":
            jkt_marker = presented_dpop_jkt.set(None)
            principal_marker = delegated_principal.set(None)
            try:
                await self.app(scope, receive, send)
            finally:
                presented_dpop_jkt.reset(jkt_marker)
                delegated_principal.reset(principal_marker)
            return

        proofs = [value for key, value in headers if key.lower() == b"dpop"]
        principal = None
        try:
            token = authorization[0][5:].strip().decode("ascii")
            decoded = [p.decode("ascii") for p in proofs]
            verifier = self.verifier()
            jkt = await self.run_sync(verifier.check_proof, decoded, scope.get("method", ""), token)
            if self.require_principal:
                principal = await self.run_sync(verifier.resolve, token, jkt)
                if principal is None:
                    await _refuse(send, "invalid_token", "the access token is not valid here")
                    return
        except UnicodeDecodeError:
            await _refuse(send, "invalid_dpop_proof", "the credentials are not ASCII")
            return
        except ContractError as exc:
            log.warning("MCP DPoP proof refused: %s", exc.code)
            await _refuse(send, "invalid_dpop_proof", f"the DPoP proof was refused: {exc.message}")
            return

        rewritten = [(k, v) for k, v in headers if k.lower() not in (b"authorization", b"dpop")]
        rewritten.append((b"authorization", b"Bearer " + token.encode("ascii")))
        jkt_marker = presented_dpop_jkt.set(jkt)
        principal_marker = delegated_principal.set(principal)
        try:
            await self.app({**scope, "headers": rewritten}, receive, send)
        finally:
            presented_dpop_jkt.reset(jkt_marker)
            delegated_principal.reset(principal_marker)

    def verifier(self) -> ResourceVerifier:
        if isinstance(self._verifier, ResourceVerifier):
            return self._verifier
        return self._verifier()


async def _refuse(send, error: str, description: str) -> None:
    body = json.dumps({"error": error, "error_description": description}).encode()
    challenge = f'DPoP error="{error}", algs="{" ".join(contract.GRANT_ALGORITHMS)}"'
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", challenge.encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
