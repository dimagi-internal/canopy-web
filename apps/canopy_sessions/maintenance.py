"""Operations on STORED sessions — auditing and repairing transcript rows after
the fact.

The ingest filters (`persist_transcript_rows`, `post_session_stream`,
`tail_as_messages`) decide what gets in. This module is the other half: what to
do about rows already in the table when a rule changes. Every prefix added to
`SYSTEM_NOISE_PREFIXES` retroactively makes some stored rows wrong, and the only
way to see them was to read the code and hand-write a query.

The FIRST time this came up it was done as a management command, which meant
reaching the data required an `aws ecs run-task` against prod with a command
override — unrepeatable, unauditable, and available only to whoever held AWS
credentials that day. So the logic lives HERE, and the callers are thin:

  * `apps.mcp.tools.sessions`  — over HTTPS, as the authenticated user, scoped to
    that user's workspaces. This is the path agents and humans should use.
  * `purge_transcript_noise`   — the same functions from a shell, for the ops case
    that has to run unscoped across every workspace.

Both go through `audit_noise` / `purge_noise`, so the two surfaces cannot drift
and neither can hand-roll its own idea of what "noise" means.

SCOPING FAILS CLOSED. `workspace_slugs` is required and an empty set matches
NOTHING — an unresolvable user must see zero rows, never every row. Unscoped
access is the explicit `ALL_WORKSPACES` sentinel, which is greppable precisely so
its few call sites stay countable.
"""
from __future__ import annotations

from django.db.models import Q

from .models import Message
from .transcript_noise import SYSTEM_NOISE_PREFIXES

# Unscoped access, spelled out. A sentinel rather than `None`, because `None` is
# what an absent argument or a failed lookup degrades to — and "the scope lookup
# returned nothing" must never be one typo away from "every workspace".
ALL_WORKSPACES = "__all_workspaces__"

# Cap on rows returned as examples, however large `sample` is asked to be. The
# report travels over MCP into a model's context; a thousand-row sample is not a
# report, it's a denial of service against the reader.
SAMPLE_MAX = 50
SAMPLE_TEXT_CHARS = 120


def _scoped(qs, workspace_slugs):
    """`qs` restricted to the caller's workspaces. Empty scope -> nothing."""
    if workspace_slugs is ALL_WORKSPACES:
        return qs
    if not workspace_slugs:
        return qs.none()
    return qs.filter(session__workspace__slug__in=workspace_slugs)


def noise_queryset(*, workspace_slugs, session_id=None):
    """Stored USER rows whose text is harness output.

    `istartswith` mirrors `is_system_noise`: prefix-anchored, so a human quoting a
    marker keeps their message. It cannot mirror that function's `.lstrip()`, so a
    row with leading whitespace is NOT matched here — the ingest filter is the
    authority and this is only catching up history. Deliberately under-matching:
    a missed row is a cosmetic bug, an over-matched one is deleted user speech.
    """
    predicate = Q()
    for prefix in SYSTEM_NOISE_PREFIXES:
        predicate |= Q(plaintext__istartswith=prefix)
    qs = Message.objects.filter(predicate, role=Message.USER)
    if session_id is not None:
        qs = qs.filter(session_id=session_id)
    return _scoped(qs, workspace_slugs)


def _report(qs, sample: int) -> dict:
    """Counts + a bounded sample, the shape both audit and purge return."""
    total = qs.count()
    rows = []
    if total and sample > 0:
        for row in qs.order_by("-created_at").select_related("session")[: min(sample, SAMPLE_MAX)]:
            rows.append({
                "session_id": str(row.session_id),
                "workspace": row.session.workspace_id and row.session.workspace.slug,
                "turn_index": row.turn_index,
                "text": " ".join((row.plaintext or "").split())[:SAMPLE_TEXT_CHARS],
            })
    return {
        "matched": total,
        "sessions": qs.values("session_id").distinct().count() if total else 0,
        "sample": rows,
    }


def audit_noise(*, workspace_slugs, session_id=None, sample: int = 5) -> dict:
    """What harness output is stored as user messages. Read-only.

    Returns {"matched", "sessions", "sample": [...]}. Run this before `purge_noise`
    — "how much of my chat history does this touch" is a question worth answering
    before the delete, not after.
    """
    return _report(noise_queryset(workspace_slugs=workspace_slugs, session_id=session_id), sample)


def purge_noise(*, workspace_slugs, session_id=None, sample: int = 5, apply: bool = False) -> dict:
    """Delete the rows `audit_noise` reports. DRY RUN unless `apply=True`.

    Returns the audit report plus {"applied": bool, "deleted": int}. The sample is
    captured BEFORE the delete, so the result records what went rather than
    describing an empty table afterwards.
    """
    qs = noise_queryset(workspace_slugs=workspace_slugs, session_id=session_id)
    report = _report(qs, sample)
    if not apply:
        return {**report, "applied": False, "deleted": 0}
    # `qs.delete()` on a sliced/ordered queryset is an error, and `_report` only
    # ever slices its own clone, so this deletes exactly what was counted.
    deleted, _ = qs.delete()
    return {**report, "applied": True, "deleted": deleted}
