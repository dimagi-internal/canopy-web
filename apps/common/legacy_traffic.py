"""Who still calls canopy on its OLD address — the evidence for retiring it.

canopy moved to canopy.dimagi.com on 2026-10-05, and the old address
(labs.connect.dimagi.com/canopy) still serves machine traffic because clients
hold it: runner configs, agent scripts, hand-added MCP connections, a cloud
runner's stack parameter. None of those can be listed from here, so before the
old address stops answering, every caller still on it is recorded in the event
log (source ``legacy_address``), one row per caller per kind of path, with a
count. Retiring it is safe when that log has gone quiet.

What is recorded is who (the credential's label, or the user), what program
(the client header / user agent) and which surface (the path with ids taken
out) — never a token, never a query string.

Throttled per row (one write a minute) because a runner heartbeats every few
seconds; the count is therefore "minutes seen", which is what the question
needs. The load balancer's health check (``/health/``) is not a caller.
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache

logger = logging.getLogger(__name__)

SOURCE = "legacy_address"
WRITE_EVERY_SECONDS = 60
_ID = re.compile(r"/(?:[0-9a-fA-F-]{8,}|\d+)(?=/|$)")


def surface(path: str) -> str:
    """The path with ids taken out, cut to three segments: `/api/harness/runners/:id/…`."""
    shape = _ID.sub("/:id", path or "/")
    return "/".join(shape.split("/")[:5]) or "/"


def note(*, path: str, user=None, credential: dict | None = None, client: str = "",
         protocol: str = "http") -> None:
    """Record one call that arrived on the old address. Never raises."""
    if path.startswith("/health"):
        return
    try:
        who = (credential or {}).get("label") or ""
        if not who and user is not None and getattr(user, "is_authenticated", False):
            who = user.email or f"user {user.pk}"
        who = who or "anonymous"
        where = surface(path)
        key = f"{protocol}:{who}:{client[:60]}:{where}"[:255]
        if not cache.add(f"legacy-addr:{key}", 1, timeout=WRITE_EVERY_SECONDS):
            return
        from apps.events import services as events
        from apps.workspaces import services as wsvc

        workspace = (wsvc.user_default_workspace(user) if user is not None
                     and getattr(user, "is_authenticated", False) else None) \
            or wsvc.ensure_default_workspace()
        if workspace is None:
            return
        events.record([{
            "source": SOURCE,
            "kind": f"legacy_address.{protocol}",
            "level": "info",
            "key": key,
            "summary": f"{who} still calls the old address: {protocol} {where}"
                       + (f" ({client[:60]})" if client else ""),
            "payload": {"who": who, "client": client[:120], "surface": where, "protocol": protocol},
        }], workspace=workspace)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a request
        logger.warning("could not record legacy-address traffic", exc_info=True)
