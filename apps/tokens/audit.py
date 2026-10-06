"""Writing the embed audit trail.

One entry point, `record`, and it is BEST-EFFORT by construction: an audit
write must never turn a working operation into a failure, and must never mask
the real error on one that already failed. The same rule `apps/mcp/audit.py`
follows, for the same reason.

That trade is deliberate and worth naming: it means a database problem loses
audit rows silently. The alternative — refusing to mint when the log is
unavailable — turns a logging outage into an authentication outage. For a
surface whose failure mode is "the widget does not load", losing the row and
keeping the service is the better side to fail on; a lost row is recoverable
from the application log line that still gets written.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest

from .models import EmbedAuditLog

log = logging.getLogger(__name__)


def client_ip(request: HttpRequest | None) -> str | None:
    """The caller's address (`apps/common/client_ip`), or None when unknown.

    Correlation material rather than evidence: it is the address the ALB saw,
    which may be a NAT or a VPN."""
    from apps.common.client_ip import client_ip as _client_ip

    return _client_ip(request) or None


def record(
    *,
    event: str,
    request: HttpRequest | None = None,
    app=None,
    app_name: str = "",
    subject=None,
    actor=None,
    ok: bool = True,
    reason: str = "",
    detail: str = "",
) -> None:
    """Append one row. Never raises."""
    try:
        EmbedAuditLog.objects.create(
            event=event,
            ok=ok,
            reason=reason[:64],
            app=app if getattr(app, "pk", None) else None,
            # Denormalised so the trail survives the app or user being deleted.
            app_name=(app_name or getattr(app, "name", "") or "")[:100],
            subject=subject if getattr(subject, "pk", None) else None,
            subject_email=(getattr(subject, "email", "") or "")[:254],
            actor=actor if getattr(actor, "pk", None) else None,
            client_ip=client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "") if request else "")[:300],
            detail=detail[:500],
        )
    except Exception:  # noqa: BLE001 - see the module docstring
        log.exception("embed audit write failed: event=%s app=%s", event, app_name)
