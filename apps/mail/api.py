# apps/mail/api.py
"""Django Ninja router for /api/mail — domains, routes, agent mail, and the SNS doors.

The workspace is in the path, the `apps/inbound` idiom. Domain and route writes
are workspace settings (INTEGRATIONS: admin or owner), audited via route_gates.SETTINGS.
"""
from __future__ import annotations

import json
import logging
import uuid as uuidlib
from dataclasses import asdict

from django.conf import settings
from django.db import InterfaceError, OperationalError
from django.db.models import OuterRef, Subquery
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents import access
from apps.api.auth import session_auth
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import agent_mail, domains, inbound, reputation, ses, sns
from .models import MailDomain, MailMessage, MailRoute, MailThread
from .schemas import (
    MailDomainIn,
    MailDomainListOut,
    MailDomainOut,
    MailDomainPatchIn,
    MailMessageOut,
    MailReplyIn,
    MailResolveOut,
    MailRouteIn,
    MailRouteOut,
    MailSendIn,
    MailThreadListOut,
    MailThreadOut,
    MailThreadStateIn,
)

logger = logging.getLogger(__name__)

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


def _refuse_keeping(request, exc: domains.DomainError):
    # A refusal whose state change must commit (a stale row found verified is flipped):
    # answer the problem instead of raising, so the route's transaction is not rolled back.
    from apps.api.api import _on_http_error

    return _on_http_error(request, HttpError(exc.status, str(exc)))


@router.get("/domains/{workspace}", response=MailDomainListOut, summary="List mail domains")
def list_mail_domains(request: HttpRequest, workspace: str) -> dict:
    ws = _ws(request.user, workspace)
    items = [_domain_out(domains.refresh(d), request.user) for d in ws.mail_domains.all()]
    return {"enabled": settings.MAIL_ENABLED, "items": items,
            "can_manage": perms.can(request.user, ws, perms.INTEGRATIONS)}


@router.post("/domains/{workspace}", response=MailDomainOut, summary="Add a mail domain (admin or owner)")
def add_mail_domain(request: HttpRequest, workspace: str, payload: MailDomainIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    try:
        d = domains.add_domain(ws, payload.name, by=request.user)
    except domains.DomainError as exc:
        if exc.keep:
            return _refuse_keeping(request, exc)
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
    fields = []
    if payload.daily_send_cap is not None:
        if not 1 <= payload.daily_send_cap <= 5000:
            raise HttpError(422, "daily_send_cap must be between 1 and 5000")
        d.daily_send_cap = payload.daily_send_cap
        fields.append("daily_send_cap")
    if payload.unsuspend and d.status == MailDomain.SUSPENDED:
        d.status, d.suspended_reason = MailDomain.VERIFIED, ""
        fields += ["status", "suspended_reason"]
    # Only the fields this request changes: a concurrent auto-suspension must not be undone.
    if fields:
        d.save(update_fields=fields)
    d.refresh_from_db()
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
    if route is None:
        raise HttpError(409, "this address no longer exists")
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
            "from_name": m.from_name, "to": m.to, "cc": m.cc, "reply_to": m.reply_to or [], "subject": m.subject, "text": m.text,
            "html": m.html, "delivery_status": m.delivery_status, "verdicts": m.verdicts or {},
            "created_at": m.created_at.isoformat(),
            "attachments": atts}


def _thread_out(t, *, messages: bool) -> dict:
    agent = t.route.agent.slug if t.route_id and t.route.agent_id else ""
    out = {"thread_id": str(t.uuid), "address": t.address, "agent": agent, "subject": t.subject,
           "message_count": t.message_count, "unread": t.unread, "archived": t.archived,
           "last_message_at": t.last_message_at.isoformat() if t.last_message_at else ""}
    if messages:
        out["messages"] = [_msg_out(m) for m in t.messages.prefetch_related("attachments")]
    return out


