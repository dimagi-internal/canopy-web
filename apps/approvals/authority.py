"""Who holds the authority a request needs — asked of the kind, never of the requester."""
from __future__ import annotations

from . import kinds
from .models import ApprovalRequest


def may_approve(req: ApprovalRequest, user) -> bool:
    """Whether `user` holds the authority this request's kind needs, right now."""
    if not getattr(user, "is_authenticated", False):
        return False
    try:
        k, p = kinds.parse(req.kind, req.params)
        return bool(k.may_approve(user, p, k.describe(p)))
    except kinds.KindError:
        return False


def approvers_of(req: ApprovalRequest) -> list:
    k, p = kinds.parse(req.kind, req.params)
    return k.approvers(p, k.describe(p))
