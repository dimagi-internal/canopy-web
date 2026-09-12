"""GET /api/system/ — the canopy capability catalog (skills / agents / commands).

Read-only, session-authed. Reads the bundled canopy plugin at
``settings.CANOPY_PLUGIN_PATH`` so the catalog always reflects the plugin's
actual file structure (no push, no hardcoded registry). See
:mod:`apps.system.reader`.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError

from . import reader, stats
from .schemas import CapabilityCatalogOut, CapabilityDetailOut, PublicStatsOut

router = Router(auth=session_auth, tags=["system"])

_KINDS = {"skill", "agent", "command"}


@router.get("/overview", response=CapabilityCatalogOut)
def overview(request: HttpRequest) -> dict:
    """Full capability catalog grouped by family (list shape — no markdown body)."""
    cat = reader.load_catalog(settings.CANOPY_PLUGIN_PATH)
    return {
        **cat,
        "items": [{k: v for k, v in item.items() if k != "body"} for item in cat["items"]],
    }


@router.get(
    "/public-stats",
    response=PublicStatsOut,
    auth=None,
    summary="Aggregate counts for the public explainer (anonymous)",
)
def public_stats(request: HttpRequest) -> dict:
    """Anonymous, aggregates only.

    NOTE: `auth=None` is half the story — apps/common/middleware.py is
    default-deny, so this path is also allowlisted there. Both are required.

    Declared above `detail()` for readability. `/{kind}/{name}` is two path
    segments, so it cannot capture this single-segment route — the order is
    not load-bearing.
    """
    return stats.public_stats()


@router.get("/{kind}/{name}", response=CapabilityDetailOut)
def detail(request: HttpRequest, kind: str, name: str) -> dict:
    """One capability with its full markdown body."""
    if kind not in _KINDS:
        raise ProblemError(
            404, "Not found", type_=TYPE_NOT_FOUND,
            detail=f"Unknown capability kind '{kind}'.",
        )
    item = reader.get_item(settings.CANOPY_PLUGIN_PATH, kind, name)
    if item is None:
        raise ProblemError(
            404, "Not found", type_=TYPE_NOT_FOUND,
            detail=f"No {kind} named '{name}' in the canopy plugin.",
        )
    return item
