"""Filing, deciding, cancelling and expiring approval requests — the whole lifecycle, in one place.

`decide()` is the ONE door every channel uses (the page, a Slack button, an alert's button, the
API). It locks the request, checks the kind's own authority for THIS person, then either declines
(nothing runs) or runs the kind's hard-coded handler as the approver, inside a savepoint so a
refusal leaves nothing half-done. A request is decided once.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import cards, delivery, kinds
from .authority import approvers_of, may_approve  # noqa: F401 — re-exported: the one place callers ask
from .models import ApprovalEvent, ApprovalRequest

logger = logging.getLogger(__name__)

DEFAULT_TTL = dt.timedelta(days=7)
MAX_TTL = dt.timedelta(days=30)
QUOTE_MAX, WHO_MAX, REASON_MAX = 1500, 200, 4000
IF_DECLINED = "Nothing happens. The request is closed."
DECIDED_ELSEWHERE = "Already decided."


class ApprovalError(Exception):
    """A request or decision canopy refuses; `status` is the HTTP status for the API."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _event(request: ApprovalRequest, kind: str, actor=None, **payload) -> None:
    ApprovalEvent.objects.create(request=request, kind=kind, actor=actor if getattr(actor, "pk", None) else None,
                                 payload=payload)


def _evidence(items) -> list[dict]:
    out = []
    for e in items or []:
        quote = str(e.get("quote") or "").strip()
        if quote:
            out.append({"quote": quote[:QUOTE_MAX], "who": str(e.get("who") or "")[:WHO_MAX],
                        "source_url": str(e.get("source_url") or "")[:500]})
    return out[:5]


def create(*, kind: str, params: dict, requested_by_agent=None, requested_by_user=None, reason: str = "",
           evidence=None, source_url: str = "", for_email: str = "", expires_in_hours: int | None = None,
           idempotency_key: str | None = None) -> tuple[ApprovalRequest, bool]:
    """File a request. The kind validates the params and renders the preview; nothing is filed that
    nobody could approve. Returns (request, created) — a replayed idempotency key returns the original."""
    if idempotency_key:
        existing = ApprovalRequest.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing, False
    try:
        k, p = kinds.parse(kind, params)
        described = k.describe(p)
    except kinds.KindError as exc:
        raise ApprovalError(str(exc), 422) from exc
    approvers = k.approvers(p, described)
    for_user = None
    if for_email:
        for_user = next((u for u in approvers if (u.email or "").lower() == for_email.strip().lower()), None)
        if for_user is None:
            raise ApprovalError(f"{for_email} cannot approve this kind of request ({k.needs})", 422)
    if not approvers:
        raise ApprovalError(f"nobody can approve this: {k.label.lower()} needs {k.needs}", 422)
    ttl = min(dt.timedelta(hours=expires_in_hours), MAX_TTL) if expires_in_hours else DEFAULT_TTL
    try:
        with transaction.atomic():
            req = ApprovalRequest.objects.create(
                workspace_id=described.workspace, kind=k.slug, params=p.model_dump(), title=described.title[:300],
                summary=described.summary, plan=described.plan, if_declined=IF_DECLINED, scopes=described.scopes,
                reason=(reason or "").strip()[:REASON_MAX], evidence=_evidence(evidence),
                source_url=(source_url or "")[:500], requested_by_agent=requested_by_agent,
                requested_by_user=requested_by_user, for_user=for_user, expires_at=timezone.now() + ttl,
                idempotency_key=idempotency_key or None)
            _event(req, ApprovalEvent.CREATED, requested_by_user,
                   request_kind=k.slug, by_agent=getattr(requested_by_agent, "slug", None))
            pk = req.pk
            transaction.on_commit(lambda: _after_create(pk))
    except IntegrityError:
        # A concurrent filing with the same key won.
        existing = ApprovalRequest.objects.filter(idempotency_key=idempotency_key).first()
        if existing is None:
            raise
        return existing, False
    return req, True


def _after_create(pk: int) -> None:
    delivery.deliver_safely(pk)
    delivery.raise_alert_safely(pk)


