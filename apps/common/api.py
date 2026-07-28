"""Django Ninja v2 router for the common surface (AI backend + /me/ + /health/)."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.realtime.models import PresencePreference, show_presence_for

from .schemas import (
    AiAuthCompleteIn,
    AiAuthCompleteOut,
    AiAuthPollOut,
    AiAuthStartOut,
    AiStatusOut,
    AiSwitchIn,
    AiSwitchOut,
    HealthOut,
    MeOut,
    PresencePreferenceIn,
    PresencePreferenceOut,
)

ai_router = Router(auth=session_auth, tags=["ai"])
common_router = Router(auth=session_auth, tags=["common"])
public_router = Router(tags=["public"])


# --- /health/ (public) -----------------------------------------


@public_router.get("/health/", auth=None, response=HealthOut, summary="Health check")
def health(request: HttpRequest) -> HealthOut:
    return HealthOut(status="ok")


# --- /me/ (auth) -----------------------------------------------


@common_router.get("/me/", response=MeOut, summary="Current user")
def me(request: HttpRequest) -> MeOut:
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


# --- AI backend (auth) -----------------------------------------


@ai_router.get("/status/", response=AiStatusOut, summary="AI backend status")
def ai_status(request: HttpRequest) -> AiStatusOut:
    backend = getattr(settings, "AI_BACKEND", "api")

    if backend == "cli":
        from .anthropic_client import cli_auth_status

        auth = cli_auth_status()
        if auth["logged_in"]:
            detail = "Authenticated via Claude subscription"
        elif not auth["installed"]:
            detail = "Claude CLI not installed"
        else:
            detail = "Sign in to connect your Claude subscription"
        return AiStatusOut(
            backend="cli",
            authenticated=auth["logged_in"],
            detail=detail,
        )
    else:
        has_key = bool(getattr(settings, "ANTHROPIC_API_KEY", ""))
        return AiStatusOut(
            backend="api",
            authenticated=has_key,
            detail="API key configured" if has_key else "No ANTHROPIC_API_KEY set",
        )


@ai_router.post("/switch/", response=AiSwitchOut, summary="Switch AI backend")
def ai_switch(request: HttpRequest, payload: AiSwitchIn) -> AiSwitchOut:
    # Switch at runtime by updating the Django setting
    settings.AI_BACKEND = payload.backend

    # Reset the cached API client so it picks up any changes
    from . import anthropic_client

    anthropic_client._client = None
    anthropic_client._consecutive_failures = 0

    return AiSwitchOut(backend=payload.backend)


@ai_router.post("/auth/start/", response=AiAuthStartOut, summary="Start Claude CLI auth")
def auth_start(request: HttpRequest) -> AiAuthStartOut:
    from . import auth_flow

    try:
        result = auth_flow.start()
    except FileNotFoundError:
        raise ProblemError(500, "Claude CLI not installed", detail="claude binary not found on PATH")
    except RuntimeError as e:
        raise ProblemError(500, "Auth start failed", detail=str(e))

    return AiAuthStartOut(
        auth_url=result.get("auth_url") or "",
        state=result.get("status", ""),
    )


@ai_router.post("/auth/complete/", response=AiAuthCompleteOut, summary="Complete Claude CLI auth")
def auth_complete(request: HttpRequest, payload: AiAuthCompleteIn) -> AiAuthCompleteOut:
    from . import auth_flow

    try:
        token = auth_flow.complete(payload.code or None)
        visible = token[:12] + "..." + token[-4:]
        return AiAuthCompleteOut(ok=True, detail=f"Authenticated. Token preview: {visible}")
    except RuntimeError as e:
        raise ProblemError(400, "Auth complete failed", detail=str(e))


@ai_router.get("/auth/poll/", response=AiAuthPollOut, summary="Poll Claude CLI auth status")
def auth_poll(request: HttpRequest) -> AiAuthPollOut:
    from . import auth_flow

    result = auth_flow.poll()

    # Map auth_flow.poll() dict → AiAuthPollOut
    # poll() returns: {active, authenticated, [elapsed_seconds]}
    if not result.get("active", False):
        if result.get("authenticated", False):
            state = "ok"
        else:
            state = "idle"
    else:
        state = "pending"

    return AiAuthPollOut(state=state)
