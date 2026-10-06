"""Django Ninja router for /api/push — Web Push subscription registry."""
from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_VALIDATION, ProblemError

from .models import NotificationPreference, PushSubscription, session_idle_minutes_for
from .schemas import (
    NotificationPreferenceIn,
    NotificationPreferenceOut,
    PushSubscribeIn,
    PushUnsubscribeIn,
    VapidKeyOut,
)

router = Router(auth=session_auth, tags=["push"])


def _push_configured() -> bool:
    """BOTH keys, deliberately: sending checks only the private key
    (services.send_to_user), so a public-key-only deployment used to accept
    subscriptions here and then silently never send — half-configured must
    read as not configured."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def _refuse_unsendable(endpoint: str) -> None:
    # canopy POSTs to every stored endpoint from inside the VPC, so an endpoint
    # is an outbound URL a signed-in person typed — the blind-SSRF shape the
    # host grant flow already guards (apps/tokens/outbound.py). A real browser's
    # endpoint is always https on a public push service (FCM, Mozilla, Apple,
    # WNS), so this refuses nothing a browser hands us. Deliberately NOT an
    # allowlist of those hosts: a new push service must not need a deploy.
    from apps.tokens.outbound import OutboundError, refuse_private

    parsed = urlparse((endpoint or "").strip())
    try:
        if parsed.scheme != "https" or not parsed.hostname:
            raise OutboundError("a push endpoint must be an https URL")
        if parsed.username or parsed.password:
            raise OutboundError("a push endpoint must not carry credentials")
        refuse_private(parsed.hostname)
    except OutboundError as exc:
        raise ProblemError(422, "Invalid push endpoint", type_=TYPE_VALIDATION,
                           detail=str(exc)) from exc


@router.get("/vapid-public-key", response=VapidKeyOut, summary="The VAPID public key")
def vapid_public_key(request: HttpRequest) -> VapidKeyOut:
    """The browser needs this to subscribe. Not a secret — it ships in the JS
    bundle anyway. 503 when unconfigured so a push-less deployment says so
    plainly rather than handing the browser an empty key it would fail on."""
    if not _push_configured():
        raise HttpError(503, "push is not configured")
    return VapidKeyOut(public_key=settings.VAPID_PUBLIC_KEY)


@router.post("/subscribe", response={201: None}, summary="Register this browser for push")
def subscribe(request: HttpRequest, payload: PushSubscribeIn):
    """Upsert on endpoint. The browser re-sends the same endpoint on every
    subscribe() call, and its keys rotate — so update rather than insert, and
    re-point the row at the caller: the endpoint belongs to the BROWSER, not the
    person, so on a shared device it must follow whoever is logged in now."""
    if not _push_configured():
        raise HttpError(503, "push is not configured")
    _refuse_unsendable(payload.endpoint)
    PushSubscription.objects.update_or_create(
        endpoint=payload.endpoint,
        defaults={
            "user": request.user,
            "p256dh": payload.p256dh,
            "auth": payload.auth,
            "user_agent": payload.user_agent[:300],
            "failure_count": 0,
        },
    )
    return 201, None


@router.delete("/subscribe", response={204: None}, summary="Unregister this browser")
def unsubscribe(request: HttpRequest, payload: PushUnsubscribeIn):
    """Idempotent, and scoped to the caller: unsubscribing an endpoint you don't
    own is a silent no-op, not a 404 — no existence leak either way."""
    PushSubscription.objects.filter(endpoint=payload.endpoint, user=request.user).delete()
    return 204, None


@router.get("/preferences", response=NotificationPreferenceOut, summary="Your notification settings")
def get_preferences(request: HttpRequest) -> NotificationPreferenceOut:
    """`session_idle_minutes`: how long a chat must stay quiet after its agent
    finishes before you are notified that it is done (0 = never)."""
    return NotificationPreferenceOut(session_idle_minutes=session_idle_minutes_for(request.user))


@router.patch("/preferences", response=NotificationPreferenceOut, summary="Change your notification settings")
def set_preferences(request: HttpRequest, payload: NotificationPreferenceIn) -> NotificationPreferenceOut:
    """Set how many quiet minutes (0–1440, 0 = never) before a finished chat notifies you."""
    NotificationPreference.objects.update_or_create(
        user=request.user, defaults={"session_idle_minutes": payload.session_idle_minutes}
    )
    return NotificationPreferenceOut(session_idle_minutes=payload.session_idle_minutes)
