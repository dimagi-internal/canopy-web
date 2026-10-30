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
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup

from . import operators, reader, stats
from .schemas import (
    CapabilityCatalogOut,
    CapabilityDetailOut,
    OperatorAddIn,
    OperatorOut,
    PublicStatsOut,
)

router = Router(auth=session_auth, tags=["system"])

_KINDS = {"skill", "agent", "command"}


@router.get("/overview", response=CapabilityCatalogOut)
@gate("authenticated")  # canopy plugin catalog, not tenant data
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
@gate("anonymous")
def public_stats(request: HttpRequest) -> dict:
    """Anonymous, aggregates only.

    NOTE: `auth=None` is half the story — apps/common/middleware.py is
    default-deny, so this path is also allowlisted there. Both are required.

    Declared above `detail()` for readability. `/{kind}/{name}` is two path
    segments, so it cannot capture this single-segment route — the order is
    not load-bearing.
    """
    return stats.public_stats()


# --- site operators (superusers) -------------------------------------------------
# Declared above `detail()`: `/operators/{user_id}` is two segments, and Django
# tries patterns in order, so `/{kind}/{name}` would otherwise capture it.


def _operator_error(exc: operators.OperatorError) -> HttpError:
    return HttpError({"not_found": 404, "last": 409}[exc.code], exc.message)


def _operator_out(request: HttpRequest, user_id: int) -> dict:
    return next(o for o in operators.operators(request.user) if o["user_id"] == user_id)


@router.get("/operators", response=list[OperatorOut], summary="List superusers (superuser only)")
@gate("superuser")
def list_operators(request: HttpRequest) -> list[dict]:
    """Everyone who holds the fleet-wide switches."""
    if not request.user.is_superuser:
        raise HttpError(403, "only a superuser may see who the superusers are")
    return operators.operators(request.user)


@router.post("/operators", response={201: OperatorOut}, summary="Make someone a superuser (superuser only)")
@gate("superuser", setting=True, step_up=True)
def add_operator(request: HttpRequest, payload: OperatorAddIn):
    """Grant superuser to the account at `email`. They must have signed in to Canopy
    once (404 otherwise). Superuser holds the fleet switches only — no workspace
    access, which is still a membership. Needs a fresh passkey."""
    if not request.user.is_superuser:
        raise HttpError(403, "only a superuser may make someone a superuser")
    stepup.require_recent_step_up(request)
    try:
        user = operators.grant(payload.email)
    except operators.OperatorError as exc:
        raise _operator_error(exc)
    return 201, _operator_out(request, user.pk)


@router.delete("/operators/{user_id}", response={204: None}, summary="Remove a superuser (superuser only)")
@gate("superuser", setting=True, step_up=True)
def remove_operator(request: HttpRequest, user_id: int):
    """Revoke superuser. Refused (409) for the last one — someone must be able to
    release a fleet hold. Needs a fresh passkey."""
    if not request.user.is_superuser:
        raise HttpError(403, "only a superuser may remove a superuser")
    stepup.require_recent_step_up(request)
    try:
        operators.revoke(user_id)
    except operators.OperatorError as exc:
        raise _operator_error(exc)
    return 204, None


@router.get("/{kind}/{name}", response=CapabilityDetailOut)
@gate("authenticated")
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
