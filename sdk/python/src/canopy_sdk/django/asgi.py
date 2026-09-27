"""The DPoP gate for a Django host's MCP ASGI app, backed by this app's tables."""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from ..host.asgi import DPoPGate
from ..host.config import HostNotConfigured
from ..host.resource import DelegatedPrincipal, ResourceVerifier
from . import conf
from .stores import DjangoJtiStore, DjangoTokenStore

log = logging.getLogger("canopy_sdk.django")


def resource_verifier() -> ResourceVerifier:
    """The verifier for this host's MCP. Raises ``HostNotConfigured`` when the
    grant is off (no signing key, or any of CLIENT_ID / ISSUER / RESOURCE /
    TOKEN_ENDPOINT missing): a host that issues no grants accepts no delegated
    tokens — including ones issued while it was on."""
    config = conf.get_host_config()
    if not config.grant_enabled:
        raise HostNotConfigured("the grant is off: CLIENT_ID, ISSUER, RESOURCE and TOKEN_ENDPOINT")
    return ResourceVerifier(config, token_store=DjangoTokenStore(),
                            replay_store=DjangoJtiStore(), subject_active=conf.subject_active())


def resolve_delegated(raw_token: str, presented_jkt: str | None) -> DelegatedPrincipal | None:
    """For the host's bearer-token verifier: the delegated principal ``raw_token``
    acts for, or ``None`` — for a token this SDK did not issue, a bound token
    presented without its key, or a host whose grant is off. Never raises for
    configuration, so a PAT/OAuth verifier can call it first or last."""
    try:
        verifier = resource_verifier()
    except HostNotConfigured:
        return None
    except Exception:  # noqa: BLE001 - an unreadable key is a deployment fault, not a 500
        log.exception("CANOPY_HOST could not be read; no delegated tokens accepted")
        return None
    return verifier.resolve(raw_token, presented_jkt)


def _run_sync(fn, *args):
    # thread_sensitive: the ORM's connections are per-thread, and Django's own
    # sync_to_async is what keeps them managed.
    return sync_to_async(fn, thread_sensitive=True)(*args)


def dpop_gate(app, *, require_principal: bool = False, run_sync=None):
    """Wrap the MCP ASGI app: ``application = dpop_gate(mcp_app)``.

    Safe to install unconditionally: while the grant is off, a request with
    ``Authorization: DPoP`` is refused 401 ``invalid_dpop_proof`` and every other
    request passes through untouched — never a 500.

    Then, in the host's bearer-token verifier, resolve a delegated token with
    ``resolve_delegated(raw, presented_dpop_jkt.get())``
    (``canopy_sdk.host.presented_dpop_jkt``) — or pass
    ``require_principal=True`` and read ``canopy_sdk.host.delegated_principal``.
    ``run_sync`` overrides how the store calls run (default: Django's
    ``sync_to_async(..., thread_sensitive=True)``; a host that closes stale
    connections around each call passes its own).
    """
    # The verifier is built per request, so a settings change (or a test's
    # override_settings) applies without rebuilding the ASGI app.
    return DPoPGate(app, resource_verifier, require_principal=require_principal,
                    run_sync=run_sync or _run_sync)
