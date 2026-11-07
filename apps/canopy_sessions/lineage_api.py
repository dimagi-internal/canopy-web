"""A child session reports back to the conversation it came from (lineage.py).

Mounted at `/api/canopy-sessions/lineage/...` (`apps/api/api.py`), before the
`/{session_id}` routes.
"""
from __future__ import annotations

import time
import uuid

from django.db import transaction
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents import refusals
from apps.api.auth import session_auth
from apps.api.errors import refusals as declared_refusals
from apps.api.route_gates import gate
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces import links

from . import access, lineage, send_paths
from . import api as session_api
from .models import Session
from .schemas import ReportBackIn, ReportBackOut

router = Router(auth=session_auth, tags=["chat"])

REPORT_VIA = "report_back"
_REPORTS_KEPT = 20


@router.post("/lineage/report-back", response={200: ReportBackOut, **declared_refusals(422)},
             summary="Report a session's outcome back to the conversation it came from")
@gate("session-acl", "agent-login", kind="conversation")  # write; or the agent's own live turn
def report_back_to_parent(request: HttpRequest, payload: ReportBackIn):
    """Send this session's outcome to the conversation it was started from — work
    taken offline from a Slack thread (`take_session_offline`), a branch, a
    dispatch. The report becomes a turn in that conversation, asked by the person
    whose work this is, and its agent posts it there (in its Slack thread, its web
    chat), so that conversation knows the outcome from then on.

    Use it when the person you are working with says to report back. Name your
    session with your `turn_id`; the report goes in that turn's person's name, and
    they must be able to write in the parent conversation. A person names the
    session with `session_id` and reports themselves. `final: false` sends an
    interim update; the work carries on.

    409 for a session that was not started from another conversation, or whose
    parent is closed; 403 for someone who may not write in the parent.
    Naming no session (`turn_id` or `session_id`) is a 422.
    """
    user = request.user
    own = access.own_turn_session(user, payload.turn_id) if payload.turn_id else None
    if own is not None:
        turn = Turn.objects.select_related("initiator_user").get(pk=payload.turn_id)
        asker = turn.initiator_user
        if asker is None:
            raise HttpError(409, "reporting back is a person's call, and this turn has no person behind it")
        child = own
        initiator = who.for_user(asker, via=REPORT_VIA,
                                 assurance=turn.initiator_assurance or who.INTERNAL)
    else:
        child = _session_by(request, turn_id=payload.turn_id, session_id=payload.session_id)
        asker, initiator = user, who.for_request(request, via=REPORT_VIA)
    try:
        report = report_back(child, asker=asker, summary=payload.summary, final=payload.final,
                                     initiator=initiator)
    except ReportRefusedError as e:
        raise HttpError(e.status, str(e)) from None
    return {"status": "sent", "message": "Reported back: the conversation this came from has the outcome.",
            "session_id": child.pk, "parent_session_id": child.parent_session_id,
            "turn_id": report.pk if report is not None else None}


def _session_by(request: HttpRequest, *, turn_id: str, session_id: str):
    """A session the caller can open and write in, named by one of its turns or its id."""
    if session_id:
        target = session_id
    elif turn_id:
        try:
            target = Turn.objects.filter(pk=uuid.UUID(str(turn_id))).values_list(
                "chat_session_id", flat=True).first()
        except ValueError:
            target = None
    else:
        raise HttpError(422, "name the session: turn_id or session_id")
    try:
        session = session_api.readable_sessions(request).filter(pk=target).first() if target else None
    except Exception:  # noqa: BLE001 — a malformed id is just "no such session"
        session = None
    if session is None:
        raise HttpError(404, "no session you can open matches that")
    if not access.can_write(request.user, session):
        raise HttpError(403, refusals.cannot_write_message(session))
    return session


# ---- the report --------------------------------------------------------------

class ReportRefusedError(Exception):
    """A report that cannot go: `status` is the HTTP status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def report_prompt(child: Session, asker, summary: str, *, final: bool) -> str:
    name = (getattr(asker, "get_full_name", lambda: "")() or getattr(asker, "email", "") or "Someone").strip()
    url = links.scoped_url_or_none(child.workspace_id, f"/sessions/{child.pk}") or ""
    where = f" ({url})" if url else ""
    state = ("The work there is finished." if final
             else "This is an interim update; the work carries on separately.")
    return (
        f"REPORT BACK. {name} took part of this conversation into a separate session{where} "
        f"and is reporting its outcome here. {state} Reply in this conversation with the report "
        "below, as its outcome: you may tighten the wording, but keep every result, decision, link "
        "and open question, and do not pick the work up here unless someone asks you to.\n\n"
        f"<report from_session=\"{child.pk}\">\n{summary.strip()}\n</report>"
    )


def report_back(child: Session, *, asker, summary: str, final: bool = True, initiator=None):
    """Send `child`'s outcome to its parent as a turn there, asked by `asker` — the
    person whose work it is, who must be able to write in the parent. Returns the
    parent's new turn. `initiator` is the asker as this request knows them
    (default: a user report, `REPORT_VIA`)."""
    if not (summary or "").strip():
        raise ReportRefusedError(422, "a summary is required: it is what the parent conversation gets")
    parent = child.parent_session if child.parent_session_id else None
    if parent is None:
        raise ReportRefusedError(409, "this session was not started from another conversation, so there "
                                 "is nothing to report back to")
    if parent.status == Session.ARCHIVED:
        raise ReportRefusedError(409, "the conversation this came from is closed")
    if asker is None or not access.can_write(asker, parent):
        raise ReportRefusedError(403, "only someone who can write in the conversation this came from can "
                                 "report back to it")
    initiator = initiator or who.for_user(asker, via=REPORT_VIA, assurance=who.INTERNAL)
    try:
        _message, turn = send_paths.send_from(
            "rest", session=parent, text=report_prompt(child, asker, summary, final=final),
            user=asker, client_id=f"{REPORT_VIA}-{uuid.uuid4().hex}",
            runner_requirements=(), chat_surface=False, initiator=initiator)
    except send_paths.NotInIt as e:
        raise ReportRefusedError(403, str(e)) from None
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=child.pk)
        meta = dict(locked.metadata or {})
        meta[lineage.REPORTS_KEY] = (list(meta.get(lineage.REPORTS_KEY) or []) + [{
            "at": time.time(), "by": asker.pk, "final": bool(final),
            "turn": str(turn.pk) if turn is not None else "",
        }])[-_REPORTS_KEPT:]
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    child.metadata = meta
    return turn
