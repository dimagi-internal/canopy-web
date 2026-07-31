"""Canonical chat DTOs — shape DB rows into the ace-web wire contract.

The reusable chat kit (canopy-ui) speaks ace-web's protocol; these builders map
canopy's models onto that contract. Every canonical field is synthesized from an
existing column — no new model fields, no migrations. Persisted transcript rows
are historical, so their `status` is always "complete" and the streaming-only
timestamps are null (live streaming state lives on the client, driven by the
chat.stream_* frames, not in the DB).
"""
from __future__ import annotations

from .models import Draft, Message, SessionParticipant


def _iso(dt):
    return dt.isoformat() if dt else None


def pending_menu(session) -> dict | None:
    """The dialog this session is blocked on, or None.

    ONE reader for every surface — the REST detail, the WS connect snapshot and
    the list flag all call this, so they cannot disagree about whether an agent
    is waiting. (The live `session.activity` frame is a fourth producer of the
    same shape, and deliberately carries the identical dict.)

    A session with no binding has no live screen and therefore nothing to be
    blocked on: a web chat that has never been sent is not waiting on anybody.
    """
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    if binding is None:
        return None
    return binding.pending_question or None


def message_dto(msg: Message) -> dict:
    return {
        "id": str(msg.pk),
        "turn_index": msg.turn_index,
        "role": msg.role,
        "content": msg.content or {},
        "plaintext": msg.plaintext,
        "status": "complete",
        "error_detail": None,
        "started_at": None,
        "completed_at": None,
        "created_at": _iso(msg.created_at),
    }


def draft_dto(draft: Draft | None) -> dict | None:
    if draft is None:
        return None
    return {
        "id": str(draft.pk),
        "slot": draft.slot,
        "status": "open",
        "body": draft.body,
        "version": draft.version,
        "last_editor": draft.last_editor_id,
        "last_edit_at": _iso(draft.updated_at),
    }


def participant_dto(sp: SessionParticipant) -> dict:
    user = sp.user
    display = (user.get_full_name() or "").strip() or user.email
    return {
        "user_id": sp.user_id,
        "email": user.email,
        "display_name": display,
        "role": sp.role,
        "joined_at": _iso(getattr(sp, "created_at", None)),
        "last_seen_at": _iso(getattr(sp, "last_seen_at", None)),
    }


def session_state_dto(*, session, current_user_id, participants, present_ids, draft, messages) -> dict:
    """The canonical `session.state` snapshot payload."""
    return {
        "messages": [message_dto(m) for m in messages],
        "active_draft": draft_dto(draft),
        "participants": [participant_dto(p) for p in participants],
        "presence_user_ids": list(present_ids),
        "current_user_id": current_user_id,
        # The dialog the agent is waiting on, if any. In the SNAPSHOT and not
        # only in a live frame, because `session.activity` is view-only and
        # reaches a client only if it was already connected when the agent
        # blocked — which is exactly the case that fails: you go to the phone
        # BECAUSE the session stopped, so you were never watching.
        "menu": pending_menu(session),
    }
