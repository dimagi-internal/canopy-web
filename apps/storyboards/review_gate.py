"""A narrative review, carried by a storyboard.

The review page is retired as a human surface; the storyboard is where a
narrative is read, commented on and decided. The review API stays exactly as
it was — it is the DDD gate: ``scripts/ddd/review.py`` polls a ReviewRequest
until it resolves and reads its ``response_json``. This module is the bridge:

- every narrative review has a board (``board_for``): the first board in its
  workspace that carries the narrative, else a one-narrative board made for it;
- the review's gate decisions (``request_json.decisions``) are decision rows on
  that board (``sync_gate_rows``) — one set, for the narrative's newest pending
  review; and
- once a person has ruled every gate row of a pending review — directly, or by
  a round closing with the edit applied — the review resolves server-side with
  the same ``response_json`` shape the review page's submit wrote
  (``resolve_ruled``).

Product tier (storyboards and reviews are both product).
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.reviews.models import ReviewRequest
from apps.runs.ddd import is_run_child_gate, narrative_slug_from_run_id
from apps.storyboards import decisions as decision_rows
from apps.storyboards.models import Act, Entry, Storyboard, StoryboardDecision

logger = logging.getLogger(__name__)

#: Gate rows sit above the board's published rows.
_GATE_POSITION = -100
_SLUG_MAX = 100
_KEY_MAX = 120


def narrative_of(review: ReviewRequest) -> str | None:
    """The narrative a review belongs to; None for a run-child gate (findings)."""
    if is_run_child_gate(review.gate):
        return None
    return (review.narrative_slug or "").strip() or narrative_slug_from_run_id(review.run_id) or None


def _title(review: ReviewRequest, narrative_slug: str) -> str:
    from apps.reviews.titles import narrative_title

    title = narrative_title(review.request_json or {}, narrative_slug) or ""
    return (title.strip() or narrative_slug)[:300]


def _free_slug(workspace_id: str, narrative_slug: str) -> str:
    base = slugify(narrative_slug)[:_SLUG_MAX].strip("-") or "narrative"
    candidates = [base] + [f"{base[: _SLUG_MAX - len(s)]}{s}" for s in ("-review",)]
    candidates += [f"{base[: _SLUG_MAX - 8]}-review-{n}" for n in range(2, 50)]
    taken = set(
        Storyboard.objects.filter(workspace_id=workspace_id, slug__in=candidates).values_list("slug", flat=True)
    )
    return next(c for c in candidates if c not in taken)


def existing_board(workspace_id: str, narrative_slug: str) -> Storyboard | None:
    """The first board in the workspace that carries the narrative — the same
    choice the suggestion pool makes (``rounds.ingest_review_suggestion``)."""
    return (
        Storyboard.objects.filter(workspace_id=workspace_id, acts__entries__narrative_slug=narrative_slug)
        .distinct()
        .order_by("pk")
        .first()
    )


def board_for(review: ReviewRequest, *, create: bool = True) -> tuple[Storyboard | None, bool]:
    """(board, created). A narrative review's board, made when there is none.

    A made board is one narrative, one act, ``comment`` capability, in the
    review's workspace and linked to the review's board project when that
    project is in the same workspace (closing a round needs one)."""
    slug = narrative_of(review)
    if not slug or review.workspace_id is None:
        return None, False
    board = existing_board(review.workspace_id, slug)
    if board is not None or not create:
        return board, False
    project = review.agent_project
    if project is not None and project.agent.workspace_id != review.workspace_id:
        project = None
    title = _title(review, slug)
    try:
        with transaction.atomic():
            board = Storyboard.objects.create(
                slug=_free_slug(review.workspace_id, slug),
                title=title,
                lede="",
                brief={},
                capability=Storyboard.CAP_COMMENT,
                workspace_id=review.workspace_id,
                agent_project=project,
                source_session_id=review.source_session_id,
                source_turn_id=review.source_turn_id,
            )
            act = Act.objects.create(storyboard=board, title=title, position=0)
            Entry.objects.create(act=act, narrative_slug=slug, position=0)
    except IntegrityError:
        # A concurrent create won the slug; theirs carries the narrative too.
        board = existing_board(review.workspace_id, slug)
        return board, False
    return board, True


def current_review(workspace_id: str, narrative_slug: str) -> ReviewRequest | None:
    """The narrative's newest PENDING review — the one whose gate is open."""
    return (
        ReviewRequest.objects.filter(
            workspace_id=workspace_id, narrative_slug=narrative_slug, status=ReviewRequest.STATUS_PENDING
        )
        .exclude(gate__in=_run_child_gates())
        .order_by("-version", "-created_at")
        .first()
    )


