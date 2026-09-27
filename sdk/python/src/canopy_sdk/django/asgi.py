"""The DPoP gate for a Django host's MCP ASGI app, backed by this app's tables."""
from __future__ import annotations

from asgiref.sync import sync_to_async

from ..host.asgi import DPoPGate
from ..host.resource import ResourceVerifier
from . import conf
from .stores import DjangoJtiStore, DjangoTokenStore


def resource_verifier() -> ResourceVerifier:
    return ResourceVerifier(conf.get_host_config(), token_store=DjangoTokenStore(),
                            replay_store=DjangoJtiStore(), subject_active=conf.subject_active())


def _run_sync(fn, *args):
    # thread_sensitive: the ORM's connections are per-thread, and Django's own
    # sync_to_async is what keeps them managed.
    return sync_to_async(fn, thread_sensitive=True)(*args)


def dpop_gate(app, *, require_principal: bool = False):
    """Wrap the MCP ASGI app: ``application = dpop_gate(mcp_app)``.

    Then, in the host's bearer-token verifier, resolve a delegated token with
    ``resource_verifier().resolve(raw, presented_dpop_jkt.get())``
    (``canopy_sdk.host.presented_dpop_jkt``) — or pass
    ``require_principal=True`` and read ``canopy_sdk.host.delegated_principal``.
    """
    # The verifier is built per request, so a settings change (or a test's
    # override_settings) applies without rebuilding the ASGI app.
    return DPoPGate(app, resource_verifier, require_principal=require_principal, run_sync=_run_sync)
