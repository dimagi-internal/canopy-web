"""``verify_execution(request)`` / ``verify_view(request, uri=, subject=)`` for a Django
host: canopy's ``Canopy-Execution`` (an agent's call) or ``Canopy-View`` (the person's own
View) statement on this request, verified with this host's configuration, its jti table
and a cached key resolver. See ``canopy_sdk.host.execution`` and ``canopy_sdk.host.view``."""
from __future__ import annotations

from ..host import execution as _execution
from ..host.client_keys import ClientKeyResolver
from ..host.execution import Execution, ExecutionRefused
from ..host.view import View, ViewRefused
from . import conf
from .stores import DjangoJtiStore

__all__ = ["Execution", "ExecutionRefused", "View", "ViewRefused", "verify_execution", "verify_view"]


def verify_execution(request, *, jkt: str | None = None, access_token: str | None = None) -> Execution:
    """The verified statement, or ``ExecutionRefused``. ``request`` is a Django or
    Starlette request (anything with ``.headers``) or a header mapping; ``jkt``
    defaults to the key ``dpop_gate`` proved on this request."""
    from django.core.cache import cache

    return _execution.verify_execution(
        request, config=conf.get_host_config(), client_keys=ClientKeyResolver(cache=cache),
        replay_store=DjangoJtiStore(), jkt=jkt, access_token=access_token)


def verify_view(request, *, uri: str, subject: str, jkt: str | None = None,
                access_token: str | None = None) -> View:
    """The verified ``Canopy-View`` statement for a read of ``uri`` as ``subject`` (the
    host's id for the person the delegated token serves), or ``ViewRefused``."""
    from django.core.cache import cache

    from ..host import view as _view

    return _view.verify_view(
        request, config=conf.get_host_config(), client_keys=ClientKeyResolver(cache=cache),
        replay_store=DjangoJtiStore(), uri=uri, subject=subject, jkt=jkt, access_token=access_token)
