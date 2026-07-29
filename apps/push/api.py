"""Django Ninja router for /api/push — Web Push subscription registry."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth

from .models import PushSubscription
from .schemas import PushSubscribeIn, PushUnsubscribeIn, VapidKeyOut

router = Router(auth=session_auth, tags=["push"])


def _push_configured() -> bool:
    """BOTH keys, deliberately: sending checks only the private key
    (services.send_to_user), so a public-key-only deployment used to accept
    subscriptions here and then silently never send — half-configured must
    read as not configured."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


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
