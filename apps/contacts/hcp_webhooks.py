"""Telling a client its grant was revoked: the signed, retried webhook (HCP 4.2.3).

When a person (or an admin, or a replayed refresh token) revokes a grant,
`hcp_oauth.revoke` records an `HcpRevocationDelivery` for the client's webhook
and calls `_deliver_soon` once the revocation has committed. The revocation never
waits on the client: the first attempt runs on a background thread, and every
later one comes from the off-request clock.

Start reading at `attempt` (one signed POST and what it records: delivered,
rescheduled by `WEBHOOK_BACKOFF`, or abandoned after `WEBHOOK_MAX_ATTEMPTS`),
then `sign` (the `HCP-Signature` header) and `deliver_due`. `maybe_deliver` is
the clock's entry point (`apps/contacts/apps.py`, at most once a minute across
the fleet). The design record is docs/architecture/hcp-service.md.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import uuid

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import hcp
from .models import HcpClient, HcpRevocationDelivery

logger = logging.getLogger(__name__)

#: 4.2.3: at least 5 attempts over at least an hour. The waits between attempts;
#: after the last wait's attempt fails, the delivery is abandoned — 6 attempts
#: spanning about 1 h 51 min.
WEBHOOK_BACKOFF = [dt.timedelta(minutes=m) for m in (1, 5, 15, 30, 60)]
WEBHOOK_MAX_ATTEMPTS = len(WEBHOOK_BACKOFF) + 1
WEBHOOK_TIMEOUT_S = 5


def _deliver_soon(delivery_id: int) -> None:
    import threading

    threading.Thread(target=_attempt_by_id, args=(delivery_id,), daemon=True).start()


def _attempt_by_id(delivery_id: int) -> None:
    from django.db import close_old_connections

    try:
        d = HcpRevocationDelivery.objects.select_related("client", "grant__person").filter(pk=delivery_id).first()
        if d is not None:
            attempt(d)
    except Exception:  # noqa: BLE001
        logger.exception("hcp: revocation delivery %s failed", delivery_id)
    finally:
        close_old_connections()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """4.2.3: hex HMAC-SHA256 over `<timestamp>.<raw body>`."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def webhook_secret(client: HcpClient) -> str:
    from apps.common.encryption import decrypt_secret as decrypt

    return decrypt(client.webhook_secret_encrypted) if client.webhook_secret_encrypted else ""


def _post(url: str, body: bytes, headers: dict) -> int:
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:  # noqa: S310 — admin-registered https URL
        return resp.status


def attempt(d: HcpRevocationDelivery, *, now: dt.datetime | None = None, post=None) -> None:
    """One delivery attempt. Same payload every time; a fresh HCP-Delivery-Id each
    attempt. Audits `revocation.notified` on first success and on abandonment."""
    now = now or timezone.now()
    with transaction.atomic():
        d = HcpRevocationDelivery.objects.select_for_update(of=("self",)).select_related(
            "client", "grant__person").get(pk=d.pk)
        if d.delivered_at or d.abandoned_at or d.next_attempt_at > now:
            return
        body = json.dumps(d.payload, sort_keys=True, separators=(",", ":")).encode()
        ts = int(now.timestamp())
        headers = {"Content-Type": "application/json", "HCP-Timestamp": str(ts),
                   "HCP-Signature": sign(webhook_secret(d.client), ts, body),
                   "HCP-Delivery-Id": str(uuid.uuid4())}
        try:
            status = (post or _post)(d.client.webhook_url, body, headers)
            ok, note = 200 <= status < 300, f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 — any failure is a retry
            ok, note = False, f"{type(exc).__name__}: {exc}"[:200]
        d.attempts += 1
        d.last_status = note
        fields = ["attempts", "last_status"]
        if ok:
            d.delivered_at = now
            fields.append("delivered_at")
            hcp.audit(d.grant.person, "revocation.notified", actor=hcp.SYSTEM, grant=d.grant,
                      detail=f"delivered to {d.client.name} after {d.attempts} attempt(s): {note}")
        elif d.attempts >= WEBHOOK_MAX_ATTEMPTS:
            d.abandoned_at = now
            fields.append("abandoned_at")
            hcp.audit(d.grant.person, "revocation.notified", actor=hcp.SYSTEM, grant=d.grant,
                      detail=f"abandoned after {d.attempts} attempts over "
                             f"{(now - d.created_at).total_seconds() / 3600:.1f}h: {note}")
        else:
            d.next_attempt_at = now + WEBHOOK_BACKOFF[d.attempts - 1]
            fields.append("next_attempt_at")
        d.save(update_fields=fields)


def deliver_due(now: dt.datetime | None = None, limit: int = 20) -> int:
    """Attempt every delivery that is due. Run from the off-request clock
    (`maybe_deliver`), like the retention sweep."""
    now = now or timezone.now()
    due = list(HcpRevocationDelivery.objects.filter(
        delivered_at__isnull=True, abandoned_at__isnull=True, next_attempt_at__lte=now)
        .order_by("next_attempt_at")[:limit])
    for d in due:
        try:
            attempt(d, now=now)
        except Exception:  # noqa: BLE001
            logger.exception("hcp: revocation delivery %s failed", d.pk)
    return len(due)


def maybe_deliver(now: dt.datetime | None = None) -> None:
    """The off-request clock's entry point: at most once a minute across the fleet. Never raises."""
    try:
        if not cache.add("hcp-revocation-deliveries", "1", timeout=60):
            return
        deliver_due(now)
    except Exception:  # noqa: BLE001
        logger.exception("hcp: revocation deliveries failed")
