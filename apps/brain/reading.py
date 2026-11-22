"""What a brain run reads (`brain_read`): the sources `eligible_sources` allows, scrubbed, once each.

A run walks its feature's eligible sources most recently active first (`activity`: the session's last activity, the
rule its list shows), a few at a time, leaving out what it already read and what a clean run of the same feature read
for the same subject (unless a removal asked for it to be derived again, or the session grew by `REREAD_ROWS` rows
since: then it is due again and served from where that read stopped). A target's run reads only sessions active in its
window (`budget.lookback_days`): an older one is never read, and nothing walks back through a backlog.
Each source comes through the core read (`transcript_read.read`) in the feature's `scope` (the whole session unless
narrowed: every row, tool calls and results included), secrets scrubbed by `common.redact`, never a sensitive or a
contact's conversation (those are not eligible). A session shared by several people comes whole: consent decides
WHICH sessions a learner reads, never which lines of one. One call serves at most `MAX_CHARS` and never splits a
source that fits in a call; one larger than a call pages across calls (`next_index`), never splitting a row. Every
source served is recorded (`BrainRunRead`, and a `brain.read` event) and the directives of the people in it travel
with it; the sources removed since the feature learned from them for this subject are named (`revoked`). Nothing is
read in a workspace whose "Learning reads" is off (`eligibility.explain`). A run reads at most its target's budget of
sessions (`budget`): past it, nothing more is served and the answer says so (`done`, `budget.stopped`)."""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import CharField, Exists, F, Max, OuterRef, Q, Subquery
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.canopy_sessions import transcript_read
from apps.canopy_sessions.models import Message
from apps.common import redact
from apps.workspaces import services as wsvc

from . import budget, learning_reads, readers
from .eligibility import eligible_sources
from .models import (
    ABOUT_A_PERSON,
    BrainRun,
    BrainRunRead,
    BrainSource,
    LearningConsent,
    OutputLineage,
    RederiveRequest,
    RevocationNotice,
    SourceLink,
)

#: Sources one `brain_read` serves at most, and characters per call.
MAX_SOURCES, MAX_CHARS = 5, 150_000
#: The purpose of a run that decides which project sessions belong to (layer 3 of `associate`): it is served
#: the title and the person's opening lines only (the core read's `opening` view), and the project's outcome and
#: open tasks beside them.
JUDGE = "judge"
JUDGE_LINES, JUDGE_LINE_CHARS = transcript_read.OPENING_LINES, transcript_read.OPENING_LINE_CHARS
#: How many rows a session must have grown past where a clean read of it stopped before it is due again (a few turns
#: of work): a session that keeps going is learned from again, from there on, and one that ticked is not re-read.
REREAD_ROWS = 50


class NotReadingError(RuntimeError):
    """This run may read nothing now: "Learning reads" is off wherever it reads, its feature is disabled or it is not open."""


def activity():
    """A source's session's last activity, in SQL: `transcript_read.with_activity`'s rule (the runner's last interaction,
    else the newest row, else its creation). Each part is one probe on a unique index (the binding's session, a
    message's (session, turn_index)), never a scan of the messages."""
    newest = Message.objects.filter(session=OuterRef("session_id")).order_by("-turn_index").values("created_at")[:1]
    return Coalesce("session__runner_binding__last_interacted_at", Subquery(newest), "session__created_at")


def _last_row():
    return Subquery(Message.objects.filter(session=OuterRef("session_id")).order_by("-turn_index")
                    .values("turn_index")[:1])


def window_start(run: BrainRun, now=None):
    """The oldest last activity a run reads (None: no window, for a run with no target)."""
    days = budget.lookback_days(run.target) if run.target_id else None
    return (now or timezone.now()) - dt.timedelta(days=days) if days else None


def eligible_for(run: BrainRun):
    """The eligible sources of this run's feature, subject and scope (no window, nothing left out for having been read)."""
    cap = run.capability
    subject = int(run.subject) if cap.purpose_class in ABOUT_A_PERSON else None
    qs = eligible_sources(cap, run.purpose, subject)
    if run.subject_type == "project" and run.purpose == JUDGE:
        # What the judge rules on: sessions only weakly tied to the project, never one already judged or
        # declared (a link above `signals` is settled).
        qs = qs.filter(links__subject_type=SourceLink.PROJECT_SUBJECT, links__subject=run.subject,
                       links__tier=SourceLink.CANDIDATE, links__how=SourceLink.SIGNALS)
    elif run.subject_type == "project":
        qs = qs.filter(links__subject_type=SourceLink.PROJECT_SUBJECT, links__subject=run.subject,
                       links__tier=SourceLink.CORE)
    if run.subject_type == "project":
        qs = qs.filter(session__workspace__in=AgentProject.objects.filter(pk=run.subject).values("workspace_id"))
    if run.scope:
        qs = qs.filter(session__workspace_id__in=subtree(run.scope))
    return qs


