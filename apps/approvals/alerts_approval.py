"""Alert source `approval_request`: a request waiting on a person, shown wherever alerts show.

The request is the record; this makes it routable like any alarm — by the scopes its kind gave it
(`workspace:<slug>`, `agent:<slug>`) — so it appears on the workspace and agent screens for the
people who could approve it, with the kind's Approve / Decline as server buttons. A viewer who
could not approve it has no actions, and so does not see it. It resolves when the request is
decided, withdrawn or expires.
"""
from __future__ import annotations

from datetime import timedelta

from apps.attention.alerts import Action, Raise

from . import cards, services
from .models import ApprovalRequest

KIND = "approval_request"
SEVERITY = "action"
CONFIRM_WITHIN: timedelta | None = None
CLOSING_ACTION_IDS = ("approve", "decline")


def _subject(req: ApprovalRequest) -> str:
    return f"approval:{req.pk}"


def raise_for(req: ApprovalRequest) -> Raise:
    return Raise(
        subject_key=_subject(req), workspace=req.workspace_id, title=req.title, url=cards.url(req),
        scopes=tuple(req.scopes), body=req.summary, fingerprint=req.status,
        ask=f"{cards.requested_by(req) or 'Someone'} is asking for your decision.")


def reconcile(now) -> list[Raise]:
    pending = ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING).select_related(
        "requested_by_agent", "requested_by_user")
    return [raise_for(r) for r in pending]


def _request(alert) -> ApprovalRequest | None:
    try:
        pk = int(alert.subject_key.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    return ApprovalRequest.objects.filter(pk=pk, status=ApprovalRequest.PENDING).first()


def actions(alert, user) -> list[Action]:
    """Only people who hold the authority the request's kind needs."""
    req = _request(alert)
    if req is None or not services.may_approve(req, user):
        return []
    return [Action("approve", "Approve", True), Action("decline", "Decline", True),
            Action("open", "Open the whole request", False, href=cards.url(req))]


def perform(alert, user, action_id) -> None:
    req = _request(alert)
    if req is None:
        raise ValueError("this request is no longer waiting")
    if action_id not in ("approve", "decline"):
        raise ValueError(f"approval_request has no handler for {action_id!r}")
    try:
        services.decide(req.pk, user, action_id, via="alert")
    except services.ApprovalError as exc:
        raise ValueError(str(exc)) from exc
