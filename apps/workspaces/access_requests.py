"""Access requests: someone whose login domain is on a workspace's list ASKS to
be invited; an admin or owner (or the workspace's auto-approve role) decides.
Approval is the only thing here that creates a membership (`_grant`)."""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from .links import access_request_link, access_request_path, base_url
from .models import Workspace, WorkspaceAccessRequest, WorkspaceMembership
from .notifications import _button, _send, _who
from .services import _email_domain, _grant, effective_memberships, is_member, user_workspace_slugs

# The replacement for self-join. A domain match lets someone ASK; a person
# (an admin or owner of the workspace) decides. Every request tells every admin
# and owner, inherited owners included. Approval is the only thing here that
# creates a membership.


class AccessRequestError(Exception):
    """`.code` is a closed set an HTTP layer maps to a status: `not_found`
    (no such workspace, OR the caller's domain is not on its list — the same
    answer on purpose, so this cannot probe tenants), `already_member`,
    `not_pending` (already decided), `invalid_role`."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


#: Roles an approval may grant. Owner is not among them: ownership is handed
#: over on the Members list by an owner, never as the answer to a request.
APPROVABLE_ROLES = (WorkspaceMembership.VIEWER, WorkspaceMembership.EDITOR, WorkspaceMembership.ADMIN)

#: Roles `Workspace.auto_approve_role` may name. An automatic grant never makes
#: an admin or an owner; anything else in the column reads as "off".
AUTO_APPROVE_ROLES = (WorkspaceMembership.VIEWER, WorkspaceMembership.EDITOR)



def _domain_matches(ws: Workspace, user) -> str:
    """The caller's login-email domain if it is on `ws`'s request list, else ""."""
    domain = _email_domain(getattr(user, "email", ""))
    if domain and domain in [d.lower() for d in (ws.access_request_domains or [])]:
        return domain
    return ""


def requestable_workspaces(user) -> list[tuple[Workspace, str, WorkspaceAccessRequest | None]]:
    """Workspaces `user` may request an invitation to: their login email's
    domain is in `access_request_domains` and they are not a member. Each comes
    with the caller's own open (pending) request, if they have one. A
    capability list, not a directory — it never names a workspace the caller
    cannot ask to join."""
    domain = _email_domain(getattr(user, "email", ""))
    if not domain:
        return []
    already = user_workspace_slugs(user)
    out: list[tuple[Workspace, str, WorkspaceAccessRequest | None]] = []
    for ws in Workspace.objects.exclude(access_request_domains=[]).order_by("display_name"):
        if ws.slug in already or not _domain_matches(ws, user):
            continue
        pending = WorkspaceAccessRequest.objects.filter(
            workspace=ws, user=user, status=WorkspaceAccessRequest.PENDING,
        ).first()
        out.append((ws, domain, pending))
    return out


def request_access(user, slug: str, note: str = "") -> tuple[WorkspaceAccessRequest, bool]:
    """Ask to be invited into `slug`. Returns `(request, created)`.

    Idempotent while open: a second call with a request still pending returns
    that request and notifies nobody again. With the workspace's
    `auto_approve_role` set, the new request is approved at once at that role
    (`auto=True`, `decided_by=None`; capped at editor — see
    `AUTO_APPROVE_ROLES`). It is the same request record either way, and every
    admin and owner is told (`notify_admins`) with the link to its page, where
    they can change the role or remove the person. Best-effort.
    """
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or not _domain_matches(ws, user):
        raise AccessRequestError("not_found")
    if is_member(user, ws.slug):
        raise AccessRequestError("already_member")
    note = (note or "").strip()
    existing = WorkspaceAccessRequest.objects.filter(
        workspace=ws, user=user, status=WorkspaceAccessRequest.PENDING,
    ).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            req = WorkspaceAccessRequest.objects.create(workspace=ws, user=user, note=note)
    except IntegrityError:
        # A concurrent call opened it first; that one is the request.
        return WorkspaceAccessRequest.objects.get(
            workspace=ws, user=user, status=WorkspaceAccessRequest.PENDING,
        ), False
    if ws.auto_approve_role in AUTO_APPROVE_ROLES:
        req = _approve(req, by=None, role=ws.auto_approve_role, auto=True)
    notify_admins(req)
    if req.status == WorkspaceAccessRequest.APPROVED:
        notify_requester(req)
    return req, True

_access_log = logging.getLogger("apps.workspaces.access_requests")


def _approve(req: WorkspaceAccessRequest, *, by, role: str, auto: bool = False) -> WorkspaceAccessRequest:
    if role not in APPROVABLE_ROLES:
        raise AccessRequestError("invalid_role")
    with transaction.atomic():
        locked = WorkspaceAccessRequest.objects.select_for_update().select_related(
            "workspace", "user").get(pk=req.pk)
        if locked.status != WorkspaceAccessRequest.PENDING:
            raise AccessRequestError("not_pending")
        _grant(locked.workspace, locked.user, role, invited_by=by)
        locked.status = WorkspaceAccessRequest.APPROVED
        locked.role = role
        locked.auto = auto
        locked.decided_by = by
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "role", "auto", "decided_by", "decided_at"])
    return locked


def approve_access_request(*, request: WorkspaceAccessRequest, by, role: str) -> WorkspaceAccessRequest:
    """Approve at `role` (viewer / editor / admin) and create the membership.
    The HTTP layer has already checked `by` may grant `role`
    (`permissions.may_manage_member`). Tells the requester, best-effort."""
    req = _approve(request, by=by, role=role)
    _record_event(req, "access_request.approved", f"{_who(req.user)} approved as {role} by {_who(by)}")
    notify_requester(req)
    return req


def deny_access_request(*, request: WorkspaceAccessRequest, by, reason: str = "") -> WorkspaceAccessRequest:
    """Deny. Creates nothing; the person may ask again later. Tells them,
    with `reason` if one was given, best-effort."""
    with transaction.atomic():
        req = WorkspaceAccessRequest.objects.select_for_update().select_related(
            "workspace", "user").get(pk=request.pk)
        if req.status != WorkspaceAccessRequest.PENDING:
            raise AccessRequestError("not_pending")
        req.status = WorkspaceAccessRequest.DENIED
        req.decided_by = by
        req.decided_at = timezone.now()
        req.decision_reason = (reason or "").strip()
        req.save(update_fields=["status", "decided_by", "decided_at", "decision_reason"])
    _record_event(req, "access_request.denied", f"{_who(req.user)} denied by {_who(by)}")
    notify_requester(req)
    return req


def access_request_recipients(ws: Workspace) -> list:
    """Every admin and owner of `ws`, inherited owners (owners of an ancestor)
    included — everyone who could act on a request."""
    from . import permissions as perms

    return [m.user for m in effective_memberships(ws) if perms.role_allows(m.role, perms.MEMBERS_MANAGE)]


def _record_event(req: WorkspaceAccessRequest, kind: str, summary: str, level: str = "info",
                  payload: dict | None = None) -> None:
    """Best-effort line in the workspace event log (admins read it)."""
    try:
        from apps.events.services import record

        record([{
            "source": "workspaces.access", "kind": kind, "level": level,
            "summary": summary[:500],
            "payload": {"request_id": req.pk, "user_id": req.user_id, **(payload or {})},
        }], workspace=req.workspace)
    except Exception:  # noqa: BLE001 — a log write must never fail the request
        _access_log.exception("could not record %s for access request %s", kind, req.pk)


def notify_admins(req: WorkspaceAccessRequest) -> dict:
    """Tell every admin and owner (inherited owners included) that someone
    asked: by email (the invite mechanism — labs SES) and Web Push, each
    carrying the deep link to the request's approve page.

    BEST-EFFORT: never raises, so a mail outage cannot fail the request. What
    happened is stored on the request (`notify_result`, shown on the access
    requests list) and written to the workspace event log — at `warn` when any
    admin could not be emailed."""
    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        ws = req.workspace
        who = _who(req.user)
        link = access_request_link(req)
        if req.status == WorkspaceAccessRequest.APPROVED:
            outcome = f"auto-approved as {req.role}"
            subject = f"{who} requested access to {ws.display_name} — auto-approved as {req.role}"
            action = f"Change their role or remove them: {link}"
            label = "Change role or remove"
        else:
            outcome = "approve / deny"
            subject = f"{who} requested access to {ws.display_name}"
            action = f"Approve or deny: {link}"
            label = "Review the request"
        line = f"{who} requested an invitation to {ws.display_name} — {outcome}: {link}"
        note = f'\n\nTheir note: "{req.note}"' if req.note else ""
        text = (f"{line}{note}\n\n{action}\n\nYou are getting this because you are an admin or "
                f"owner of {ws.display_name} on Canopy.\n")
        html = (f"<p>{escape(who)} requested an invitation to <strong>{escape(ws.display_name)}"
                f"</strong> — {escape(outcome)}.</p>"
                + (f"<blockquote>{escape(req.note)}</blockquote>" if req.note else "")
                + _button(link, label)
                + f'<p style="color:#78716c;font-size:13px">{escape(link)}<br>You are getting this '
                  f"because you are an admin or owner of {escape(ws.display_name)} on Canopy.</p>")
        recipients = access_request_recipients(ws)
        for admin in recipients:
            if not admin.email:
                continue
            status = _send(subject, text, html, admin.email, reply_to=req.user.email or None)
            if status == "sent":
                result["emailed"].append(admin.email)
            elif status == "not_configured":
                result["not_configured"] = True
            else:
                result["failed"].append(admin.email)
            try:
                from apps.push.services import send_to_user

                result["pushed"] += send_to_user(admin, f"Access request · {ws.display_name}",
                                                 f"{who} — {outcome}", access_request_path(req))
            except Exception:  # noqa: BLE001
                _access_log.exception("access-request push failed: admin=%s", admin.pk)
        result["recipients"] = len(recipients)
    except Exception:  # noqa: BLE001
        _access_log.exception("notifying admins of access request %s failed", req.pk)
        result["error"] = True
    try:
        req.notify_result = result
        req.save(update_fields=["notify_result"])
    except Exception:  # noqa: BLE001
        _access_log.exception("could not store notify_result on access request %s", req.pk)
    failed = bool(result["failed"] or result.get("error")) or not result.get("recipients")
    _record_event(
        req, "access_request.created",
        f"{_who(req.user)} requested an invitation ({req.status}); emailed "
        f"{len(result['emailed'])} admin(s)"
        + (f", FAILED for {', '.join(result['failed'])}" if result["failed"] else "")
        + ("; email delivery is off" if result["not_configured"] else ""),
        level="warn" if failed else "info",
        payload={"notify": result, "status": req.status},
    )
    return result


def notify_requester(req: WorkspaceAccessRequest) -> str:
    """Tell the person who asked how it went — approved (with a link into the
    workspace) or denied (with the reason, if any). Best-effort; never raises."""
    try:
        user = req.user
        if not user.email:
            return "not_configured"
        ws = req.workspace
        if req.status == WorkspaceAccessRequest.APPROVED:
            link = f"{base_url()}/w/{ws.slug}"
            subject = f"You're in: {ws.display_name} on Canopy"
            text = (f"Your request to join {ws.display_name} on Canopy was approved. You are a "
                    f"{req.role}.\n\nOpen the workspace:\n{link}\n")
            html = (f"<p>Your request to join <strong>{escape(ws.display_name)}</strong> on Canopy "
                    f"was approved. You are a {escape(req.role)}.</p>" + _button(link, "Open the workspace")
                    + f'<p style="color:#78716c;font-size:13px">{escape(link)}</p>')
        elif req.status == WorkspaceAccessRequest.DENIED:
            reason = req.decision_reason
            subject = f"Your request to join {ws.display_name} on Canopy"
            text = (f"Your request to join {ws.display_name} on Canopy was not approved."
                    + (f'\n\nReason: "{reason}"' if reason else "")
                    + "\n\nIf you think this is a mistake, reply to this email.\n")
            html = (f"<p>Your request to join <strong>{escape(ws.display_name)}</strong> on Canopy "
                    f"was not approved.</p>"
                    + (f"<blockquote>{escape(reason)}</blockquote>" if reason else ""))
        else:
            return "not_configured"
        reply_to = req.decided_by.email if req.decided_by is not None and req.decided_by.email else None
        status = _send(subject, text, html, user.email, reply_to=reply_to)
        if status == "failed":
            _record_event(req, "access_request.requester_notify_failed",
                          f"could not email {user.email} the decision", level="warn")
        return status
    except Exception:  # noqa: BLE001
        _access_log.exception("notifying requester of access request %s failed", req.pk)
        return "failed"
