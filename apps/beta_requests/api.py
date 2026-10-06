from django.http import HttpRequest
from ninja import Router

from apps.api.errors import TYPE_RATE_LIMIT, ProblemError
from apps.tokens.audit import client_ip

from . import services
from .schemas import BetaRequestIn, BetaRequestOut

router = Router(tags=["beta"])


# `auth=None` is half the story: apps/common/middleware.py is default-deny, so this
# path is also allowlisted there. The response is identical for a first request and
# a repeat, so the form cannot be used to learn who has already asked.
@router.post("/beta-requests", response=BetaRequestOut, auth=None,
             summary="Request access to the closed beta (anonymous)")
def submit_beta_request(request: HttpRequest, payload: BetaRequestIn) -> dict:
    """Ask to join canopy's closed beta.

    Records the request and notifies the team that runs the beta, who reply by
    email. Granting access is a separate, ordinary workspace invite.
    """
    if payload.website:
        return {"ok": True}
    try:
        services.submit(
            email=str(payload.email), reason=payload.reason,
            client_ip=client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except services.TooManyRequests:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT,
                           detail="Too many requests from this address. Try again later.")
    return {"ok": True}