def _run_child_gates():
    from apps.runs.ddd import RUN_CHILD_GATES

    return list(RUN_CHILD_GATES)


def gate_key(review: ReviewRequest, decision_id: str) -> str:
    words = slugify(decision_id).replace("_", "-").strip("-") or "decision"
    return f"gate-{review.pk.hex[:8]}-{words}"[:_KEY_MAX].rstrip("-")


def _gate_specs(review: ReviewRequest) -> list[dict]:
    """``request_json.decisions`` as publishable rows. A malformed decision is
    skipped (logged), never a failure — the review itself still stands."""
    out = []
    decisions = (review.request_json or {}).get("decisions") or []
    if not isinstance(decisions, list):
        return out
    slug = narrative_of(review) or ""
    version = f" (version {review.version})" if review.version else ""
    for d in decisions:
        if not isinstance(d, dict) or not str(d.get("id") or "").strip():
            continue
        raw = {
            "id": gate_key(review, str(d["id"])),
            "scope": slug,
            "question": str(d.get("prompt") or d.get("question") or d["id"]),
            "plain_question": str(d.get("plain_question") or d.get("prompt") or ""),
            "options": [str(o) for o in (d.get("options") or []) if isinstance(o, (str, int))],
            "recommended": str(d.get("recommended") or ""),
            "reasoning": str(d.get("reasoning") or ""),
            "source": f"Review gate {review.gate}{version}",
            "review_ask": "required-before",
            "needed_by": "next-run",
        }
        try:
            row = decision_rows.validate(raw)
        except decision_rows.DecisionInvalid as e:
            logger.warning("review %s: gate decision %r is not a valid row: %s", review.pk, d.get("id"), e)
            continue
        row["decision_id"] = str(d["id"])
        out.append(row)
    return out


def sync_gate_rows(board: Storyboard, narrative_slug: str) -> list[StoryboardDecision]:
    """Make the board's gate rows for ``narrative_slug`` match its newest
    pending review: create that review's rows if missing, and drop UNRULED gate
    rows left by older reviews of the same narrative. A ruled row is a person's
    record and stays. Idempotent."""
    review = current_review(board.workspace_id, narrative_slug)
    with transaction.atomic():
        stale = board.decisions.filter(review__isnull=False, scope=narrative_slug)
        if review is not None:
            stale = stale.exclude(review=review)
        for row in stale:
            if not decision_rows.is_ruled(row):
                row.delete()
        if review is None:
            return []
        rows = []
        for i, spec in enumerate(_gate_specs(review)):
            spec.pop("decision_id")
            key = spec.pop("id")
            row = board.decisions.filter(key=key).first()
            if row is None:
                row = StoryboardDecision.objects.create(
                    storyboard=board, key=key, review=review, position=_GATE_POSITION + i, **spec
                )
            rows.append(row)
        return rows


def ensure_for_review(review: ReviewRequest) -> Storyboard | None:
    """Board + gate rows for a narrative review. Never raises: the review
    exists whatever happens to its board."""
    try:
        board, _ = board_for(review)
        if board is not None:
            sync_gate_rows(board, narrative_of(review))
        return board
    except Exception:  # noqa: BLE001
        logger.exception("review %s: could not give it a storyboard", review.pk)
        return None


# ----------------------------------------------------------------- resolving


