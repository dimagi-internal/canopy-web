"""A removal reaches everything derived from it.

What was learned is written down with its sources (`OutputLineage`), so when a source goes, the sweep
(`sweep`, a tick job and also run after the act that caused it) finds what came from it:

* an output derived ONLY from removed sources is **withdrawn**: its words are emptied, its lineage kept as
  ids, and it is seen by no one (`outputs.visible_to`);
* one derived from several, some removed, is marked **stale** ("a source was removed") and asks to be
  derived again from the rest (`RederiveRequest`);
* the evidence on the source's links is deleted, and the learner is given a `RevocationNotice`.

A source is removed for an output when its words are gone (deleted, withdrawn, its session gone), or when the
person the output is about, or for a shared output ANY person in the source (`participant_scope`), took it out
or stopped agreeing to the class. About one person: removing X withdraws what was learned about X; what was
learned about Y from Y's own words stays.

It is a tombstone, not a toggle: a withdrawn output stays withdrawn when the person lets the session back in
(`consent.include`), when a replay emits it again (`emit_allowed`) or when a runner ships the rows again. Other
stores (an HCP entry) register with `register_store` and are asked to withdraw or mark stale the same way.
The sweep is idempotent."""
from __future__ import annotations

import logging
from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import participants
from .models import (
    ABOUT_A_PERSON,
    BrainOutput,
    BrainSource,
    LearningConsent,
    OutputLineage,
    RederiveRequest,
    RevocationNotice,
    SourceExclusion,
    SourceLink,
    SourceParticipant,
)

logger = logging.getLogger(__name__)

OUTPUT_KIND = "brain.output"
WITHDRAW, STALE = "withdraw", "stale"

#: `output_kind` -> `fn(output_id, mode, reason)`. Registered by the app that owns the store (an HCP entry:
#: the memory store registers itself beside the person's memory page; nothing here touches it).
_STORES: dict = {}


def register_store(kind: str, fn) -> None:
    """Ask `fn(output_id, mode, reason)` to withdraw (`WITHDRAW`) or mark (`STALE`) what `kind` holds."""
    _STORES[kind] = fn


def stores() -> dict:
    return dict(_STORES)


def removed_reason(source: BrainSource | None, output_class: str, subject_user_id, scope) -> str:
    """Why `source` is gone for an output of this class, or "". `scope` is the people whose words were in it
    when the output was made."""
    if source is None or source.state == BrainSource.WITHDRAWN:
        return "withdrawn"
    if participants.sealed_reason(source.session):
        return "sealed"
    if output_class in ABOUT_A_PERSON:
        people = [subject_user_id] if subject_user_id is not None else []
    else:
        people = [u for u in scope if u is not None]
    if not people:
        return ""
    if SourceExclusion.objects.filter(source=source, user_id__in=people).exists():
        return "excluded"
    agreed = {c.user_id: c for c in LearningConsent.objects.filter(user_id__in=people, revoked_at__isnull=True)}
    for uid in people:
        c = agreed.get(uid)
        if c is None or output_class not in (c.classes or []):
            return "consent_ended"
    return ""


def _scrub_links(source: BrainSource) -> None:
    SourceLink.objects.filter(source=source).exclude(evidence=[]).update(evidence=[])


def _notify(slug: str, source: BrainSource, reason: str) -> None:
    RevocationNotice.objects.get_or_create(capability_slug=slug, source=source, defaults={"reason": reason[:40]})


def emit_allowed(source: BrainSource) -> bool:
    """May a new output cite `source`? Never one that was removed: a replay cannot bring it back."""
    return (source.state == BrainSource.OPEN and not participants.sealed_reason(source.session)
            and not SourceExclusion.objects.filter(source=source).exists())


def withdraw_sources_whose_words_are_gone() -> int:
    """Sources whose words were deleted, or whose session is gone, become `withdrawn` for good."""
    gone = (BrainSource.objects.filter(state=BrainSource.OPEN)
            .filter(Q(session__isnull=True) | Q(session__content_deleted_at__isnull=False)))
    return gone.update(state=BrainSource.WITHDRAWN)


@transaction.atomic
def _apply_output(out: BrainOutput, removed: list, total: int) -> str:
    for source, reason in removed:
        if source is not None:
            _notify(out.capability.slug, source, reason)
            _scrub_links(source)
    if len(removed) >= total:
        BrainOutput.objects.filter(pk=out.pk).update(state=BrainOutput.WITHDRAWN, body={}, title="")
        RederiveRequest.objects.filter(output=out, done_at__isnull=True).update(done_at=timezone.now())
        return "withdrawn"
    if out.state == BrainOutput.ACTIVE:
        BrainOutput.objects.filter(pk=out.pk).update(state=BrainOutput.STALE)
    if not RederiveRequest.objects.filter(output=out, done_at__isnull=True).exists():
        RederiveRequest.objects.create(output=out, reason="a source was removed")
    return "stale"


def sweep(source_ids=None) -> dict:
    """One pass. `source_ids` narrows it to outputs citing those sources (the prompt run after an act); none
    looks at everything not yet withdrawn (the tick). Returns counts."""
    done = {"withdrawn": 0, "stale": 0, "sources": withdraw_sources_whose_words_are_gone()}
    lineage = OutputLineage.objects.filter(output_kind=OUTPUT_KIND)
    if source_ids is not None:
        affected = set(lineage.filter(source_id__in=list(source_ids)).values_list("output_id", flat=True))
        lineage = lineage.filter(output_id__in=affected)
    by_output = defaultdict(list)
    for row in lineage.select_related("source__session"):
        by_output[row.output_id].append(row)
    outputs = {str(o.pk): o for o in BrainOutput.objects.filter(pk__in=[int(i) for i in by_output])
               .exclude(state=BrainOutput.WITHDRAWN).select_related("capability")}
    for oid, rows in by_output.items():
        out = outputs.get(oid)
        if out is None:
            continue
        subject = int(out.subject) if out.subject_type == "person" and out.subject.isdecimal() else None
        gone = []
        for r in rows:
            why = removed_reason(r.source, out.capability.purpose_class, subject, r.participant_scope or [])
            if why:
                gone.append((r.source, why))
        if gone:
            done[_apply_output(out, gone, len(rows))] += 1
    for kind, fn in _STORES.items():
        _sweep_store(kind, fn, source_ids)
    return done


def _sweep_store(kind: str, fn, source_ids) -> None:
    qs = OutputLineage.objects.filter(output_kind=kind)
    if source_ids is not None:
        qs = qs.filter(source_id__in=list(source_ids))
    grouped = defaultdict(list)
    for row in qs.select_related("source__session"):
        grouped[row.output_id].append(row)
    for oid, rows in grouped.items():
        gone = [r for r in rows if removed_reason(r.source, "memory", None, r.participant_scope or []) in
                ("withdrawn", "sealed")]
        if gone:
            try:
                fn(oid, WITHDRAW if len(gone) == len(rows) else STALE, "a source was removed")
            except Exception:  # noqa: BLE001 — one store must not stop the sweep
                logger.exception("could not withdraw %s %s", kind, oid)


def after_commit(source_ids=None) -> None:
    """Run the sweep when the act that removed something commits (prompt), narrowed to its sources."""
    ids = None if source_ids is None else list(source_ids)
    transaction.on_commit(lambda: sweep(ids))


def sources_of(user) -> list[int]:
    """The sources a person is in: what their revoke or class removal can have changed."""
    return list(SourceParticipant.objects.filter(user=user).values_list("source_id", flat=True))
