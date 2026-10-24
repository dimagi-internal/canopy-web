"""Storyboard feedback rounds — the product rules over apps.feedback.rounds.

Who may open/close/consume, what versions a round pins, which anchors a note
may carry, who sees round notes, and what closing does (one task, one turn)."""
from __future__ import annotations

import logging
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.feedback import rounds as frounds
from apps.feedback.models import Feedback
from apps.runs import aggregate
from apps.storyboards.decisions import public_name
from apps.storyboards.models import Entry, Storyboard

logger = logging.getLogger(__name__)

TARGET = "storyboard"
_ANCHOR_PREFIXES = ("act:", "cut:", "decision:", "product:", "scene:")
RoundError = frounds.RoundError
#: A round's staged-edit list lives in one JSON column; cap it so it stays small.
MAX_STAGED_EDITS = 500
#: `AgentTask.title` is a CharField(max_length=300).
_TASK_TITLE_MAX = 300


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


def _check_project(board, *, verb: str) -> None:
    """The board's project must exist and sit in the board's own workspace: closing
    starts that project's agent, which must never be another tenant's."""
    if board.agent_project_id is None:
        raise RoundError(422, f"Link this storyboard to a board project before {verb}: "
                              "closing the round hands it to that project's agent.")
    if board.agent_project.workspace_id != board.workspace_id:
        raise RoundError(422, f"Link this storyboard to a board project in its own workspace "
                              f"before {verb}: closing the round hands it to that project's agent.")


def open_round(board, *, by_user):
    _check_project(board, verb="opening a round")
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


def _people_count(rnd) -> int:
    """Distinct named authors, counted by the name readers see (never an email)."""
    names = rnd.notes.exclude(author_name="").values_list("author_name", flat=True)
    return len({_display_name(n, "") for n in names} - {""})


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
        "people_count": _people_count(rnd),
    }


