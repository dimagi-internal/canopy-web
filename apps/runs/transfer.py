"""Moving a narrative between workspaces — a first-class, supported operation.

Why this needs to exist at all: a DDD narrative is **not a table**. It is
inferred at read time from the ``ReviewRequest`` and ``Walkthrough`` rows that
share its slug. So "which workspace owns this narrative" is not one field — it
is the same answer repeated across every artifact, with nothing keeping them
in agreement. Post one version from a differently scoped caller and the lineage
silently splits across tenants, and neither side can read its own history.

That happened on labs: ``create-survey-solicitation`` had v12 and v7..v1 in
``dimagi`` while v8..v11 sat in ``connect``. A storyboard scoped to one tenant
then diffed v12 against v7 instead of v11 — a month-wide diff presented as one
iteration.

Two reasons this is a supported operation rather than a repair script:
a narrative can land in the wrong tenant by accident, and a narrative can
legitimately need to move (it turns out to belong to another team). Both want
the same verb.

Request-free so the REST route, the management command, and any future MCP tool
share one implementation — the discipline ``apps/harness/schedule_services.py``
established.
"""
from __future__ import annotations

from django.db import transaction

from apps.reviews.models import ReviewRequest
from apps.runs.aggregate import narrative_of_review, narrative_of_walkthrough
from apps.storyboards.models import Entry, Storyboard
from apps.walkthroughs.models import Walkthrough


class TransferError(Exception):
    """The move cannot proceed as asked."""


def plan_move(slugs: set[str], target: str) -> dict:
    """What ``apply_move`` would do. Pure read — touches nothing.

    Always compute this first: it is the only record of where the artifacts were
    before the move, and the move itself has no undo beyond running it again in
    reverse.
    """
    reviews = [r for r in ReviewRequest.objects.all() if narrative_of_review(r) in slugs]
    walkthroughs = [w for w in Walkthrough.objects.all() if narrative_of_walkthrough(w) in slugs]

    # A storyboard resolves its entries against ITS OWN workspace, so a board
    # left behind after its narratives move renders nothing but placeholders —
    # that would relocate the split rather than heal it.
    board_ids = set(
        Entry.objects.filter(narrative_slug__in=slugs).values_list(
            "act__storyboard_id", flat=True
        )
    )
    boards = list(Storyboard.objects.filter(id__in=board_ids))

    per_narrative: dict[str, dict] = {}
    for slug in sorted(slugs):
        by_workspace: dict[str, list[int]] = {}
        for r in reviews:
            if narrative_of_review(r) == slug:
                by_workspace.setdefault(str(r.workspace_id), []).append(r.version or 0)
        n_wts = sum(1 for w in walkthroughs if narrative_of_walkthrough(w) == slug)
        # Only report narratives that actually exist. A slug with no artifacts is
        # not an empty narrative, it is a typo — and the caller should hear 404
        # rather than a confident plan to move nothing.
        if not by_workspace and not n_wts:
            continue
        per_narrative[slug] = {
            "versions_by_workspace": {k: sorted(v) for k, v in sorted(by_workspace.items())},
            "walkthroughs": n_wts,
            "split": len(by_workspace) > 1,
        }

    return {
        "target": target,
        "narratives": per_narrative,
        "source_workspaces": sorted(
            {str(r.workspace_id) for r in reviews if str(r.workspace_id) != target}
        ),
        "reviews_to_move": sum(1 for r in reviews if str(r.workspace_id) != target),
        "walkthroughs_to_move": sum(1 for w in walkthroughs if str(w.workspace_id) != target),
        "storyboards_to_move": sum(1 for b in boards if str(b.workspace_id) != target),
        "_reviews": reviews,
        "_walkthroughs": walkthroughs,
        "_boards": boards,
    }


def public_plan(plan: dict) -> dict:
    """The plan without the model instances, for JSON responses."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


@transaction.atomic
def apply_move(slugs: set[str], target: str) -> dict:
    """Move every artifact of ``slugs`` into ``target``. Idempotent.

    One transaction: a half-moved narrative is exactly the split state this
    exists to remove.
    """
    plan = plan_move(slugs, target)

    for r in plan["_reviews"]:
        if str(r.workspace_id) != target:
            r.workspace_id = target
            r.save(update_fields=["workspace"])
    for w in plan["_walkthroughs"]:
        if str(w.workspace_id) != target:
            w.workspace_id = target
            w.save(update_fields=["workspace"])
    for b in plan["_boards"]:
        if str(b.workspace_id) != target:
            b.workspace_id = target
            b.save(update_fields=["workspace", "updated_at"])

    return public_plan(plan)
