"""Canonical chat DTOs — shape DB rows into the ace-web wire contract.

The reusable chat kit (canopy-ui) speaks ace-web's protocol; these builders map
canopy's models onto that contract. Every canonical field is synthesized from an
existing column — no new model fields, no migrations. Persisted transcript rows
are historical, so their `status` is always "complete" and the streaming-only
timestamps are null (live streaming state lives on the client, driven by the
chat.stream_* frames, not in the DB).
"""
from __future__ import annotations

from .drafts import body_of
from .models import Draft, Message, SessionParticipant
from .schemas import MessageOut


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


def session_subagents(session) -> list[dict]:
    """The session's subagent summaries (`RunnerBinding.subagents`); [] with no
    binding."""
    binding = getattr(session, "runner_binding", None)
    return list(binding.subagents or []) if binding is not None else []


#: A row's extras, present on the socket only when set: an MCP Apps View
#: (`mcp_apps_views.annotate`), a secret request's card (`secret_requests.annotate`),
#: a sensitive conversation's sealed words (set only by `sensitivity.readable`).
_OPTIONAL_ROW_FIELDS = ("app", "secret_request", "sealed")


def message_dto(msg: Message) -> dict:
    """A transcript row on the socket: REST's `MessageOut` — the one message
    shape, so a row reads the same whichever transport carried it — plus the
    streaming fields the socket's live frames update in place."""
    row = MessageOut.from_orm(msg).model_dump(mode="json")
    for key in _OPTIONAL_ROW_FIELDS:
        if row.get(key) is None:
            row.pop(key, None)
    return {**row, "status": "complete", "error_detail": None,
            "started_at": None, "completed_at": None}


def draft_dto(draft: Draft | None, *, words: bool = True) -> dict | None:
    """The author's own draft. `words` False withholds the body: a sensitive
    conversation's draft reaches only a reader `sensitivity.reads_content` admits."""
    if draft is None:
        return None
    return {
        "id": str(draft.pk),
        "slot": draft.slot,
        "status": "open",
        "body": body_of(draft) if words else "",
        "version": draft.version,
        # `last_editor` kept for canopy-ui <= 0.12, which treats a draft whose
        # last_editor is itself as its own. This DTO only ever reaches its author.
        "last_editor": draft.author_id,
        "author_id": draft.author_id,
        "last_edit_at": _iso(draft.updated_at),
        # So the author's OTHER tabs show the same chosen mode.
        "visibility": draft.visibility,
    }


def peer_draft_dto(draft: Draft, *, words: bool = True) -> dict:
    """Someone ELSE's draft, as the live `draft.typing` row. Deliberately not the
    `draft_dto` shape: an old client adopts any draft-shaped frame as its own.

    THE ONLY PLACE the per-mode rule lives — the words must never leave the
    server for `typing`/`hidden`:
      - `live`   -> the body, plus `typing` reflecting whether it's non-empty.
      - `typing` -> body withheld (""), `typing` reflects the REAL body.
      - `hidden` -> body withheld (""), `typing` always False (nothing shows
                    until send).
    `words` False withholds the body whatever the mode (a sensitive
    conversation, a reader `sensitivity.reads_content` does not admit)."""
    body = body_of(draft)
    user = draft.author
    author = {"id": draft.author_id,
             "name": (user.get_full_name() or "").strip() or user.email}
    at = _iso(draft.updated_at)
    if draft.visibility == Draft.HIDDEN:
        return {"author": author, "body": "", "at": at, "typing": False}
    if draft.visibility == Draft.TYPING:
        return {"author": author, "body": "", "at": at, "typing": bool(body)}
    return {"author": author, "body": body if words else "", "at": at, "typing": bool(body)}


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


def participant_dto_for(user, role: str) -> dict:
    """A roster entry for someone reading WITHOUT a participant row."""
    display = (user.get_full_name() or "").strip() or user.email
    return {
        "user_id": user.pk,
        "email": user.email,
        "display_name": display,
        "role": role,
        "joined_at": None,
        "last_seen_at": None,
    }


def session_state_dto(*, session, current_user_id, participants, present_ids, draft,
                      messages, peer_drafts=(), queued=(), cancelled=(), current_contact_id=None,
                      draft_words: bool = True, activity: str | None = None,
                      has_more_before: bool = False,
                      oldest_loaded_turn_index: int | None = None) -> dict:
    """The canonical `session.state` snapshot payload. `draft_words` False
    withholds every draft's body (`draft_dto`)."""
    return {
        "messages": [message_dto(m) for m in messages],
        # The scroll-back cursor, as REST's `GET /{id}` returns it: whether older
        # rows exist, and the turn_index to page back from (`GET /{id}/messages`).
        "has_more_before": has_more_before,
        "oldest_loaded_turn_index": oldest_loaded_turn_index,
        # The CONNECTING user's own draft (None for a contact, who has none).
        "active_draft": draft_dto(draft, words=draft_words),
        # Everyone else's draft in progress, as `draft.typing` rows.
        "peer_drafts": [peer_draft_dto(d, words=draft_words) for d in peer_drafts],
        # Human sends that have not reached the transcript yet, visible to
        # everyone watching — in the snapshot for the same reason `menu` and
        # `turn_status` are: a client that just connected has no live frame to
        # have caught it from.
        "queued": list(queued),
        # Sends whose turn ended without their words reaching the transcript
        # (`send.ended_sends`): kept in place, each with its final `state`.
        "cancelled": list(cancelled),
        # What the session is doing right now (`session_state.activity`: working |
        # blocked | idle), the same values as the runner's live `session.activity`
        # frames. In the snapshot so a reconnect replaces whatever a client last
        # heard — a live frame that was missed cannot leave it saying "working".
        "activity": activity,
        # Already DTOs: the socket merges rowless readers in (`_snapshot`).
        "participants": [p if isinstance(p, dict) else participant_dto(p) for p in participants],
        "presence_user_ids": list(present_ids),
        "current_user_id": current_user_id,
        # The connecting principal when it is a CONTACT (a widget visitor), who
        # has no user id: without it a client cannot tell which authored lines
        # are the viewer's own. Null for a member.
        "current_contact_id": current_contact_id,
        # The dialog the agent is waiting on, if any. In the SNAPSHOT and not
        # only in a live frame, because `session.activity` is view-only and
        # reaches a client only if it was already connected when the agent
        # blocked — which is exactly the case that fails: you go to the phone
        # BECAUSE the session stopped, so you were never watching.
        "menu": pending_menu(session),
        # What the session's subagents are doing, for the `Agent` rows. In the
        # snapshot for the same reason as `menu`: a background agent's work
        # happens while nobody is watching.
        "subagents": session_subagents(session),
        # Where the ask this session is waiting on currently stands. In the
        # snapshot for the same reason `menu` is: you open the page BECAUSE it
        # went quiet, so the one client that most needs the status is the one
        # that was not connected when it changed.
        "turn_status": _turn_status(session),
    }


def _turn_status(session) -> dict | None:
    """Never raises: a snapshot that cannot say where the turn is must still
    deliver the conversation."""
    try:
        from .status_feed import status_for_session

        return status_for_session(session)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("could not resolve turn status")
        return None