def candidates(run: BrainRun, after: int = 0):
    """The sources this run may read now, most recently active first: eligible, active in its window, not yet read by
    this run, and not learned (`learned`). Annotated `_active` (the last activity) and `_resume` (the row a re-read
    starts after, or None to read it whole). `after` is kept for callers; the order no longer depends on it."""
    qs = eligible_for(run).annotate(_active=activity())
    since = window_start(run)
    if since is not None:
        qs = qs.filter(_active__gte=since)
    if run.pk is not None:
        qs = qs.exclude(pk__in=BrainRunRead.objects.filter(run_id=run.pk).values("source_id"))
    done, redo = _clean_reads(run), _redo(run)
    qs = qs.annotate(_read=Exists(done.filter(source_id=OuterRef("pk"))),
                     _unknown=Exists(done.filter(source_id=OuterRef("pk"), through__isnull=True)),
                     _through=Subquery(done.filter(source_id=OuterRef("pk")).values("source_id")
                                       .annotate(m=Max("through")).values("m")[:1]),
                     _redo=Q(pk__in=redo))
    due = Q(_read=False) | Q(_redo=True)
    if run.purpose != JUDGE:
        # A session that grew well past where its last clean read stopped is due again, from there.
        qs = qs.annotate(_rows=_last_row())
        due |= Q(_unknown=False, _through__isnull=False, _rows__gte=F("_through") + REREAD_ROWS)
    return qs.filter(due).order_by(F("_active").desc(), "-pk")


def resume_after(source) -> int | None:
    """Where a served candidate's read starts: after the row its last clean read reached, for a session due again for
    growing; None (from the start) for one never read or asked to be derived again."""
    if getattr(source, "_read", False) and not getattr(source, "_redo", False):
        return getattr(source, "_through", None)
    return None


def subtree(slug: str) -> set[str]:
    """A run's scope: its workspace and every workspace below it (`workspaces.services.descendant_slugs`)."""
    return {slug} | wsvc.descendant_slugs({slug})


def _clean_reads(run: BrainRun):
    return BrainRunRead.objects.filter(run__capability_id=run.capability_id, run__subject_type=run.subject_type,
                                       run__subject=run.subject, run__state=BrainRun.DONE)


def _redo(run: BrainRun):
    """The sources a removal asked to be derived again: the remaining sources of a `stale` output with an open
    `RederiveRequest`."""
    redo_outputs = (RederiveRequest.objects.filter(done_at__isnull=True, output__capability_id=run.capability_id,
                                                   output__subject_type=run.subject_type, output__subject=run.subject)
                    .annotate(key=Cast("output_id", CharField())).values("key"))
    return OutputLineage.objects.filter(output_kind="brain.output", output_id__in=redo_outputs).values("source_id")


def learned(run: BrainRun) -> Q:
    """The sources a CLEAN run of this feature already read for this subject, less those a removal asked to be
    derived again. (`candidates` also lets a session that grew since back in.)"""
    return Q(pk__in=_clean_reads(run).values("source_id")) & ~Q(pk__in=_redo(run))


def _project_context(run: BrainRun) -> dict:
    """The project a judge rules for: what done looks like and what is still open. Its own words, not a session's."""
    from apps.agents.models import AgentTask  # noqa: PLC0415 — as the other lazy imports here

    project = AgentProject.objects.filter(pk=run.subject).first()
    if project is None:
        return {}
    open_tasks = list(project.tasks.filter(status__in=AgentTask.LIVE_STATUSES)
                      .order_by("-updated_at").values_list("title", flat=True)[:15])
    return {"handle": project.handle, "name": project.name,
            "outcome": redact.redact_text(project.outcome or "")[0][:1500],
            "open_tasks": [redact.redact_text(t or "")[0][:200] for t in open_tasks]}


def view_of(run: BrainRun) -> str:
    """The view this run reads a source in: its feature's scope, and never wider than `opening` for a judge."""
    scope = run.capability.scope or transcript_read.FULL
    return transcript_read.narrower(scope, transcript_read.OPENING) if run.purpose == JUDGE else scope


def _directives(source: BrainSource, cls: str) -> list[str]:
    users = list(source.participants.filter(user__isnull=False).values_list("user_id", flat=True))
    rows = LearningConsent.objects.filter(user_id__in=users, revoked_at__isnull=True)
    return sorted({d for c in rows if cls in (c.classes or []) for d in (c.directives or [])})


