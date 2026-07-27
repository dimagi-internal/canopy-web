"""Request-free service layer, so REST and (later) MCP share one implementation.

Mirrors ``apps/harness/schedule_services.py`` — the pattern that keeps the two
surfaces from drifting.
"""
from __future__ import annotations

from django.db import transaction

from apps.feedback.models import Feedback

_INGEST_FIELDS = (
    "target_kind",
    "target_ref",
    "target_version",
    "anchor_id",
    "kind",
    "body",
    "suggested_text",
    "author_name",
    "author_email",
    "channel",
    "source_ref",
)


def ingest(items: list[dict], *, submitted_by=None) -> dict:
    """Create feedback rows, skipping ones already ingested.

    Items with neither ``body`` nor ``suggested_text`` are skipped and counted
    as ``empty`` — a note with no words is not feedback.

    Idempotent per ``(channel, source_ref)`` so re-reading a mailbox or a doc is
    safe — an agent that re-scans its inbox must not double-file every thread. A
    blank ``source_ref`` never dedupes: a web submit has no natural id.

    The whole batch commits in one transaction, so a doc with forty comments
    lands atomically rather than half-ingesting on an error.
    """
    created_ids: list[int] = []
    duplicate = 0
    empty = 0

    with transaction.atomic():
        for raw in items:
            # A note with no words is not feedback. The UI disables its submit
            # button, but the API accepted it and quietly created a row — an
            # agent ingesting a mailbox could fill the pool with blanks that
            # someone then has to triage.
            if not (raw.get("body") or "").strip() and not (
                raw.get("suggested_text") or ""
            ).strip():
                empty += 1
                continue

            data = {k: raw.get(k) for k in _INGEST_FIELDS if raw.get(k) is not None}
            channel = data.get("channel") or Feedback.CHANNEL_WEB
            source_ref = data.get("source_ref") or ""

            if source_ref and Feedback.objects.filter(
                channel=channel, source_ref=source_ref
            ).exists():
                duplicate += 1
                continue

            fb = Feedback.objects.create(**data, submitted_by=submitted_by)
            created_ids.append(fb.pk)

    return {
        "created": len(created_ids),
        "duplicate": duplicate,
        "empty": empty,
        "ids": created_ids,
    }


def list_feedback(*, target_kind=None, target_ref=None, state=None, channel=None):
    """The pool, filtered. Returns a QuerySet so callers can count or slice."""
    qs = Feedback.objects.all()
    if target_kind:
        qs = qs.filter(target_kind=target_kind)
    if target_ref:
        qs = qs.filter(target_ref=target_ref)
    if state:
        qs = qs.filter(state=state)
    if channel:
        qs = qs.filter(channel=channel)
    return qs


def resolve(pk: int, *, state: str, note: str = "", resolved_in_version=None) -> Feedback:
    """Record what a decision turn did with one piece of feedback.

    This is the ONLY mutation. There is no general PATCH: feedback is what
    somebody said, and editing that after the fact would make the pool
    untrustworthy as a record.
    """
    fb = Feedback.objects.get(pk=pk)
    fb.state = state
    if note:
        fb.disposition_note = note
    if resolved_in_version is not None:
        fb.resolved_in_version = resolved_in_version
    fb.save(update_fields=["state", "disposition_note", "resolved_in_version", "updated_at"])
    return fb