def decide(request_id: int, user, decision: str, *, comment: str = "", via: str = "web") -> ApprovalRequest:
    if decision not in ("approve", "decline"):
        raise ApprovalError("decision must be approve or decline", 422)
    if not getattr(user, "is_authenticated", False):
        raise ApprovalError("sign in to decide this", 401)
    with transaction.atomic():
        req = ApprovalRequest.objects.select_for_update(of=("self",)).select_related("workspace").filter(
            pk=request_id).first()
        if req is None:
            raise ApprovalError(f"request {request_id} not found", 404)
        if not req.is_pending:
            raise ApprovalError(already_decided(req), 409)
        if req.expires_at and req.expires_at <= timezone.now():
            _close(req, ApprovalRequest.EXPIRED, None, via="", comment="", event=ApprovalEvent.EXPIRED)
            raise ApprovalError("This request expired before it was decided.", 409)
        try:
            k, p = kinds.parse(req.kind, req.params)
            described = k.describe(p)
        except kinds.KindError as exc:
            raise ApprovalError(f"This request is no longer valid: {exc}", 409) from exc
        if not k.may_approve(user, p, described):
            raise ApprovalError(f"You can't approve this: {k.label.lower()} needs {k.needs}.", 403)
        comment = (comment or "").strip()
        if decision == "decline":
            _close(req, ApprovalRequest.DECLINED, user, via=via, comment=comment, event=ApprovalEvent.DECLINED)
        else:
            _event(req, ApprovalEvent.APPROVED, user, via=via)
            try:
                with transaction.atomic():
                    result = k.execute(user, p, req)
            except kinds.ExecError as exc:
                _close(req, ApprovalRequest.FAILED, user, via=via, comment=comment, event=ApprovalEvent.FAILED,
                       error=str(exc))
            except Exception as exc:  # noqa: BLE001 — a handler bug must not leave a half-state
                logger.exception("approvals: %s handler failed for %s", req.kind, req.key)
                _close(req, ApprovalRequest.FAILED, user, via=via, comment=comment, event=ApprovalEvent.FAILED,
                       error="Something went wrong running this. Nothing was changed.")
                logger.debug("handler error was %r", exc)
            else:
                _close(req, ApprovalRequest.DONE, user, via=via, comment=comment, event=ApprovalEvent.EXECUTED,
                       result=result)
        pk = req.pk
        transaction.on_commit(lambda: _after_close(pk))
    return req


def _close(req: ApprovalRequest, status: str, user, *, via: str, comment: str, event: str,
           result: dict | None = None, error: str = "") -> None:
    req.status = status
    req.decided_by = user if getattr(user, "pk", None) else None
    req.decided_at = timezone.now()
    req.via = via
    req.comment = comment
    req.result = result or {}
    req.error = error
    req.save(update_fields=["status", "decided_by", "decided_at", "via", "comment", "result", "error", "updated_at"])
    _event(req, event, user, via=via, error=error, result=result or {})


def _after_close(pk: int) -> None:
    delivery.settle_safely(pk)


def already_decided(req: ApprovalRequest) -> str:
    return f"{DECIDED_ELSEWHERE} {cards.outcome_line(req)}"


def cancel(request_id: int, user) -> ApprovalRequest:
    """Withdrawn by whoever filed it, or by someone who could have approved it."""
    with transaction.atomic():
        req = ApprovalRequest.objects.select_for_update(of=("self",)).filter(pk=request_id).first()
        if req is None:
            raise ApprovalError(f"request {request_id} not found", 404)
        if not req.is_pending:
            raise ApprovalError(already_decided(req), 409)
        filer = (req.requested_by_user_id == user.pk
                 or (req.requested_by_agent_id and req.requested_by_agent.user_id == user.pk))
        if not (filer or may_approve(req, user)):
            raise ApprovalError("only whoever filed this, or someone who could approve it, can withdraw it", 403)
        _close(req, ApprovalRequest.CANCELLED, user, via="", comment="", event=ApprovalEvent.CANCELLED)
        pk = req.pk
        transaction.on_commit(lambda: _after_close(pk))
    return req


def expire_due(now=None) -> int:
    """Close every pending request past its time. Returns how many."""
    now = now or timezone.now()
    n = 0
    for pk in list(ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING, expires_at__lte=now)
                   .values_list("pk", flat=True)[:100]):
        with transaction.atomic():
            req = ApprovalRequest.objects.select_for_update(of=("self",)).filter(
                pk=pk, status=ApprovalRequest.PENDING).first()
            if req is None:
                continue
            _close(req, ApprovalRequest.EXPIRED, None, via="", comment="", event=ApprovalEvent.EXPIRED)
            transaction.on_commit(lambda pk=pk: _after_close(pk))
            n += 1
    return n


def user_by_pk(pk):
    return get_user_model().objects.filter(pk=pk).first()
