"""Passkey step-up: "a person, at a browser, just touched their passkey."

The one helper other apps call is `require_recent_step_up(request)`. It raises
a 403 problem whose top-level `error` is `"step_up_required"`; the SPA's API
client catches exactly that, asks for the passkey (navigator.credentials.get),
and retries the original request (`frontend/src/auth/stepUp.ts`).

The window lives in the Django SESSION, never in a cookie of its own and never
on a token: a step-up is a property of one signed-in browser, and lasts
`STEP_UP_SECONDS`. It is stamped with the user it was earned by, so it cannot
survive a session changing hands.

A TOKEN can never hold a step-up — there is no browser behind it to ask. So a
machine caller (any Authorization header, an MCP call, a session minted from a
token; `apps.common.views_debug.is_machine`) is refused outright by
`require_recent_step_up`, with a message that says why rather than one that
invites a passkey prompt nothing can answer. Routes that let tokens through
without a step-up (a PAT writing a credential from `canopy vault import`)
decide that themselves, before calling this — see `apps/agents/api.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.http import HttpRequest
from django.utils import timezone

from apps.api.errors import TYPE_FORBIDDEN, TYPE_STEP_UP_REQUIRED, ProblemError

#: How long one passkey touch covers. The issue's contract: five minutes.
STEP_UP_SECONDS = 5 * 60
SESSION_KEY = "passkeys.step_up"
#: Where a person registers their first passkey (the SPA's account settings).
REGISTER_PATH = "/settings#passkeys"
STEP_UP_REQUIRED = "step_up_required"
BROWSER_REQUIRED = "browser_session_required"


class StepUpRequired(ProblemError):
    """403 `{"error": "step_up_required", ...}` — confirm with a passkey, then retry."""

    def __init__(self, *, has_passkey: bool) -> None:
        detail = (
            "Confirm it's you with your passkey, then try again."
            if has_passkey else
            "This needs a passkey confirmation, and you have no passkey yet. "
            "Register one for this device (Settings → Passkeys), then try again."
        )
        super().__init__(
            403, "Passkey confirmation required", type_=TYPE_STEP_UP_REQUIRED, detail=detail,
            members={
                "error": STEP_UP_REQUIRED,
                "has_passkey": has_passkey,
                "register_url": REGISTER_PATH,
                "step_up_seconds": STEP_UP_SECONDS,
            },
        )


def is_browser(request: HttpRequest) -> bool:
    """A person signed in through the browser — not a token, an MCP call, or a
    session minted from a token."""
    from apps.common.views_debug import is_machine

    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated) and not is_machine(request)


def require_browser(request: HttpRequest, what: str = "this") -> None:
    if not is_browser(request):
        raise ProblemError(
            403, "A browser session is required", type_=TYPE_FORBIDDEN,
            detail=(f"{what} needs a person at a browser confirming with a passkey; "
                    "a token or a token-minted session cannot."),
            members={"error": BROWSER_REQUIRED},
        )


def mark_stepped_up(request: HttpRequest, *, now: datetime | None = None) -> datetime:
    """Record a successful passkey assertion on this session; returns the expiry."""
    until = (now or timezone.now()) + timedelta(seconds=STEP_UP_SECONDS)
    request.session[SESSION_KEY] = {"user": request.user.pk, "until": until.timestamp()}
    return until


def step_up_until(request: HttpRequest, *, now: datetime | None = None) -> datetime | None:
    """When this session's step-up expires, or None if it has none in force."""
    session = getattr(request, "session", None)
    user = getattr(request, "user", None)
    if session is None or user is None or not user.is_authenticated:
        return None
    try:
        raw = session.get(SESSION_KEY)
    except Exception:  # noqa: BLE001 - an unreadable session holds no step-up
        return None
    if not isinstance(raw, dict) or raw.get("user") != user.pk:
        return None
    try:
        until = datetime.fromtimestamp(float(raw.get("until")), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        return None
    return until if until > (now or timezone.now()) else None


def has_recent_step_up(request: HttpRequest) -> bool:
    return is_browser(request) and step_up_until(request) is not None


def require_recent_step_up(request: HttpRequest) -> None:
    """Pass when this browser session confirmed a passkey in the last five
    minutes; else raise `StepUpRequired` (or a plain 403 for a machine caller)."""
    require_browser(request)
    if step_up_until(request) is not None:
        return
    from apps.passkeys.models import Passkey

    raise StepUpRequired(has_passkey=Passkey.objects.filter(user=request.user).exists())