def read_next(run: BrainRun, *, after: int = 0, limit: int = MAX_SOURCES, turn_id: str = "",
              index: int | None = None) -> dict:
    """Serve the next sources due (most recently active first) and record them, with how far each was read
    (`BrainRunRead.through`). `next_after` is what to pass back; `done` says nothing is left. A source larger than a whole call is served in parts: its last part's `next_index` (and the
    response's) is what to pass as `index`, with `next_after`, to read on in it. The caller carries the position:
    reading a source twice discloses nothing new."""
    if (learning_reads.run_refusal(run.capability, run.subject, run.scope) or not run.capability.enabled
            or run.state != BrainRun.OPEN):
        raise NotReadingError("this run may not read")
    limit = max(1, min(int(limit), MAX_SOURCES))
    left = budget.run_left(run)
    if index is not None:
        limit = 1                                       # the rest of a source already served (and counted)
    elif left["sources"] <= 0:
        return _stopped(run, after, left)
    else:
        limit = min(limit, left["sources"])
    view = view_of(run)
    if index is not None:
        # The rest of the source the last call left part-way: the one right past `after`, read by this run already.
        page = list(eligible_for(run).filter(pk=after + 1, pk__in=BrainRunRead.objects.filter(run_id=run.pk)
                                             .values("source_id")).select_related("session")[:1])
    else:
        page = list(candidates(run).select_related("session")[:limit])
    served, used, next_index = [], 0, None
    with transaction.atomic():
        for i, source in enumerate(page):
            start = index if index is not None else resume_after(source)
            room = MAX_CHARS - used
            got = transcript_read.read(source.session, view=view, scrub=True, after=start, max_chars=room)
            if served and (got.truncated or got.chars > room):
                page = page[:i]                         # the call is full: this one and the rest come next call
                break
            used += got.chars
            through = got.next_after if got.truncated else (got.messages[-1]["index"] if got.messages
                                                                    else start if start is not None else -1)
            row, _ = BrainRunRead.objects.get_or_create(run=run, source=source)
            if through is not None and (row.through is None or through > row.through):
                BrainRunRead.objects.filter(pk=row.pk).update(through=through)
            readers.record_read(reader=None, capability=run.capability.slug, purpose=run.purpose,
                                session=source.session, sources=[source], turn_id=turn_id)
            served.append({"source": source.pk, "kind": source.kind, "workspace": source.workspace_id,
                           "title": redact.redact_text(source.session.title or "")[0],
                           "started_at": source.session.created_at, "messages": got.messages,
                           "truncated": got.truncated, "next_index": got.next_after,
                           "read_before": start if index is None else None,
                           "directives": _directives(source, run.capability.purpose_class)})
            if got.truncated:                           # larger than a call: the rest of it comes next call
                next_index = got.next_after
                page = page[:i + 1]
                break
        revoked = _deliver_revocations(run)
    if next_index is not None:
        last = page[-1].pk - 1                          # read on in the same source
    else:
        last = page[-1].pk if page else after
    left = budget.run_left(run)
    stopped = "sources" if left["sources"] <= 0 and next_index is None else ""
    out = {"run": run.pk, "learner": run.capability.slug, "purpose": run.purpose,
           "subject_type": run.subject_type, "subject": run.subject, "sources": served, "view": view,
           "next_after": last, "next_index": next_index,
           "done": bool(stopped) or (next_index is None and not candidates(run).exists()), "revoked": revoked,
           "budget": _budget(run, left, stopped)}
    if run.purpose == JUDGE and run.subject_type == "project":
        out["project"] = _project_context(run)
    return out


def _budget(run: BrainRun, left: dict, stopped: str) -> dict:
    lim = budget.limits(run.target)
    return {"max_sources_per_run": lim.max_sources_per_run, "max_outputs_per_run": lim.max_outputs_per_run,
            "sources_left": left["sources"], "outputs_left": left["outputs"], "stopped": stopped}


def _stopped(run: BrainRun, after: int, left: dict) -> dict:
    """The run's budget of sessions is spent: nothing more is served, and it is told so (`done`, `budget.stopped`)."""
    with transaction.atomic():
        revoked = _deliver_revocations(run)
    return {"run": run.pk, "learner": run.capability.slug, "purpose": run.purpose, "subject_type": run.subject_type,
            "subject": run.subject, "sources": [], "view": view_of(run), "next_after": after, "next_index": None,
            "done": True, "revoked": revoked, "budget": _budget(run, left, "sources")}


def _deliver_revocations(run: BrainRun) -> list[int]:
    """The sources removed since this feature learned from them for this run's subject, told once: what it derived
    from them is gone (the cascade withdrew it) and is not to be relied on or rebuilt from memory. Ids only, and
    never a source another subject's runs read."""
    ours = BrainRunRead.objects.filter(run__capability_id=run.capability_id, run__subject_type=run.subject_type,
                                       run__subject=run.subject).values("source_id")
    rows = list(RevocationNotice.objects.filter(capability_slug=run.capability.slug, delivered_at__isnull=True,
                                                source_id__in=ours)
                .order_by("pk").values_list("pk", "source_id")[:200])
    if rows:
        RevocationNotice.objects.filter(pk__in=[pk for pk, _ in rows]).update(delivered_at=timezone.now())
    return sorted({sid for _, sid in rows})
