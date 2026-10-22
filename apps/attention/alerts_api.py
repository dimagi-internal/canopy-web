"""`/api/me/alerts/` — the persisted things that need you (`apps/attention/alerts.py`).

A user-level view like `/api/me/attention/`: read once per organisation
(`wsvc.each_organisation`), each row labelled with its `organisation`. Who sees
an alert is the alert's workspace being one the caller may read AND its source
offering them an action; a row that fails either is a 404 by id, never a 403
that would say it exists.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, ProblemError
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc

from . import alerts
from .models import Alert, AlertEvent
from .schemas import AlertActIn, AlertActionOut, AlertEventOut, AlertOut

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["alerts"])

#: How long a dismissed alert stays on the `status=dismissed` list.
DISMISSED_WINDOW = dt.timedelta(days=7)


def _users(ids) -> dict:
    return {u.pk: u for u in get_user_model().objects.filter(pk__in=set(ids))}


def _name(user) -> str:
    return (user.get_full_name() or "").strip() or user.email or user.get_username()


def _last_notify(alert: Alert) -> str:
    ev = alert.events.filter(kind=AlertEvent.NOTIFIED).order_by("-at", "-pk").first()
    if ev is None:
        return ""
    to = [int(i) for i in (ev.payload.get("to") or [])]
    by = ev.payload.get("by")
    users = _users(to + ([by] if by else []))
    names = ", ".join(_name(users[i]) for i in to if i in users) or "someone"
    at = timezone.localtime(ev.at).strftime("%H:%M")
    if by and by in users:
        return f"{_name(users[by])} asked {names} at {at}"
    return f"Notified {names} at {at}"


def _offered(alert: Alert, user) -> list[alerts.Action] | None:
    """The viewer's actions, or None when the source cannot be asked."""
    try:
        return alerts.visible_actions(alert, user)
    except LookupError:
        return None


def _dismiss_offered(alert: Alert, user) -> bool:
    try:
        return alerts.offers_dismiss(alert, user)
    except LookupError:
        return False


def _out(alert: Alert, user, organisation: str, actions: list[alerts.Action]) -> AlertOut:
    return AlertOut(
        id=alert.pk, kind=alert.kind, severity=alert.severity, title=alert.title, body=alert.body,
        url=alert.url, workspace=alert.workspace_id, organisation=organisation, scopes=list(alert.scopes),
        status=alert.status, first_seen_at=alert.first_seen_at, last_seen_at=alert.last_seen_at,
        occurrences=alert.occurrences, closed_by_email=alert.closed_by.email if alert.closed_by else "",
        closed_at=alert.closed_at,
        actions=[AlertActionOut(id=a.id, label=a.label, closes=a.closes, href=a.href) for a in actions],
        last_notify=_last_notify(alert),
    )


def _visible(request: HttpRequest, alert_id: int) -> Alert:
    """The alert, if the caller may see it (404 otherwise): its workspace is one
    they are in, and — open — they are offered an action, or — dismissed — they
    are offered dismiss."""
    user = request.user
    alert = (Alert.objects.select_related("closed_by")
             .filter(pk=alert_id, workspace_id__in=wsvc.request_member_slugs(request)).first())
    if alert is not None:
        if alert.status == Alert.OPEN:
            ok = bool(_offered(alert, user))
        elif alert.status == Alert.DISMISSED:
            ok = _dismiss_offered(alert, user)
        else:
            ok = False
        if ok:
            return alert
    raise ProblemError(404, "Alert not found", type_=TYPE_NOT_FOUND)


def _history_visible(request: HttpRequest, alert_id: int) -> Alert:
    """History is the audit trail (it must outlive the alert): visible when the
    workspace is the caller's and the source offers them any action, whatever
    the alert's status."""
    alert = (Alert.objects.select_related("closed_by")
             .filter(pk=alert_id, workspace_id__in=wsvc.request_member_slugs(request)).first())
    if alert is not None:
        try:
            if alerts.source_for(alert.kind).actions(alert, request.user):
                return alert
        except LookupError:
            pass
    raise ProblemError(404, "Alert not found", type_=TYPE_NOT_FOUND)


def _organisation(alert: Alert) -> str:
    return wsvc.tree_roots({alert.workspace_id}).get(alert.workspace_id, alert.workspace_id)


