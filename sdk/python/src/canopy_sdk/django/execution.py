"""``verify_execution(request)`` for a Django host: canopy's ``Canopy-Execution``
statement on this request, verified with this host's configuration, its jti table
and a cached key resolver. See ``canopy_sdk.host.execution``."""
from __future__ import annotations

from ..host import execution as _execution
from ..host.client_keys import ClientKeyResolver
from ..host.execution import Execution, ExecutionRefused
from . import conf
from .stores import DjangoJtiStore

__all__ = ["Execution", "ExecutionRefused", "verify_execution"]


def verify_execution(request, *, jkt: str | None = None, access_token: str | None = None) -> Execution:
    """The verified statement, or ``ExecutionRefused``. ``request`` is a Django or
    Starlette request (anything with ``.headers``) or a header mapping; ``jkt``
    defaults to the key ``dpop_gate`` proved on this request."""
    from django.core.cache import cache

    return _execution.verify_execution(
        request, config=conf.get_host_config(), client_keys=ClientKeyResolver(cache=cache),
        replay_store=DjangoJtiStore(), jkt=jkt, access_token=access_token)
