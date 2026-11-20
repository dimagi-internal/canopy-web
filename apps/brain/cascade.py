"""A removal reaches everything derived from it.

What was learned is written down with its sources (`OutputLineage`), so when a source goes, the sweep
(`sweep`, a tick job and also run after the act that caused it) finds what came from it:

* an output derived ONLY from removed sources is **withdrawn**: its words are emptied, its lineage kept as
  ids, and it is seen by no one (`outputs.visible_to`);
* one derived from several, some removed, is **stale**: its words are emptied too and nobody sees it, and
  it asks to be derived again from the rest (`RederiveRequest`, which the feature's next run takes);
* the learner is given a `RevocationNotice` (served by its next `brain_read`);
* the evidence on every link of a source removed from shared use is deleted, whether an output cites it or not;
* the conversation of every run that read a removed source is deleted (`content_deletion.delete_content`):
  it holds what the run was served and what it emitted, and outputs are the durable record of a run;
* a personal-memory (HCP) entry derived from a removed session (`EntryRelationship` `derivedFrom`) is
  deleted through HCP's own path, which clears its embedding (`hcp_store`).

A source is removed for an output when its words are gone (deleted, withdrawn, its session gone), when it is
sealed, when the person the output is about took it out, or, for a shared use (`project`, `system`), when ANYONE
ever took it out (as `eligibility.eligible` and `emit_allowed`: whoever they were, still in it or since taken out
of it), or when the person the output is about, or for a shared output any person in the source when it was made,
stopped agreeing to the class or narrowed their consent. About one person: removing X withdraws what was learned
about X; what was learned about Y from Y's own words stays.

Who was in a source "when it was made": an output records it (`OutputLineage.participant_scope`). A run's reads and
a link's evidence do not, so they are judged against who is in it now, less anyone who never agreed to the class:
every use asked `eligible`, which needed everyone's consent, so such a person joined after it. Their joining closes
every later read; it destroys nothing already made. (Someone who once agreed and stopped counts, whenever they
joined: that fails safe.)

It is a tombstone, not a toggle: a withdrawn output stays withdrawn when the person lets the session back in
(`consent.include`), when a replay emits it again (`emit_allowed`) or when a runner ships the rows again. Other
stores that keep `OutputLineage` register with `register_store` and are asked to withdraw or mark stale the same
way. The sweep is idempotent and skips what is already withdrawn."""
from __future__ import annotations

import logging
from collections import defaultdict

from django.db import IntegrityError, transaction
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.utils import timezone

