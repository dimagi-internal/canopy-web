"""Minting a browser session from a token.

POST /api/debug/mint-session/ turns an authenticated caller — in practice a PAT
— into a short-lived Django session cookie. Its one consumer is the macOS
menubar app, which trades the runner's PAT for a cookie so its web view opens
/supervisor already signed in; a bearer header cannot log a web view in.

It used to be a Settings button for handing a cookie to an AI assistant. That
button is gone (2026-09-25): an assistant working the API uses a short-lived PAT.

A MINTED SESSION IS STILL A MACHINE. The few "canopy web app only" decisions
(transfer an agent's owner, change its admins) refuse any Authorization header,
so a leaked token cannot take over an agent — and before this, any PAT could
walk straight past that by minting a cookie first. Every minted session carries
`DEBUG_SESSION_MARKER`, and `is_machine(request)` treats it exactly like a
bearer, so those gates hold for a person signed in through Google and nobody else.
"""
import json

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

DEFAULT_TTL_SECONDS = 24 * 3600  # 24 hours
MAX_TTL_SECONDS = 7 * 24 * 3600  # 1 week
DEBUG_SESSION_MARKER = "_canopy_debug_session"


def is_machine(request) -> bool:
    """True when a token, not a person signed in through the browser, is behind
    this request: any Authorization header, or a session minted from a token."""
    if request.META.get("HTTP_AUTHORIZATION"):
        return True
    session = getattr(request, "session", None)
    try:
        return bool(session is not None and session.get(DEBUG_SESSION_MARKER))
    except Exception:  # noqa: BLE001 — an unreadable session is not a person
        return True


def _cookie_name() -> str:
    return getattr(settings, "SESSION_COOKIE_NAME", "sessionid")


@require_POST
def mint_session(request):
    """POST /api/debug/mint-session/

    Creates a new Django session authenticated as the caller. Returns the
    session key, a curl example, and the expiry timestamp.

    Body (optional): {"ttl_seconds": int} — clamped to MAX_TTL_SECONDS.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Sign in required."}, status=401)

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    try:
        ttl = int(body.get("ttl_seconds", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_SECONDS
    ttl = max(60, min(ttl, MAX_TTL_SECONDS))

    user = request.user
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = (
        getattr(user, "backend", None)
        or settings.AUTHENTICATION_BACKENDS[0]
    )
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session[DEBUG_SESSION_MARKER] = {
        "minted_at": timezone.now().isoformat(),
        "minted_for_email": user.email,
    }
    session.set_expiry(ttl)
    session.save()

    cookie_name = _cookie_name()
    origin = f"{request.scheme}://{request.get_host()}"
    curl_example = (
        f'curl -H "Cookie: {cookie_name}={session.session_key}" '
        f'{origin}/api/projects/'
    )

    return JsonResponse({
        "cookie_name": cookie_name,
        "cookie_value": session.session_key,
        "origin": origin,
        "expires_at": (
            timezone.now() + timezone.timedelta(seconds=ttl)
        ).isoformat(),
        "ttl_seconds": ttl,
        "email": user.email,
        "curl_example": curl_example,
    })
