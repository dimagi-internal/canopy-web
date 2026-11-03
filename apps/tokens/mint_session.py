"""POST /api/debug/mint-session/ — a Django session cookie from a token.

What a minted session means to the rest of the app (it is still a machine) is
`apps/common/views_debug.py`; this is the view that makes one."""
import json

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.common.views_debug import DEBUG_SESSION_MARKER

from .middleware import minting_refusal

DEFAULT_TTL_SECONDS = 24 * 3600  # 24 hours
MAX_TTL_SECONDS = 7 * 24 * 3600  # 1 week


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
    # A cookie living a week, minted from an hour-long OAuth token or a site's
    # delegated token, would outlive both — and escape the site's surface limit.
    refused = minting_refusal(request)
    if refused:
        return JsonResponse({"detail": refused}, status=403)

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
        f'{origin}/api/agents/'
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
