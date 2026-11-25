"""The card: everything a person needs to decide a request, in one place.

One renderer, so the page, the Slack message, the push, the email, the alert and the API all say
the same thing. The preview (`plan`, `title`, `summary`) is the server's rendering of the typed
params; the requester's own words (`reason`, `evidence`) are kept apart and labelled as theirs.
"""
from __future__ import annotations

from apps.workspaces.links import scoped_path, scoped_url

from . import kinds
from .authority import may_approve
from .models import ApprovalDelivery, ApprovalRequest

_VIA = {"slack": "Slack", "web": "the web", "email": "email", "push": "a notification",
        "alert": "an alert", "api": "the API"}


def url(req: ApprovalRequest) -> str:
    return scoped_path(req.workspace_id, f"/approvals/{req.pk}")


def page_url(req: ApprovalRequest) -> str:
    """`url`, absolute: what a Slack message, an email or an agent's reply links to."""
    return scoped_url(req.workspace_id, f"/approvals/{req.pk}")


#: The words every channel uses for the one way to approve a kind with `Kind.step_up`.
STEP_UP_APPROVE = "Open in canopy to approve (passkey)"


def who(user) -> str:
    if user is None:
        return "someone"
    return (user.get_full_name() or "").strip() or user.email or user.get_username()


def requested_by(req: ApprovalRequest) -> str:
    if req.requested_by_agent_id:
        return req.requested_by_agent.name or req.requested_by_agent.slug
    return who(req.requested_by_user) if req.requested_by_user_id else ""


def outcome_line(req: ApprovalRequest) -> str:
    """One plain sentence for a request that is no longer pending."""
    by, via = who(req.decided_by), _VIA.get(req.via, req.via)
    where = f" via {via}" if via else ""
    return {
        ApprovalRequest.DONE: f"Approved by {by}{where} and done.",
        ApprovalRequest.FAILED: f"Approved by {by}{where}, but it could not be done: {req.error}",
        ApprovalRequest.DECLINED: f"Declined by {by}{where}.",
        ApprovalRequest.CANCELLED: "Withdrawn before anyone decided it.",
        ApprovalRequest.EXPIRED: "Expired without a decision.",
    }.get(req.status, "Waiting for a decision.")


def build(req: ApprovalRequest, *, viewer=None) -> dict:
    """The card as a dict matching `ApprovalCardOut`. `viewer` fills `can_decide`.

    `step_up` says approving needs a passkey in a browser: every channel then offers a link to the
    request's page (`STEP_UP_APPROVE`) where it would offer Approve, and keeps Decline, which needs none."""
    try:
        kind = kinds.get(req.kind)
        label, needs, step_up = kind.label, kind.needs, kind.step_up
    except kinds.KindError:
        label, needs, step_up = req.kind, "", False
    sent = {}
    for d in req.deliveries.select_related("user"):
        sent.setdefault(who(d.user), set()).add(d.channel)
    decided = None
    if not req.is_pending:
        decided = {"by": who(req.decided_by) if req.decided_by_id else "", "via": req.via, "at": req.decided_at,
                   "comment": req.comment}
    return {
        "id": req.pk, "key": req.key, "kind": req.kind, "kind_label": label, "needs": needs,
        "title": req.title, "summary": req.summary, "status": req.status, "workspace": req.workspace_id,
        "url": url(req), "page_url": page_url(req), "step_up": step_up,
        "requested_by": requested_by(req), "reason": req.reason, "evidence": req.evidence,
        "plan": req.plan, "if_declined": req.if_declined, "source_url": req.source_url,
        "created_at": req.created_at, "expires_at": req.expires_at,
        "sent_to": [f"{name} ({', '.join(sorted(ch))})" for name, ch in sorted(sent.items())],
        "decided": decided, "outcome": outcome_line(req), "result": req.result, "error": req.error,
        "can_decide": bool(req.is_pending and viewer is not None and may_approve(req, viewer)),
        "history": [{"kind": e.kind, "by": who(e.actor) if e.actor_id else "", "at": e.at}
                    for e in req.events.select_related("actor")],
    }


CHANNELS = ApprovalDelivery.CHANNELS
