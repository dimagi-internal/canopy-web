"""Send a request to the people who can approve it, once each, and keep every copy honest.

Order of reach: a push and a Slack DM for people who have those; an email only to someone neither
reached (so a person with a phone is not also mailed). Everything is idempotent — a (request,
person, channel) is delivered once — and a send that fails never breaks the filing that triggered it.
"""
from __future__ import annotations

import datetime as dt
import logging
from importlib import import_module

from django.utils import timezone

from apps.attention import alerts
from apps.push.models import NotificationPreference

from . import cards, kinds
from .models import ApprovalDelivery, ApprovalEvent, ApprovalRequest

logger = logging.getLogger(__name__)

#: channel -> module with send(request, user, card) -> ref | None  (and, optionally, settle(delivery, card)).
CHANNEL_MODULES = {
    ApprovalDelivery.PUSH: "apps.approvals.channel_push",
    ApprovalDelivery.SLACK: "apps.slack.approvals",
    ApprovalDelivery.EMAIL: "apps.approvals.channel_email",
}
FIRST_WAVE = (ApprovalDelivery.PUSH, ApprovalDelivery.SLACK)
MAX_RECIPIENTS = 5
BACKSTOP_WINDOW = dt.timedelta(hours=6)


def _send(channel: str, req: ApprovalRequest, user, card: dict) -> dict | None:
    try:
        return import_module(CHANNEL_MODULES[channel]).send(req, user, card)
    except Exception:  # noqa: BLE001 — one channel failing must not stop the others
        logger.exception("approvals: %s send failed for %s", channel, req.key)
        return None


def wants(user) -> bool:
    pref = NotificationPreference.objects.filter(user=user).first()
    return pref is None or pref.approval_cards


def recipients(req: ApprovalRequest) -> list:
    """Who is told: the person it was routed to, else everyone who could approve it (capped)."""
    if req.for_user_id:
        return [req.for_user]
    k, p = kinds.parse(req.kind, req.params)
    return k.approvers(p, k.describe(p))[:MAX_RECIPIENTS]


def deliver(request_id: int) -> dict[str, int]:
    """Deliver the request to its approvers. Returns {channel: copies sent}."""
    req = (ApprovalRequest.objects.select_related("workspace", "requested_by_agent", "requested_by_user", "for_user")
           .filter(pk=request_id).first())
    if req is None or not req.is_pending:
        return {}
    sent = {c: 0 for c in ApprovalDelivery.CHANNELS}
    for user in (u for u in recipients(req) if wants(u)):
        card = cards.build(req, viewer=user)
        have = set(ApprovalDelivery.objects.filter(request=req, user=user).values_list("channel", flat=True))
        for channel in FIRST_WAVE:
            if channel in have:
                continue
            ref = _send(channel, req, user, card)
            if ref is not None:
                ApprovalDelivery.objects.get_or_create(request=req, user=user, channel=channel,
                                                       defaults={"ref": ref})
                have.add(channel)
                sent[channel] += 1
        if not have:
            ref = _send(ApprovalDelivery.EMAIL, req, user, card)
            if ref is not None:
                ApprovalDelivery.objects.get_or_create(request=req, user=user, channel=ApprovalDelivery.EMAIL,
                                                       defaults={"ref": ref})
                sent[ApprovalDelivery.EMAIL] += 1
    if any(sent.values()):
        ApprovalEvent.objects.create(request=req, kind=ApprovalEvent.DELIVERED, payload=sent)
    return sent


def deliver_safely(request_id: int) -> None:
    try:
        deliver(request_id)
    except Exception:  # noqa: BLE001 — a notification never costs the filing that raised it
        logger.exception("approvals: delivery failed for request %s", request_id)


def raise_alert_safely(request_id: int) -> None:
    """Show the request wherever alerts show for its scopes, now rather than on the next tick."""
    from . import alerts_approval  # noqa: PLC0415 — alerts_approval imports services, which imports this module

    try:
        req = ApprovalRequest.objects.filter(pk=request_id, status=ApprovalRequest.PENDING).first()
        if req is not None:
            alerts.raise_now(alerts_approval.KIND, alerts_approval.raise_for(req))
    except Exception:  # noqa: BLE001
        logger.exception("approvals: could not raise the alert for request %s", request_id)


def settle(request_id: int) -> int:
    """Bring every sent copy up to date once the request is closed. Returns copies updated."""
    req = (ApprovalRequest.objects.select_related("workspace", "requested_by_agent", "requested_by_user",
                                                  "decided_by").filter(pk=request_id).first())
    if req is None or req.is_pending:
        return 0
    card = cards.build(req)
    n = 0
    for d in ApprovalDelivery.objects.filter(request=req, settled_at__isnull=True).select_related("user"):
        try:
            mod = import_module(CHANNEL_MODULES[d.channel])
            if hasattr(mod, "settle"):
                mod.settle(d, card)
        except Exception:  # noqa: BLE001 — the decision stands; a stale copy is a courtesy lost
            logger.exception("approvals: settle failed for %s via %s", req.key, d.channel)
            continue
        ApprovalDelivery.objects.filter(pk=d.pk).update(settled_at=timezone.now())
        n += 1
    return n


def settle_safely(request_id: int) -> None:
    try:
        settle(request_id)
    except Exception:  # noqa: BLE001
        logger.exception("approvals: settle failed for request %s", request_id)


def deliver_missed(now=None) -> int:
    """The backstop: recent pending requests nobody has been told about yet."""
    now = now or timezone.now()
    missed = (ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING, created_at__gte=now - BACKSTOP_WINDOW)
              .exclude(deliveries__isnull=False).order_by("pk")[:50])
    n = 0
    for req in missed:
        deliver_safely(req.pk)
        n += 1
    return n