def _counterpart(direction, from_addr, to) -> str:
    """The other party of a thread's newest message: its sender if inbound, else its first To."""
    if direction == MailMessage.IN:
        return from_addr or ""
    if direction == MailMessage.OUT:
        to = to if isinstance(to, list) else []
        return to[0] if to else ""
    return ""


def _agent_route(ws, slug: str) -> MailRoute | None:
    """The agent's own address here: never the catch-all, and the same one every time."""
    return (MailRoute.objects.filter(domain__workspace=ws, kind=MailRoute.AGENT, agent__slug=slug,
                                     enabled=True)
            .exclude(local_part=MailRoute.CATCH_ALL)
            .select_related("domain", "agent").order_by("domain__name", "local_part").first())


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
    address = ""
    if agent:
        qs = qs.filter(agent_slug=agent)  # snapshotted: survives the route's removal
        r = _agent_route(ws, agent)
        address = r.address if r else ""
    newest = MailMessage.objects.filter(thread=OuterRef("pk")).order_by("-created_at", "-pk")
    qs = qs.annotate(_dir=Subquery(newest.values("direction")[:1]),
                     _from=Subquery(newest.values("from_addr")[:1]),
                     _to=Subquery(newest.values("to")[:1]))
    items = []
    for t in qs[:200]:
        out = _thread_out(t, messages=False)
        out["counterpart"] = _counterpart(t._dir, t._from, t._to)
        items.append(out)
    return {"agent_address": address, "items": items}


@router.get("/threads/{workspace}/{uuid}", response=MailThreadOut, summary="Read a mail thread")
def get_mail_thread(request: HttpRequest, workspace: str, uuid: str) -> dict:
    t = _thread(_ws(request.user, workspace), uuid)
    out = _thread_out(t, messages=True)
    newest = t.messages.order_by("-created_at", "-pk").values("direction", "from_addr", "to").first()
    out["counterpart"] = _counterpart(newest["direction"], newest["from_addr"], newest["to"]) if newest else ""
    out["default_reply_to"] = _reply_recipients(t)
    return out


def _host(addr: str) -> str:
    return addr.rpartition("@")[2].lower().rstrip(".")


def _reply_recipients(t) -> list[str]:
    """Who a reply with no `to` goes to: the newest inbound message's Reply-To, but only
    when every Reply-To address is on the sender's own domain (or a subdomain of it) —
    else its sender. Reply-To is unauthenticated, so a foreign one could steer the agent's
    reply anywhere. Malformed entries and the mailbox's own address are dropped first, so
    Reply-To content can never fail the whole reply."""
    m = t.messages.filter(direction=MailMessage.IN).order_by("-created_at", "-pk").first()
    if m is None:
        return []
    sender = [m.from_addr] if m.from_addr else []
    own = (t.address or "").lower()
    candidates = []
    for a in m.reply_to or []:
        a = (a or "").strip().lower()
        if (a and a != own and len(a) <= 320 and a.isascii() and agent_mail._ADDR_RE.match(a)
                and a not in candidates):
            candidates.append(a)
    home = _host(m.from_addr or "")
    if not candidates or not home:
        return sender
    if all(_host(a) == home or _host(a).endswith("." + home) for a in candidates):
        return candidates[:agent_mail.MAX_RECIPIENTS]
    return sender


