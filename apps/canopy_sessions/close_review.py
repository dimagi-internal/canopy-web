"""/ide close review: an agent proposes sessions to close, a person confirms.

Spec: docs/superpowers/specs/2026-10-16-ide-close-review-design.md. The MCP
tools (`apps/mcp/tools/close_review.py`) are thin; this module is the rule.

**The agent never closes.** `proposal_rows` only reads. `close` runs only from
the review View's button (an app-only tool, refused to anything but a person's
grant), as the person who clicked, through the same `control.close_session`
the /ide right-click uses. The agent reads the candidates from /ide itself — a
page action the person's own browser answers — so no server read here widens
what an agent's login can see.

**The rows are canopy's, never the agent's.** Every row is re-read here at the
moment it is asked for, with the caller's own access, so the panel shows the
same facts the rail does — not the agent's copy of them, which could be stale
or wrong. The agent contributes only its quote and its reason.
"""
from __future__ import annotations

import uuid

from . import access
from . import control
from . import session_state, transcript_read
from .models import Session
from .session_row import session_out

#: A proposal's quote and reason, as shown in the panel.
QUOTE_CHARS = 400
WHY_CHARS = 300
#: One review at most this many sessions — /ide's page state carries no more.
MAX_SESSIONS = 150


def _ids(values) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values or []:
        try:
            u = uuid.UUID(str(v))
        except (TypeError, ValueError):
            continue
        if u not in out:
            out.append(u)
    return out[:MAX_SESSIONS]


def _readable(user, ids: list[uuid.UUID]) -> dict[uuid.UUID, Session]:
    rows = (access.readable_sessions(user).filter(pk__in=ids)
            .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
            .annotate(_last_msg_at=transcript_read.last_message_at()))
    rows = session_state.with_state(transcript_read.with_last_reply(transcript_read.with_driving_turn(rows)))
    return {s.pk: s for s in rows}


def _blocked(user, session: Session, row: dict) -> str:
    """Why this session cannot be closed from the review right now, or "".

    The same refusals the right-click menu shows (`closeIntent`), checked here
    because the View's rows must be true when the person clicks."""
    from apps.harness.models import Runner

    if session.status == Session.ARCHIVED:
        return "Already closed"
    if not access.can_write(user, session):
        return "You can read this session but not close it"
    if row["running"]:
        return "It is running a turn"
    if row["waiting_on_you"]:
        return "It is waiting on you"
    binding = getattr(session, "runner_binding", None)
    if control.is_runner_reported(binding) and \
            binding.runner.live_status not in (Runner.ONLINE, Runner.DEGRADED):
        return f"Its runner {binding.runner.name} is offline"
    return ""


def _facts(user, session: Session) -> dict:
    # The list's own row, so the panel and the rail agree.
    row = session_out(session, reply=True, viewer=user)
    reply = row["last_reply"]
    return {
        "id": str(session.pk),
        "title": row["title"] or "(untitled)",
        "project": row["emdash_project"] or row["project"] or "",
        "agent": row["agent_slug"] or "",
        "runner": row["runner_name"] or "",
        "runner_status": row["runner_status"] or "",
        "status": row["status"],
        "running": bool(row["running"]),
        "waiting_on_you": bool(row["waiting_on_you"]),
        "mode": row["turn_mode"] or "",
        "last_activity_at": row["last_activity_at"].isoformat(),
        "agent_spoke_last": bool(row["agent_spoke_last"]),
        "last_reply": reply,
        "blocked": _blocked(user, session, row),
    }


def proposal_rows(user, proposals) -> dict:
    """The agent's proposals as rows the review View renders: canopy's facts,
    re-read now, beside the agent's own quote and reason."""
    said: dict[uuid.UUID, dict] = {}
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        ids = _ids([p.get("session_id")])
        if ids and ids[0] not in said:
            said[ids[0]] = p
    ids = list(said)[:MAX_SESSIONS]
    found = _readable(user, ids)
    rows = []
    for i in ids:
        if i not in found:
            continue
        p = said[i]
        rows.append({
            **_facts(user, found[i]),
            "quote": str(p.get("quote") or "")[:QUOTE_CHARS],
            "why": str(p.get("why") or "")[:WHY_CHARS],
        })
    # How many were proposed, not which the caller could not read: an agent's own
    # login reads none of a person's sessions, and the View re-reads as the person.
    return {"proposed": len(ids), "rows": rows}


def close(user, session_ids) -> dict:
    """Close each session the caller may write, as the right-click Delete does.

    One outcome per id: `closed`, `closing` (relayed to its runner, which
    deletes the emdash task), or a refusal — `not_found`, or `blocked` with the
    reason (re-checked now: a session that started running after the review
    rendered is not closed under the person who ticked it)."""
    ids = _ids(session_ids)
    found = _readable(user, ids)
    results = []
    for i in ids:
        session = found.get(i)
        reason = ""
        if session is None:
            outcome = "not_found"
        else:
            reason = _blocked(user, session, session_out(session, reply=True, viewer=user))
            outcome = "blocked" if reason else control.close_session(session=session)
        results.append({"id": str(i), "title": session.title if session else "",
                         "outcome": outcome, "reason": reason})
    return {"results": results,
            "closed": sum(r["outcome"] in ("closed", "closing") for r in results)}