def response_for(review: ReviewRequest, values: dict[str, str], *, note: str = "") -> dict:
    """The ``response_json`` the review page's submit wrote, with no wording
    edits: the gate decisions, every scene as it was posted, the build order.
    The DDD reader (``scripts/ddd/narrative.py apply``) reads ``decisions`` and
    reconciles ``edited_scenes`` — an unchanged scene list is a no-op."""
    rj = review.request_json or {}
    narration = rj.get("narration") if isinstance(rj.get("narration"), list) else []
    scenes = []
    for n in narration:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        scenes.append({
            "id": str(n["id"]),
            "title": n.get("title") or "",
            "narration": n.get("text") or "",
            "deleted": False,
            "features": [
                {"id": f.get("id") or "", "description": f.get("description") or "",
                 "verify": f.get("verify") or "", "feedback": ""}
                for f in (n.get("features") or []) if isinstance(f, dict)
            ],
            "feedback": "",
        })
    build_order = rj.get("build_order") or [s["id"] for s in scenes]
    return {
        "decisions": values,
        "edited_scenes": scenes,
        "overall_feedback": note,
        "build_order": build_order,
    }


def _decision_ids(review: ReviewRequest) -> dict[str, str]:
    """gate row key -> the review's own decision id."""
    out = {}
    for d in (review.request_json or {}).get("decisions") or []:
        if isinstance(d, dict) and str(d.get("id") or "").strip():
            out[gate_key(review, str(d["id"]))] = str(d["id"])
    return out


def resolve_ruled(board: Storyboard) -> list[ReviewRequest]:
    """Resolve every pending review whose gate rows on ``board`` are all ruled.

    Runs in the caller's transaction (a round close rolls it back with the
    round). Returns the reviews it resolved."""
    resolved = []
    review_ids = (
        board.decisions.filter(review__isnull=False, review__status=ReviewRequest.STATUS_PENDING)
        .values_list("review_id", flat=True).distinct()
    )
    for rid in list(review_ids):
        with transaction.atomic():
            review = ReviewRequest.objects.select_for_update().filter(pk=rid).first()
            if review is None or review.status != ReviewRequest.STATUS_PENDING:
                continue
            ids = _decision_ids(review)
            rows = list(board.decisions.filter(review=review, superseded_by=""))
            if not rows or not all(decision_rows.is_ruled(r) for r in rows):
                continue
            values = {ids.get(r.key, r.key): decision_rows.value_of(r) for r in rows}
            note = "\n\n".join(r.override_reasoning for r in rows if r.override_reasoning)
            response = response_for(review, values, note=note)
            deciders = sorted({r.decided_by_name for r in rows if r.decided_by_name})
            response["resolved_via"] = {"storyboard": board.slug, "decided_by": deciders}
            review.response_json = response
            review.status = ReviewRequest.STATUS_RESOLVED
            review.resolved_at = timezone.now()
            review.save(update_fields=["response_json", "status", "resolved_at"])
            resolved.append(review)
    return resolved


def mirror_submission(review: ReviewRequest, *, by_user) -> None:
    """A review resolved through the API (``submit_review``): mark its gate
    rows ruled the same way, so the board does not still ask. Never raises."""
    try:
        values = (review.response_json or {}).get("decisions") or {}
        if not isinstance(values, dict):
            return
        for key, decision_id in _decision_ids(review).items():
            row = StoryboardDecision.objects.filter(review=review, key=key).first()
            value = values.get(decision_id)
            if row is None or decision_rows.is_ruled(row) or not isinstance(value, str):
                continue
            if value not in row.options:
                continue
            decision_rows.apply_edit(row, action="pick", value=value, by_user=by_user,
                                     by_name="the review API")
    except Exception:  # noqa: BLE001
        logger.exception("review %s: could not mirror its submission onto its board", review.pk)


def is_gate_row(row: StoryboardDecision) -> bool:
    return row.review_id is not None


# ----------------------------------------------------------------- the old link


def destination(review: ReviewRequest, *, token_ok: bool) -> str | None:
    """Where the review's old page now lives: its narrative on its board,
    app-relative. Carries the board's share token ONLY when the caller proved
    the review's own token (``token_ok``). None for a review with no narrative
    (a findings review keeps its own page)."""
    from apps.workspaces import links as wslinks

    board, _ = board_for(review)
    if board is None:
        return None
    sync_gate_rows(board, narrative_of(review))
    path = f"/storyboard/{board.slug}"
    if token_ok:
        path += f"?t={board.ensure_share_token()}"
    return wslinks.scoped_path(board.workspace_id, path) + f"#entry-{narrative_of(review)}"
