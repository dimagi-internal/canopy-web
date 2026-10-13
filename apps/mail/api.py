# apps/mail/api.py
"""Django Ninja router for /api/mail — domains, routes, agent mail, and the SNS doors.

The workspace is in the path, the `apps/inbound` idiom. Domain and route writes
are workspace settings (INTEGRATIONS: admin or owner), audited via route_gates.SETTINGS.
"""
from __future__ import annotations

import uuid as uuidlib
from dataclasses import asdict

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents import access
from apps.api.auth import session_auth
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import agent_mail, domains, ses
from .models import MailDomain, MailMessage, MailRoute, MailThread
from .schemas import (MailDomainIn, MailDomainListOut, MailDomainOut, MailDomainPatchIn,
                      MailMessageOut, MailReplyIn, MailResolveOut, MailRouteIn, MailRouteOut,
                      MailSendIn, MailThreadListOut, MailThreadOut, MailThreadStateIn)

router = Router(auth=session_auth, tags=["mail"])


def _ws(user, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    return ws


def _admin_ws(user, slug: str) -> Workspace:
    ws = _ws(user, slug)
    if not perms.can(user, ws, perms.INTEGRATIONS):
        raise HttpError(403, "requires the admin role or above")
    return ws


def _domain(ws, domain_id: int) -> MailDomain:
    d = MailDomain.objects.filter(pk=domain_id, workspace=ws).first()
    if d is None:
        raise HttpError(404, "no such domain")
    return d


def _route_out(r: MailRoute) -> dict:
    return {"id": r.pk, "local_part": r.local_part, "address": r.address, "kind": r.kind,
            "agent_slug": r.agent.slug if r.agent_id else "",
            "forward_to_user_id": r.forward_to_id,
            "forward_to_email": r.forward_to.email if r.forward_to_id else "",
            "enabled": r.enabled}


def _domain_out(d: MailDomain, user) -> dict:
    return {"id": d.pk, "name": d.name, "status": d.status, "receiving": d.receiving,
            "mx_provider": d.mx_provider, "suspended_reason": d.suspended_reason,
            "daily_send_cap": d.daily_send_cap,
            "records": [asdict(r) for r in domains.records(d)],
            "routes": [_route_out(r) for r in d.routes.select_related("agent", "forward_to")],
            "can_manage": perms.can(user, d.workspace, perms.INTEGRATIONS)}


def _raise(exc: domains.DomainError):
    raise HttpError(exc.status, str(exc))


@router.get("/domains/{workspace}", response=MailDomainListOut, summary="List mail domains")
def list_mail_domains(request: HttpRequest, workspace: str) -> dict:
    ws = _ws(request.user, workspace)
    items = [_domain_out(domains.refresh(d), request.user) for d in ws.mail_domains.all()]
    return {"enabled": settings.MAIL_ENABLED, "items": items}


@router.post("/domains/{workspace}", response=MailDomainOut, summary="Add a mail domain (admin or owner)")
def add_mail_domain(request: HttpRequest, workspace: str, payload: MailDomainIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    try:
        d = domains.add_domain(ws, payload.name, by=request.user)
    except domains.DomainError as exc:
        _raise(exc)
    audit.note_change(scope=f"mail.domain:{d.name}", before=None, after={"name": d.name})
    return _domain_out(d, request.user)


@router.post("/domains/{workspace}/{domain_id}/refresh", response=MailDomainOut,
             summary="Re-check a domain's DNS and verification now")
def refresh_mail_domain(request: HttpRequest, workspace: str, domain_id: int) -> dict:
    ws = _ws(request.user, workspace)
    return _domain_out(domains.refresh(_domain(ws, domain_id), force=True), request.user)


@router.patch("/domains/{workspace}/{domain_id}", response=MailDomainOut,
              summary="Change a domain's send cap or lift its suspension (admin or owner)")
def update_mail_domain(request: HttpRequest, workspace: str, domain_id: int,
                       payload: MailDomainPatchIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    before = {"daily_send_cap": d.daily_send_cap, "status": d.status}
    if payload.daily_send_cap is not None:
        if not 1 <= payload.daily_send_cap <= 5000:
            raise HttpError(422, "daily_send_cap must be between 1 and 5000")
        d.daily_send_cap = payload.daily_send_cap
    if payload.unsuspend and d.status == MailDomain.SUSPENDED:
        d.status, d.suspended_reason = MailDomain.VERIFIED, ""
    d.save()
    audit.note_change(scope=f"mail.domain:{d.name}", before=before,
                      after={"daily_send_cap": d.daily_send_cap, "status": d.status})
    return _domain_out(d, request.user)


@router.delete("/domains/{workspace}/{domain_id}", response={204: None},
               summary="Remove a mail domain (admin or owner)")
def remove_mail_domain(request: HttpRequest, workspace: str, domain_id: int):
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    audit.note_change(scope=f"mail.domain:{d.name}", before={"name": d.name}, after=None)
    domains.remove_domain(d)
    return 204, None


@router.put("/domains/{workspace}/{domain_id}/routes", response=MailRouteOut,
            summary="Create or replace the route for one address (admin or owner)")
def save_mail_route(request: HttpRequest, workspace: str, domain_id: int, payload: MailRouteIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    prior = d.routes.filter(local_part=payload.local_part.strip().lower()).first()
    try:
        r = domains.save_route(d, local_part=payload.local_part, kind=payload.kind,
                               agent_slug=payload.agent_slug,
                               forward_to_user_id=payload.forward_to_user_id, enabled=payload.enabled)
    except domains.DomainError as exc:
        _raise(exc)
    audit.note_change(scope=f"mail.route:{r.address}",
                      before=_route_out(prior) if prior else None, after=_route_out(r))
    return _route_out(r)


@router.delete("/domains/{workspace}/{domain_id}/routes/{route_id}", response={204: None},
               summary="Remove an address's route (admin or owner)")
def delete_mail_route(request: HttpRequest, workspace: str, domain_id: int, route_id: int):
    ws = _admin_ws(request.user, workspace)
    r = _domain(ws, domain_id).routes.filter(pk=route_id).first()
    if r is None:
        raise HttpError(404, "no such route")
    audit.note_change(scope=f"mail.route:{r.address}", before=_route_out(r), after=None)
    r.delete()
    return 204, None


def _thread(ws, uuid: str) -> MailThread:
    try:
        key = uuidlib.UUID(uuid)
    except ValueError:
        raise HttpError(404, "no such thread") from None
    t = MailThread.objects.filter(workspace=ws, uuid=key).select_related("route__agent", "route__domain").first()
    if t is None:
        raise HttpError(404, "no such thread")
    return t


def _need(user, ws, cap):
    if not perms.can(user, ws, cap):
        raise HttpError(403, "not allowed")


def _agent_full(user, route) -> None:
    """THE rule (`access.decide`) decides what a person may make this agent do; mail needs FULL."""
    if not route.agent_id:
        raise HttpError(409, "this address is not an agent mailbox")
    d = access.decide(route.agent, user, verified=True, origin="email")
    if d.access != access.FULL:
        raise HttpError(403, d.reason or f"you may not act as {route.agent.slug}")


def _presign(a) -> str:
    try:
        return ses.presign(a.s3_key, a.filename)
    except ses.MailOff:
        return ""


def _msg_out(m) -> dict:
    atts = [{"filename": a.filename, "content_type": a.content_type, "size": a.size,
             "url": _presign(a)} for a in m.attachments.all()]
    return {"id": m.pk, "direction": m.direction, "message_id": m.message_id, "from_addr": m.from_addr,
            "from_name": m.from_name, "to": m.to, "cc": m.cc, "subject": m.subject, "text": m.text,
            "html": m.html, "delivery_status": m.delivery_status, "created_at": m.created_at.isoformat(),
            "attachments": atts}


def _thread_out(t, *, messages: bool) -> dict:
    out = {"thread_id": str(t.uuid), "address": t.route.address,
           "agent": t.route.agent.slug if t.route.agent_id else "", "subject": t.subject,
           "message_count": t.message_count, "unread": t.unread, "archived": t.archived,
           "last_message_at": t.last_message_at.isoformat() if t.last_message_at else ""}
    if messages:
        out["messages"] = [_msg_out(m) for m in t.messages.prefetch_related("attachments")]
    return out


@router.get("/resolve", response=MailResolveOut, summary="Which mail backend serves an address")
def resolve_mail_address(request: HttpRequest, address: str) -> dict:
    domain, route = domains.resolve(address)
    if (route is None or route.kind != MailRoute.AGENT
            or domain.workspace_id not in wsvc.user_workspace_slugs(request.user)):
        raise HttpError(404, "not an SES mailbox")
    return {"backend": "ses", "workspace": domain.workspace_id, "agent": route.agent.slug}


@router.get("/threads/{workspace}", response=MailThreadListOut, summary="List mail threads")
def list_mail_threads(request: HttpRequest, workspace: str, agent: str = "", archived: bool = False) -> dict:
    ws = _ws(request.user, workspace)
    qs = MailThread.objects.filter(workspace=ws, archived=archived).select_related("route__agent", "route__domain")
    if agent:
        qs = qs.filter(route__agent__slug=agent)
    return {"items": [_thread_out(t, messages=False) for t in qs[:200]]}


@router.get("/threads/{workspace}/{uuid}", response=MailThreadOut, summary="Read a mail thread")
def get_mail_thread(request: HttpRequest, workspace: str, uuid: str) -> dict:
    return _thread_out(_thread(_ws(request.user, workspace), uuid), messages=True)


@router.post("/threads/{workspace}/{uuid}/reply", response=MailMessageOut, summary="Reply on a thread as its agent")
def reply_mail_thread(request: HttpRequest, workspace: str, uuid: str, payload: MailReplyIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    t = _thread(ws, uuid)
    if not t.route.enabled or t.route.kind != MailRoute.AGENT:
        raise HttpError(409, "this address no longer routes to an agent")
    _agent_full(request.user, t.route)
    to = payload.to or [m.from_addr for m in
                        t.messages.filter(direction=MailMessage.IN).order_by("-created_at", "-pk")[:1]
                        if m.from_addr]
    if not to:
        raise HttpError(422, "no one to reply to")
    try:
        m = agent_mail.send(t.route, to=to, cc=payload.cc, text=payload.text, html=payload.html, thread=t)
    except agent_mail.SendRefused as exc:
        raise HttpError(exc.status, str(exc)) from None
    return _msg_out(m)


@router.post("/send/{workspace}", response=MailMessageOut, summary="Start a new thread as an agent")
def send_mail(request: HttpRequest, workspace: str, payload: MailSendIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    route = MailRoute.objects.filter(domain__workspace=ws, kind=MailRoute.AGENT, agent__slug=payload.agent,
                                     enabled=True).select_related("domain", "agent").first()
    if route is None:
        raise HttpError(404, f"agent {payload.agent!r} has no mail address here")
    _agent_full(request.user, route)
    try:
        m = agent_mail.send(route, to=payload.to, cc=payload.cc, subject=payload.subject,
                            text=payload.text, html=payload.html)
    except agent_mail.SendRefused as exc:
        raise HttpError(exc.status, str(exc)) from None
    return _msg_out(m)


@router.post("/threads/{workspace}/{uuid}/state", response=MailThreadOut, summary="Mark a thread read/archived")
def set_mail_thread_state(request: HttpRequest, workspace: str, uuid: str, payload: MailThreadStateIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    t = _thread(ws, uuid)
    _agent_full(request.user, t.route)
    if payload.unread is not None:
        t.unread = payload.unread
    if payload.archived is not None:
        t.archived = payload.archived
    t.save(update_fields=["unread", "archived"])
    return _thread_out(t, messages=False)