from . import participants
from .eligibility import covers, rule_denies
from .models import (
    ABOUT_A_PERSON,
    PROJECT,
    SHARED,
    BrainOutput,
    BrainRun,
    BrainRunRead,
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
#: `kind` -> `fn(source_ids)`: a store keyed by session, with no `OutputLineage` (a preview snapshot, which may name
#: sessions no source was made for). Each sweep calls it once, narrowed like the sweep; it judges its own sessions.
_SESSION_STORES: dict = {}


def register_store(kind: str, fn, *, by_session: bool = False) -> None:
    """Ask `fn(output_id, mode, reason)` to withdraw (`WITHDRAW`) or mark (`STALE`) what `kind` holds. With
    `by_session`, `fn(source_ids)` is called once per sweep instead (`source_ids` None: look at everything)."""
    (_SESSION_STORES if by_session else _STORES)[kind] = fn


def stores() -> dict:
    return {**_STORES, **_SESSION_STORES}


def removed_reason(source: BrainSource | None, output_class: str, subject_user_id, scope, *,
                   capability: str = "", purpose: str = "", scope_is_now: bool = False, memo: dict | None = None) -> str:
    """Why `source` is gone for an output of this class, or "". `scope` is the people whose words were in it
    when the output was made; `scope_is_now` says it is who is in it NOW (a run's read, a link's evidence), so a
    person who never agreed to the class is left out (they joined after: see the module docstring). Any
    exclusion of the source removes it from a shared use, whoever made it. Besides a removal or a dropped class,
    a person NARROWING their consent removes it: its workspace no longer covered (`eligibility.covers`) or a
    standing rule now denying the use (`eligibility.rule_denies`, matched on `capability` and `purpose` when the
    caller knows them; a rule naming a learner or purpose the caller does not know is not taken to match)."""
    if source is None or source.state == BrainSource.WITHDRAWN:
        return "withdrawn"
    if participants.sealed_reason(source.session):
        return "sealed"
    if output_class in SHARED and SourceExclusion.objects.filter(source=source).exists():
        return "excluded"                        # anyone's, permanently, as `eligible` and `emit_allowed`
    if output_class in ABOUT_A_PERSON:
        people = [subject_user_id] if subject_user_id is not None else []
    else:
        people = [u for u in scope if u is not None]
    if not people:
        return ""
    if SourceExclusion.objects.filter(source=source, user_id__in=people).exists():
        return "excluded"
    if scope_is_now:
        memo = {} if memo is None else memo
        people = [u for u in people if ever_agreed(u, output_class, memo)]
        if not people:
            return ""
    agreed = {c.user_id: c for c in LearningConsent.objects.filter(user_id__in=people, revoked_at__isnull=True)}
    for uid in people:
        c = agreed.get(uid)
        if c is None or output_class not in (c.classes or []):
            return "consent_ended"
    for uid in people:
        c = agreed[uid]
        if not covers(c, source.workspace_id):
            return "outside_workspaces"
        if any(rule_denies(r, capability=capability, purpose=purpose, cls=output_class, workspace=source.workspace_id)
               for r in (c.standing_rules or []) if _readable_rule(r, capability, purpose)):
            return "standing_rule"
    return ""


def ever_agreed(user_id, cls: str, memo: dict | None = None) -> bool:
    """Did this person ever agree to `cls` (now, or before a class removal or a revoke)? Their consent rows (a
    revoked one keeps its classes) and, for a class since dropped, their `brain.consent` events. `memo` holds the
    answers for one sweep, so a person is looked up once per class however many rows name them."""
    if user_id is None:
        return False
    memo = {} if memo is None else memo
    key = (user_id, cls)
    if key not in memo:
        from apps.events.models import Event  # noqa: PLC0415 — as the other lazy imports here

        from . import consent  # noqa: PLC0415 — consent imports this module

        held = LearningConsent.objects.filter(user_id=user_id).values_list("classes", flat=True)
        found = any(cls in (c or []) for c in held)
        if not found:
            rows = Event.objects.filter(source=consent.SOURCE, payload__user_id=user_id,
                                        kind__in=[consent.CONSENTED, consent.CLASS_ADDED, consent.CLASS_REMOVED])
            found = any(cls in (p.get("classes") or []) or p.get("class") == cls
                        for p in rows.values_list("payload", flat=True))
        memo[key] = found
    return memo[key]


def _readable_rule(rule, capability: str, purpose: str) -> bool:
    """A rule naming a learner or purpose is judged only when the caller knows it. An unreadable rule (not a
    dict) is passed on, and `rule_denies` denies it."""
    if not isinstance(rule, dict):
        return True
    return not ((rule.get("capability") and not capability) or (rule.get("purpose") and not purpose))


def _scrub_links(source: BrainSource) -> None:
    SourceLink.objects.filter(source=source).exclude(evidence=[]).update(evidence=[])


def _people(source: BrainSource) -> list[int]:
    return list(SourceParticipant.objects.filter(source=source, user__isnull=False).values_list("user_id", flat=True))


def scrub_link_evidence(source_ids=None, memo: dict | None = None) -> int:
    """A link's evidence is the judge's quote of a session's lines, a shared (project) use of it: it goes as soon
    as the source is removed from that use, whether or not any output cites the source. Who is in it now is the
    scope, less anyone who joined since (`removed_reason`)."""
    memo = {} if memo is None else memo
    links = SourceLink.objects.exclude(evidence=[])
    if source_ids is not None:
        links = links.filter(source_id__in=list(source_ids))
    ids = set(links.values_list("source_id", flat=True))
    gone = {src.pk for src in BrainSource.objects.filter(pk__in=ids).select_related("session")
            if removed_reason(src, PROJECT, None, _people(src), scope_is_now=True, memo=memo)}
    return SourceLink.objects.filter(source_id__in=gone).exclude(evidence=[]).update(evidence=[]) if gone else 0


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
    if out.state == BrainOutput.STALE:
        return "unchanged"
    # Its words came partly from what was removed and cannot be told apart: they go, the ids stay.
    BrainOutput.objects.filter(pk=out.pk).update(state=BrainOutput.STALE, body={}, title="")
    try:
        with transaction.atomic():
            RederiveRequest.objects.get_or_create(output=out, done_at=None,
                                                  defaults={"reason": "a source was removed"})
    except IntegrityError:
        pass                                      # another sweep asked first: one open request is the point
    return "stale"


def sweep(source_ids=None) -> dict:
    """One pass. `source_ids` narrows it to outputs citing those sources (the prompt run after an act); none
    looks at everything not yet withdrawn (the tick). Returns counts."""
    done = {"withdrawn": 0, "stale": 0, "unchanged": 0, "sources": withdraw_sources_whose_words_are_gone()}
    live = (BrainOutput.objects.exclude(state=BrainOutput.WITHDRAWN).annotate(key=Cast("pk", CharField()))
            .values("key"))
    lineage = OutputLineage.objects.filter(output_kind=OUTPUT_KIND, output_id__in=live)
    if source_ids is not None:
        affected = set(lineage.filter(source_id__in=list(source_ids)).values_list("output_id", flat=True))
        lineage = lineage.filter(output_id__in=affected)
    by_output = defaultdict(list)
    for row in lineage.select_related("source__session"):
        by_output[row.output_id].append(row)
    outputs = {str(o.pk): o for o in BrainOutput.objects.filter(pk__in=[int(i) for i in by_output])
               .exclude(state=BrainOutput.WITHDRAWN).select_related("capability", "run")}
    seen: dict = {}
    memo: dict = {}                                # who ever agreed to what, once per sweep
    for oid, rows in by_output.items():
        out = outputs.get(oid)
        if out is None:
            continue
        subject = int(out.subject) if out.subject_type == "person" and out.subject.isdecimal() else None
        gone = []
        for r in rows:
            key = (r.source_id, out.capability_id, out.run.purpose if out.run_id else "", subject,
                   tuple(r.participant_scope or []))
            if key not in seen:
                seen[key] = removed_reason(r.source, out.capability.purpose_class, subject, r.participant_scope or [],
                                           capability=out.capability.slug,
                                           purpose=out.run.purpose if out.run_id else "")
            if seen[key]:
                gone.append((r.source, seen[key]))
        if gone:
            done[_apply_output(out, gone, len(rows))] += 1
    scrub_link_evidence(source_ids, memo)
    done["runs"] = scrub_runs(source_ids, memo)
    for kind, fn in _STORES.items():
        _sweep_store(kind, fn, source_ids)
    for kind, fn in _SESSION_STORES.items():
        try:
            done[kind] = fn(source_ids)
        except Exception:  # noqa: BLE001 — one store must not stop the sweep
            logger.exception("could not sweep %s", kind)
    from . import hcp_store  # noqa: PLC0415 — contacts imports this app; the store loads late

    done["memory_entries"] = hcp_store.sweep(source_ids, memo)
    return done


def scrub_runs(source_ids=None, memo: dict | None = None) -> int:
    """Delete the conversation of every run that read a source now removed for its feature and subject: what
    `brain_read` served and `brain_emit` was sent sit in its messages, events and transcripts, which cannot be
    scrubbed source by source. Who is in the source now is the scope, less anyone who joined since
    (`removed_reason`). Only runs whose conversation is still there are looked at, each distinct (source, feature,
    purpose, subject) once. A run whose turn is still working is left for the next sweep."""
    from apps.canopy_sessions import content_deletion  # noqa: PLC0415 — canopy_sessions imports this app

    memo = {} if memo is None else memo
    reads = BrainRunRead.objects.filter(run__session__isnull=False, run__session__content_deleted_at__isnull=True)
    if source_ids is not None:
        reads = reads.filter(source_id__in=list(source_ids))
    rows = list(reads.values_list("run_id", "source_id", "run__capability_id", "run__capability__purpose_class",
                                  "run__capability__slug", "run__purpose", "run__subject").distinct())
    srcs = {s.pk: s for s in BrainSource.objects.filter(pk__in={r[1] for r in rows}).select_related("session")}
    people: dict[int, list[int]] = {}
    seen: dict = {}
    doomed: set[int] = set()
    for run_id, source_id, cap_id, cls, slug, purpose, run_subject in rows:
        if run_id in doomed:
            continue
        subject = int(run_subject) if cls in ABOUT_A_PERSON and (run_subject or "").isdecimal() else None
        key = (source_id, cap_id, purpose, subject)
        if key not in seen:
            src = srcs.get(source_id)
            if src is not None and source_id not in people:
                people[source_id] = _people(src)
            seen[key] = removed_reason(src, cls, subject, people.get(source_id, []), capability=slug,
                                       purpose=purpose, scope_is_now=True, memo=memo)
        if seen[key]:
            doomed.add(run_id)
    n = 0
    for run in BrainRun.objects.filter(pk__in=doomed).select_related("session"):
        try:
            content_deletion.delete_content(run.session, by=None)
            n += 1
        except (ValueError, content_deletion.BytesRemain):
            logger.info("brain run %s is still working; its conversation is deleted by a later sweep", run.pk)
        except Exception:  # noqa: BLE001 — one run must not stop the sweep; the next one tries again
            logger.exception("could not delete the conversation of brain run %s", run.pk)
    return n


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
