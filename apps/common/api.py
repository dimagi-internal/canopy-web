"""Django Ninja router for the common surface: /health/. The caller's own page (`/me/…`) is
`apps/tokens/api_me.py` — it reads the harness, realtime, contacts and sessions."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.route_gates import gate

from .schemas import HealthOut

public_router = Router(tags=["public"])


# --- /health/ (public) -----------------------------------------


@public_router.get("/health/", auth=None, response=HealthOut, summary="Health check")
@gate("anonymous")
def health(request: HttpRequest) -> HealthOut:
    return HealthOut(status="ok")