@router.get("/", response=list[AlertOut], summary="Alerts that need you")
# the caller's own list; each alert's source decides what this viewer is offered, and no action means no row
@gate("self")
def list_alerts(request: HttpRequest, scope: str | None = None,
                status: Literal["open", "dismissed"] = "open") -> list[AlertOut]:
    """Open alerts you are offered an action on, in the workspaces you are in,
    most severe first (`blocking`, `action`, `setup`) then newest. `scope` keeps
    those whose `scopes` include it. `status=dismissed` lists what was dismissed
    in the last 7 days that you could dismiss (so you can `undismiss` it)."""
    user = request.user

    def read() -> list[AlertOut]:
        slugs = wsvc.request_workspace_slugs(request)
        qs = Alert.objects.filter(workspace_id__in=slugs, status=status).select_related("closed_by")
        if status == Alert.DISMISSED:
            qs = qs.filter(closed_at__gte=timezone.now() - DISMISSED_WINDOW)
        roots = wsvc.tree_roots({a.workspace_id for a in qs}) if slugs else {}
        out = []
        for a in qs:
            if scope is not None and scope not in a.scopes:
                continue
            if status == Alert.OPEN:
                actions = _offered(a, user)
                if not actions:
                    continue
            else:
                if not _dismiss_offered(a, user):
                    continue
                actions = []
            out.append((a, _out(a, user, roots.get(a.workspace_id, a.workspace_id), actions)))
        return out

    rows = {a.pk: o for a, o in wsvc.each_organisation(request, read)}  # a row in two reads is one row
    ranked = sorted(rows.values(), key=lambda o: (Alert.SEVERITY_RANK[o.severity], -o.last_seen_at.timestamp()))
    return ranked


@router.post("/{id}/act", response=AlertOut, summary="Do one of an alert's actions")
@gate("self", kind="action")  # runs an action the alert's source offers THIS caller; re-checked on every call
def act_on_alert(request: HttpRequest, id: int, payload: AlertActIn) -> AlertOut:
    """Runs `action` if it is still offered to you (a link action is followed in
    the browser, never run here). 404 if you cannot see the alert; 409
    `action-not-offered` if the action is not (or no longer) yours to run."""
    alert = _visible(request, id)
    try:
        alerts.act(alert, request.user, payload.action)
    except alerts.ActionNotOffered as e:
        raise _not_offered(str(e)) from e
    return _after(request, alert)


@router.post("/{id}/undismiss", response=AlertOut, summary="Put a dismissed alert back")
@gate("self", kind="action")  # anyone currently offered dismiss may undo it; nothing is sent
def undismiss_alert(request: HttpRequest, id: int) -> AlertOut:
    """Re-opens a dismissed alert. 404 if you cannot see it; 409
    `action-not-offered` unless you are offered `dismiss` on it."""
    alert = _visible(request, id)
    if alert.status != Alert.DISMISSED or not _dismiss_offered(alert, request.user):
        raise _not_offered(f"alert {alert.pk} is not dismissed, or dismiss is not offered to you")
    alerts.undismiss(alert, request.user)
    return _after(request, alert)


@router.get("/{id}/history", response=list[AlertEventOut], summary="An alert's audit trail")
# same visibility as the alert itself: a stranger gets a 404
@gate("self")
def alert_history(request: HttpRequest, id: int) -> list[AlertEventOut]:
    """Every change to the alert, oldest first (also once resolved): raised, changed, acted,
    notified, dismissed, undismissed, resolved, reopened."""
    alert = _history_visible(request, id)
    return [AlertEventOut(at=e.at, kind=e.kind, actor_email=e.actor.email if e.actor else "",
                          action_id=e.action_id, payload=e.payload)
            for e in alert.events.select_related("actor").order_by("at", "pk")]


def _not_offered(detail: str) -> ProblemError:
    return ProblemError(409, "Action not offered", type_=TYPE_CONFLICT, detail=detail,
                        members={"code": "action-not-offered"})


def _after(request: HttpRequest, alert: Alert) -> AlertOut:
    """The alert as it now stands, with what the caller may still do on it."""
    alert = Alert.objects.select_related("closed_by").get(pk=alert.pk)
    return _out(alert, request.user, _organisation(alert), _offered(alert, request.user) or [])
