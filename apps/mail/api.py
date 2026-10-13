# apps/mail/api.py
"""Django Ninja router for /api/mail — domains, routes, agent mail, and the SNS doors.

The workspace is in the path, the `apps/inbound` idiom. Domain and route writes
are workspace settings (INTEGRATIONS: admin or owner), audited via route_gates.SETTINGS.
"""
from __future__ import annotations

from dataclasses import asdict

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import dns, domains
from .models import MailDomain, MailRoute
from .schemas import (MailDomainIn, MailDomainListOut, MailDomainOut, MailDomainPatchIn,
                      MailRouteIn, MailRouteOut)

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
            "records": [asdict(r) for r in dns.check(d)],
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
