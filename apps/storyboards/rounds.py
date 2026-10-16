"""Storyboard feedback rounds — the product rules over apps.feedback.rounds.

Who may open/close/consume, what versions a round pins, which anchors a note
may carry, who sees round notes, and what closing does (one task, one turn)."""
from __future__ import annotations

import logging

from apps.feedback import rounds as frounds
from apps.feedback.models import Feedback
from apps.runs import aggregate
from apps.storyboards.decisions import public_name
from apps.storyboards.models import Entry, Storyboard

logger = logging.getLogger(__name__)

TARGET = "storyboard"
_ANCHOR_PREFIXES = ("act:", "cut:", "decision:", "product:", "scene:")
RoundError = frounds.RoundError


def _where(board: Storyboard) -> dict:
    return {"workspace_id": board.workspace_id, "target_kind": TARGET, "target_ref": board.slug}


def current(board):
    return frounds.current(**_where(board))


def by_number(board, number: int):
    return frounds.by_number(**_where(board), number=number)


def latest_finished(board):
    return frounds.latest_finished(**_where(board))


def pinned_versions(board) -> dict[str, int]:
    """Each narrative's current version — what everyone in the round watches."""
    out: dict[str, int] = {}
    for slug in Entry.objects.filter(act__storyboard=board).values_list("narrative_slug", flat=True):
        narrative = aggregate.build_narrative(slug, {board.workspace_id}) or {}
        versions = narrative.get("versions") or []
        if versions and versions[0].get("version") is not None:
            out[slug] = int(versions[0]["version"])
    return out


def open_round(board, *, by_user):
    if board.agent_project_id is None:
        raise RoundError(422, "Link this storyboard to a board project before opening a round: "
                              "closing the round hands it to that project's agent.")
    from apps.storyboards import services

    return frounds.open_round(
        workspace=board.workspace, target_kind=TARGET, target_ref=board.slug,
        pinned_versions=pinned_versions(board), by_user=by_user,
        adopt=services.board_feedback(board).filter(state=Feedback.STATE_NEW),
    )


def check_anchor(board, anchor_id: str) -> None:
    a = (anchor_id or "").strip()
    if not a:
        return
    if a.startswith("decision:"):
        if not board.decisions.filter(key=a.split(":", 1)[1]).exists():
            raise RoundError(404, "no such decision on this storyboard")
        return
    if a.startswith(_ANCHOR_PREFIXES):
        return
    if ":" in a:
        raise RoundError(422, f"unknown anchor {a!r}")
    # A bare id is a scene id — the convention NarrativeReviewPage already uses.


def author_for(user, payload_name: str, *, member: bool, in_round: bool) -> str:
    if member and getattr(user, "is_authenticated", False):
        return public_name(user)
    name = (payload_name or "").strip()
    if in_round and not name:
        raise RoundError(422, "Tell us your name so the others in this round know who said it.")
    return name


def link_participant(board, user) -> None:
    """A signed-in member who comments joins the board's project (best-effort)."""
    if board.agent_project_id is None or not getattr(user, "is_authenticated", False):
        return
    try:
        from apps.agents import participants
        from apps.agents.models import ProjectParticipant
        from apps.contacts import people

        person = people.by_email(user.email or "")
        if person is not None:
            participants.ensure(board.agent_project, person, source=ProjectParticipant.REVIEW)
    except Exception:  # noqa: BLE001
        logger.exception("rounds: could not link %s to %s's project", user.pk, board.slug)


def summary(rnd) -> dict:
    notes = rnd.notes.all()
    return {
        "number": rnd.number,
        "state": rnd.state,
        "opened_at": rnd.opened_at.isoformat(),
        "opened_by": public_name(rnd.opened_by) if rnd.opened_by else "",
        "closed_at": rnd.closed_at.isoformat() if rnd.closed_at else None,
        "close_note": rnd.close_note,
        "pinned_versions": rnd.pinned_versions,
        "note_count": notes.count(),
        "people_count": notes.exclude(author_name="").values("author_name").distinct().count(),
    }


def note_out(fb, *, viewer) -> dict:
    return {
        "id": fb.pk, "kind": fb.kind, "body": fb.body, "suggested_text": fb.suggested_text,
        "author_name": fb.author_name or "Anonymous", "anchor_id": fb.anchor_id,
        "target_kind": fb.target_kind, "target_ref": fb.target_ref,
        "target_version": fb.target_version, "state": fb.state,
        "disposition_note": fb.disposition_note, "resolved_in_version": fb.resolved_in_version,
        "created_at": fb.created_at.isoformat(),
        "mine": bool(getattr(viewer, "is_authenticated", False) and fb.submitted_by_id == viewer.pk),
    }


def round_payload(board, rnd, *, viewer) -> dict | None:
    if rnd is None:
        return None
    notes = rnd.notes.exclude(body="", suggested_text="").order_by("created_at")
    return {
        **summary(rnd),
        "notes": [note_out(fb, viewer=viewer) for fb in notes],
        "staged_edits": [
            {k: e.get(k, "") for k in ("edit_id", "key", "action", "value", "reasoning", "by_name", "at")}
            for e in rnd.staged_edits
        ],
    }


_STAGED_KEYS = ("edit_id", "key", "action", "value", "reasoning", "by_name", "at")


def stage(rnd, row, *, action, value, reasoning, by_name, by_user=None) -> dict:
    return frounds.stage_edit(rnd, {
        "key": row.key, "action": action, "value": (value or "").strip(),
        "reasoning": (reasoning or "").strip(), "by_name": by_name,
        "by_user_id": by_user.pk if getattr(by_user, "is_authenticated", False) else None,
    })


def pending_by_key(rnd) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in (rnd.staged_edits if rnd is not None else []):
        out.setdefault(e.get("key", ""), []).append({k: e.get(k, "") for k in _STAGED_KEYS})
    return out


def pending_for(board, key: str) -> list[dict]:
    return pending_by_key(current(board)).get(key, [])
