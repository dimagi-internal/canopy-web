"""Django Ninja v2 router for the common surface (AI backend + /me/ + /health/)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.realtime.models import PresencePreference, show_presence_for

from .schemas import (
    HealthOut,
    MeOut,
    PresencePreferenceIn,
    PresencePreferenceOut,
)

common_router = Router(auth=session_auth, tags=["common"])
public_router = Router(tags=["public"])


# --- /health/ (public) -----------------------------------------


@public_router.get("/health/", auth=None, response=HealthOut, summary="Health check")
def health(request: HttpRequest) -> HealthOut:
    return HealthOut(status="ok")


# --- /me/ (auth) -----------------------------------------------


@common_router.get("/me/", response=MeOut, summary="Current user")
def me(request: HttpRequest) -> MeOut:
    from apps.workspaces.services import can_create_workspace

    user = request.user
    avatar_url = ""
    social = (
        user.socialaccount_set.filter(provider="google").first()
        if hasattr(user, "socialaccount_set")
        else None
    )
    if social:
        avatar_url = social.extra_data.get("picture", "") or ""
    return MeOut(
        email=user.email,
        name=(user.get_full_name() or user.username or user.email),
        avatar_url=avatar_url,
        can_create_workspace=can_create_workspace(user),
    )


# --- /me/presence-preference/ (auth) ----------------------------


@common_router.get(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Get the current user's presence visibility preference",
)
def get_presence_preference(request: HttpRequest) -> PresencePreferenceOut:
    return PresencePreferenceOut(show_presence=show_presence_for(request.user))


@common_router.patch(
    "/me/presence-preference/",
    response=PresencePreferenceOut,
    summary="Set the current user's presence visibility preference",
)
def set_presence_preference(
    request: HttpRequest, payload: PresencePreferenceIn
) -> PresencePreferenceOut:
    PresencePreference.objects.update_or_create(
        user=request.user, defaults={"show_presence": payload.show_presence}
    )
    return PresencePreferenceOut(show_presence=payload.show_presence)