@router.post("/threads/{workspace}/{uuid}/reply", response=MailMessageOut, summary="Reply on a thread as its agent")
def reply_mail_thread(request: HttpRequest, workspace: str, uuid: str, payload: MailReplyIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    t = _thread(ws, uuid)
    if t.route is None or not t.route.enabled:
        raise HttpError(409, "this address no longer exists")
    if t.route.kind != MailRoute.AGENT:
        raise HttpError(409, "this address no longer routes to an agent")
    _agent_full(request.user, t.route)
    to = payload.to or _reply_recipients(t)
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
    route = _agent_route(ws, payload.agent)
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


def _notification_message(msg: dict) -> dict | None:
    # A verified Notification whose Message is not a JSON object is not mail or an event —
    # e.g. SES's "Successfully validated SNS topic for Amazon SES event publishing." —
    # so it is acknowledged and noted at INFO, never logged as a failure.
    try:
        body = json.loads(msg.get("Message") or "{}")
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        logger.info("ignored non-JSON notification on %s: %.80r", msg.get("TopicArn"), msg.get("Message"))
        return None
    return body


@router.post("/ses/inbound/", auth=None, response={200: dict, 404: dict, 500: dict},
             url_name="ses_inbound", summary="SES receipt notifications (SNS HTTPS delivery)")
def ses_inbound(request: HttpRequest):
    """SNS posts `text/plain`, so the body is read raw rather than through a schema.

    Verify before trusting a byte; 404 on any failure so a probe learns nothing.
    500 only for transient failures (SNS retries those for ~40 min); anything
    deterministic is acknowledged with 200 so a poisoned message cannot loop.
    """
    try:
        msg = json.loads(request.body or b"{}")
        sns.verify(msg, topic_arn=settings.MAIL_INBOUND_TOPIC_ARN)
    except (ValueError, RecursionError, sns.Invalid):
        return 404, {"detail": "not found"}
    except sns.Unavailable:
        return 500, {"detail": "retry"}  # the cert fetch failed transiently: real mail must not drop
    kind = msg.get("Type")
    if kind == "SubscriptionConfirmation":
        try:
            sns.confirm(msg)
        except sns.Invalid:
            return 404, {"detail": "not found"}
        except sns.Unavailable:
            return 500, {"detail": "retry"}
        return 200, {"confirmed": True}
    if kind != "Notification":
        return 200, {"ignored": kind}
    note = _notification_message(msg)
    if note is None:
        return 200, {"ignored": "non-JSON notification"}
    try:
        out = inbound.handle(note)
    except inbound.Transient as exc:
        logger.warning("mail inbound transient failure; SNS will retry: %s", exc.outcomes)
        return 500, {"detail": "retry"}
    except inbound.TRANSIENT:
        logger.exception("mail inbound transient failure; SNS will retry")
        return 500, {"detail": "retry"}
    except Exception as exc:  # noqa: BLE001 - deterministic: acknowledge, never retry
        logger.exception("mail inbound failed permanently; acknowledged")
        return 200, {"outcomes": [{"recipient": "*", "outcome": f"error:{type(exc).__name__}"}]}
    return 200, {"outcomes": out}


@router.post("/ses/events/", auth=None, response={200: dict, 404: dict, 500: dict},
             summary="SES sending events (SNS HTTPS delivery)")
def ses_events(request: HttpRequest):
    try:
        msg = json.loads(request.body or b"{}")
        sns.verify(msg, topic_arn=settings.MAIL_EVENTS_TOPIC_ARN)
    except (ValueError, RecursionError, sns.Invalid):
        return 404, {"detail": "not found"}
    except sns.Unavailable:
        return 500, {"detail": "retry"}
    if msg.get("Type") == "SubscriptionConfirmation":
        try:
            sns.confirm(msg)
        except sns.Invalid:
            return 404, {"detail": "not found"}
        except sns.Unavailable:
            return 500, {"detail": "retry"}
        return 200, {"confirmed": True}
    if msg.get("Type") != "Notification":
        return 200, {}
    event = _notification_message(msg)
    if event is None:
        return 200, {"ignored": "non-JSON notification"}
    try:
        reputation.record(event, sns_message_id=str(msg.get("MessageId") or "")[:128])
    except (OperationalError, InterfaceError):
        logger.exception("ses events: transient db error; SNS will retry")
        return 500, {"detail": "try again"}
    except Exception:  # noqa: BLE001 — a poisoned event must not be retried by SNS for hours
        logger.exception("ses events: dropping unprocessable event")
    return 200, {}
