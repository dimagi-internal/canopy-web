"""Give every pending narrative review a storyboard, and its gate a row there.

    manage.py backfill_review_storyboards [--workspace connect] [--dry-run]

One board per NARRATIVE, not per review: a narrative already on a board keeps
that board (no second one is made); otherwise a one-narrative board is created
(``review_gate.board_for``). The gate rows then follow the narrative's newest
pending review (``review_gate.sync_gate_rows``). Idempotent: a second run
creates nothing. Never resolves a review and never touches a feedback round.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.reviews.models import ReviewRequest
from apps.storyboards import review_gate


class Command(BaseCommand):
    help = "Create storyboards (and gate rows) for pending narrative reviews."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default="connect")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, workspace: str, dry_run: bool, **opts):
        pending = (
            ReviewRequest.objects.filter(workspace_id=workspace, status=ReviewRequest.STATUS_PENDING)
            .order_by("-version", "-created_at")
        )
        newest: dict[str, ReviewRequest] = {}
        for review in pending:
            slug = review_gate.narrative_of(review)
            if slug and slug not in newest:
                newest[slug] = review
        created = reused = 0
        for slug, review in sorted(newest.items()):
            board = review_gate.existing_board(workspace, slug)
            if dry_run:
                where = f"reuse {board.slug}" if board else "create a board"
                self.stdout.write(f"{slug}: {where} (review {review.pk})")
                continue
            board, made = review_gate.board_for(review)
            if board is None:
                self.stdout.write(f"{slug}: skipped (no board)")
                continue
            rows = review_gate.sync_gate_rows(board, slug)
            created += made
            reused += not made
            verb = "created" if made else "reused"
            self.stdout.write(f"{slug}: {verb} {board.slug} — {len(rows)} gate row(s) for review {review.pk}")
        self.stdout.write(f"done: {created} created, {reused} reused, {len(newest)} narratives")