def note_out(fb, *, viewer) -> dict:
    return {
        "id": fb.pk, "kind": fb.kind, "body": fb.body, "suggested_text": fb.suggested_text,
        # An adopted pool note may carry an address as its name: show the local-part.
        "author_name": _display_name(fb.author_name, "") or "Anonymous", "anchor_id": fb.anchor_id,
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
_STAGED_VALUE_MAX = 200
_STAGED_REASONING_MAX = 2000


def stage(rnd, row, *, action, value, reasoning, by_name, by_user=None) -> dict:
    # A confirm carries no value: what is confirmed is the row's recommendation.
    value = "" if action == "confirm" else (value or "").strip()
    reasoning = (reasoning or "").strip()
    if len(value) > _STAGED_VALUE_MAX:
        raise RoundError(422, f"a staged value is at most {_STAGED_VALUE_MAX} characters")
    if len(reasoning) > _STAGED_REASONING_MAX:
        raise RoundError(422, f"reasoning is at most {_STAGED_REASONING_MAX} characters")
    return frounds.stage_edit(rnd, {
        "key": row.key, "action": action, "value": value,
        "reasoning": reasoning, "by_name": by_name,
        "by_user_id": by_user.pk if getattr(by_user, "is_authenticated", False) else None,
    }, max_edits=MAX_STAGED_EDITS)


def pending_by_key(rnd) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in (rnd.staged_edits if rnd is not None else []):
        out.setdefault(e.get("key", ""), []).append({k: e.get(k, "") for k in _STAGED_KEYS})
    return out


def pending_for(board, key: str) -> list[dict]:
    return pending_by_key(current(board)).get(key, [])


# ------------------------------------------------------------------ closing


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _intent(edit: dict, recommended: str) -> tuple[str, str]:
    """What an edit would do to its row, so two spellings of one wish agree:
    a write-in is a pick, and picking the recommendation is confirming it
    (exactly as `decisions.apply_edit` resolves them)."""
    action, value = edit.get("action", ""), _norm(edit.get("value", ""))
    if action == "write_in":
        action = "pick"
    if action == "pick" and value == _norm(recommended):
        return ("confirm", "")
    return (action, "" if action == "confirm" else value)


def _choose(board, staged: list[dict], picks: dict[str, str]) -> list[dict]:
    """One edit per row. Same intent from several people is not a conflict;
    different intents need the closer's pick (spec C, Closing a round)."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for e in staged:
        by_key[e["key"]].append(e)
    recommended = dict(board.decisions.filter(key__in=list(by_key)).values_list("key", "recommended"))
    chosen, conflicts = [], []
    for key, edits in by_key.items():
        intents = {_intent(e, recommended.get(key, "")) for e in edits}
        if len(intents) == 1:
            chosen.append(edits[-1])
        elif key in picks:
            match = next((e for e in edits if e["edit_id"] == picks[key]), None)
            if match is None:
                raise RoundError(422, f"{key}: the picked edit is not one of this round's edits")
            chosen.append(match)
        else:
            conflicts.append(key)
    if conflicts:
        raise RoundError(409, "People disagreed on these decisions; pick one edit for each: "
                              + ", ".join(sorted(conflicts)))
    return chosen


def _skipped_text(skipped: list[dict]) -> str:
    """The staged edits a close could not apply, for the agent to rule on."""
    if not skipped:
        return ""
    lines = ["Staged edits not applied \u2014 their decision changed under them during "
             "the round; carry each into the next version or decline it:"]
    for e in skipped:
        where = e.get("reason") or (f"replaced by {e['superseded_by']}" if e.get("superseded_by")
                                    else "removed by the publisher")
        what = e.get("action", "")
        if e.get("value"):
            what += f" \u201c{e['value']}\u201d"
        lines.append(f"- {e['key']} ({where}): {e.get('by_name') or 'someone'} wanted {what}")
    return "\n".join(lines)


def _task_title(number: int, board_title: str, tail: str) -> str:
    """Fit the title column: shorten the board's title, never the counts."""
    head = f"Feedback round {number} on \u201c"
    rest = f"\u201d: {tail}"
    room = _TASK_TITLE_MAX - len(head) - len(rest)
    title = board_title if len(board_title) <= room else board_title[: max(room - 1, 0)].rstrip() + "\u2026"
    return (head + title + rest)[:_TASK_TITLE_MAX]


def _hand_to_agent(board, rnd, *, by_user, note: str, changed: int, skipped: list[dict]):
    """One task on the board's project, approved once -> exactly one turn.

    The task asks for review and is approved by the closer in the same breath:
    `act(approve)` on a card with no `on_approve` enqueues the card's own turn
    (`apps.harness.dispatch.enqueue_task_turn`). Both are plain DB writes inside
    the caller's transaction, so a failure rolls them back with the round."""
    from apps.agents import services as agent_services
    from apps.workspaces import links as wslinks

    project = board.agent_project
    notes = rnd.notes.count()
    people = _people_count(rnd)
    where = f"?ws={board.workspace_id}"
    body = "\n\n".join(p for p in ((note or "").strip(), _skipped_text(skipped)) if p)
    (task,) = agent_services.create_tasks(project.agent, [{
        "title": _task_title(rnd.number, board.title,
                             f"{notes} {'note' if notes == 1 else 'notes'} from {people} "
                             f"{'person' if people == 1 else 'people'}, "
                             f"{changed} {'decision' if changed == 1 else 'decisions'} changed"),
        "ask_kind": "review",
        "ask_body": body,
        "project": project.ext_id,
        "source_url": wslinks.scoped_url(board.workspace_id, f"/storyboard/{board.slug}"),
        "next_action": f"Run ddd-feedback-round for storyboard {board.slug} round {rnd.number}.",
        "plan": (f"1. Read the round: GET /api/storyboards/{board.slug}/rounds/{rnd.number}{where}\n"
                 "2. Answer every note: POST /api/feedback/<id>/resolve (answered or declined).\n"
                 "3. Publish the next version of each narrative the notes change.\n"
                 f"4. POST /api/storyboards/{board.slug}/rounds/{rnd.number}/consume{where} "
                 "with the run id \u2014 this opens the next round."),
        "idempotency_key": f"storyboard-round-{rnd.pk}",
    }])
    try:
        agent_services.act(
            task, action="approve", comment=note or "", by=by_user.email, by_user=by_user,
            actor_workspace_ids={board.workspace_id}, may_start_turns=True,
        )
    except agent_services.ClosedAskError:
        # Backstop only: `lock_open` already refuses a second close. Should the
        # idempotency key ever replay an already-approved task (a lock the
        # database did not honour, e.g. SQLite), refuse cleanly — never a 500,
        # never a second turn — and let the caller's transaction roll back.
        raise RoundError(409, f"round {rnd.number} is closed") from None
    return task


def close_round(board, rnd, *, by_user, note: str = "", picks: dict[str, str] | None = None):
    """Close `rnd` in ONE transaction: lock it, apply its staged edits (the
    closer resolves disagreements via `picks`), mark it closed, and hand it to
    the board project's agent as one approved task -> one turn. Any failure
    leaves the round open with nothing applied and no task or turn.

    An edit staged on a row the publisher replaced (superseded) or removed
    mid-round is skipped, not applied, and listed on the task for the agent to
    rule on — a routine republish must never make the round impossible to close."""
    from apps.storyboards import decisions as decision_rows

    user_model = get_user_model()
    with transaction.atomic():
        rnd = frounds.lock_open(rnd)
        board.refresh_from_db(fields=["agent_project"])
        _check_project(board, verb="closing the round")
        rows = {r.key: r for r in board.decisions.all()}
        live, skipped = [], []
        for e in rnd.staged_edits:
            row = rows.get(e["key"])
            if row is None or row.superseded_by:
                skipped.append({**e, "superseded_by": row.superseded_by if row else ""})
            elif e.get("action") == "pick" and e.get("value", "") not in row.options:
                # A republish replaced an unruled row's options in place.
                skipped.append({**e, "reason": "the value is no longer one of its options"})
            else:
                live.append(e)
        changed = 0
        try:
            for e in _choose(board, live, picks or {}):
                uid = e.get("by_user_id")
                user = user_model.objects.filter(pk=uid).first() if uid else None
                try:
                    with transaction.atomic():
                        decision_rows.apply_edit(
                            rows[e["key"]], action=e["action"], value=e.get("value", ""),
                            reasoning=e.get("reasoning", ""), by_user=user,
                            by_name=e.get("by_name", ""))
                except decision_rows.DecisionInvalid as exc:
                    # Valid when staged, stale now: hand it to the agent, never block the close.
                    skipped.append({**e, "reason": f"it no longer applies: {exc}"})
                    continue
                changed += 1
        except decision_rows.DecisionConflict as exc:
            raise RoundError(409, str(exc)) from None
        # A gate row ruled by this close resolves its review (the DDD gate).
        from apps.storyboards import review_gate

        review_gate.resolve_ruled(board)
        frounds.mark_closed(rnd, by_user=by_user, note=note)
        rnd.task = _hand_to_agent(board, rnd, by_user=by_user, note=note,
                                  changed=changed, skipped=skipped)
        rnd.save(update_fields=["task"])
    return rnd


def delete_open_round_note(board, note_id: int) -> None:
    """Hard-delete a note from this board's CURRENT OPEN round (a test or spam
    note every link holder would otherwise see). Closed and consumed rounds are
    the agent's record and stay immutable."""
    with transaction.atomic():
        # A note targets its narrative; what ties it to this board is its round.
        fb = Feedback.objects.filter(pk=note_id, round__workspace_id=board.workspace_id,
                                     round__target_kind=TARGET, round__target_ref=board.slug
                                     ).select_related("round").first()
        if fb is None:
            raise RoundError(404, "no such note on this storyboard")
        try:
            frounds.lock_open(fb.round)
        except RoundError:
            raise RoundError(409, "only a note in the open round can be removed") from None
        fb.delete()


def detail(board, rnd, *, viewer) -> dict:
    """What the agent reads: every note with its author's email, the staged
    edits, and the board's current decisions (rulings are binding)."""
    from apps.storyboards import services
    from apps.workspaces import links as wslinks

    out = round_payload(board, rnd, viewer=viewer)
    emails = dict(rnd.notes.values_list("pk", "author_email"))
    for n in out["notes"]:
        n["author_email"] = emails.get(n["id"], "")
    out["decisions"] = services.board_decisions(board)
    out["board_url"] = wslinks.scoped_url(board.workspace_id, f"/storyboard/{board.slug}")
    return out


def consume_round(board, rnd, *, run_id: str, by_user):
    """Mark a closed, fully answered round consumed and open the next one, in one
    transaction — a refusal either way leaves the round closed."""
    with transaction.atomic():
        frounds.mark_consumed(rnd, run_id=run_id)
        return open_round(board, by_user=by_user)


def _display_name(name: str | None, email: str | None) -> str:
    """The pool shows a person's name, never an address: fall back to the local-part."""
    name = (name or "").strip()
    if name and "@" not in name:
        return name
    address = name if "@" in name else (email or "").strip()
    return address.split("@")[0] if address else ""


def ingest_review_suggestion(review, *, name, email, response_json, count, by_user=None) -> list[int]:
    """A review-page suggestion is feedback too: one narrative, one pool (spec C).

    Lands on the open round of any board in the review's workspace that carries the
    narrative. Never raises: a pool write must not cost the reviewer their suggestion."""
    try:
        slug = review.narrative_slug or ""
        rj = response_json if isinstance(response_json, dict) else {}
        base = {"target_kind": "narrative", "target_ref": slug, "target_version": review.version,
                "author_name": _display_name(name, email), "author_email": (email or "").strip(),
                "channel": "web"}
        ref = f"review:{review.pk}:{count}:"
        items = []
        for s in rj.get("edited_scenes") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            if s.get("deleted"):
                items.append({**base, "kind": "comment", "anchor_id": s["id"], "body": "Remove this scene",
                              "source_ref": f"{ref}{s['id']}:remove"})
            else:
                items.append({**base, "kind": "suggestion", "anchor_id": s["id"],
                              "suggested_text": s.get("narration") or "", "body": s.get("feedback") or "",
                              "source_ref": f"{ref}{s['id']}:scene"})
        for scene_id, text in (rj.get("narration_edits") or {}).items():
            items.append({**base, "kind": "suggestion", "anchor_id": scene_id, "suggested_text": text,
                          "source_ref": f"{ref}{scene_id}:narration"})
        if (rj.get("overall_feedback") or "").strip():
            items.append({**base, "kind": "comment", "anchor_id": "", "body": rj["overall_feedback"],
                          "source_ref": f"{ref}overall"})
        if not items or not slug or review.workspace_id is None:
            return []
        boards = Storyboard.objects.filter(
            workspace_id=review.workspace_id, acts__entries__narrative_slug=slug
        ).distinct().order_by("pk")
        rnd = next((r for r in (current(b) for b in boards) if r is not None), None)
        return frounds.ingest_into(rnd, items, workspace=review.workspace, submitted_by=by_user)["ids"]
    except Exception:  # noqa: BLE001
        logger.exception("rounds: could not pool review %s's suggestion", review.pk)
        return []
