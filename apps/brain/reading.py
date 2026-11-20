"""What a brain run reads (`brain_read`): the sources `eligible_sources` allows, scrubbed, once each.

A run walks its feature's eligible sources in order, a few at a time (the caller passes `after`), leaving out what a
clean run of the same feature already read for the same subject (unless a removal asked for it to be derived again).
For each it gets the stored messages (user and assistant text only, harness noise dropped, secrets scrubbed by
`common.redact`), never a tool's raw output, never a sensitive or a contact's conversation (those are not eligible).
For a feature that learns ABOUT a person, only that person's own lines come, with the assistant's replies to them:
other people's words in the same session, and the replies to them, are left out, and a line with no recorded
author is the subject's only when nobody else is in the session. One call serves at most `MAX_CHARS`, and a source
is never split across calls. Every source served is recorded (`BrainRunRead`, and a `brain.read` event) and the
directives of the people in it travel with it; the sources removed since the feature learned from them for this
subject are named (`revoked`). Nothing is read while `BRAIN_READS_ENABLED` is off."""
from __future__ import annotations

from canopy_transcript import is_system_noise
from django.db import transaction
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.utils import timezone

from apps.canopy_sessions.models import Message
from apps.common import redact

from . import readers
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

#: Sources one `brain_read` serves at most, messages per source, characters per message and per call.
MAX_SOURCES, MAX_MESSAGES, MAX_MESSAGE_CHARS, MAX_CHARS = 5, 400, 6000, 150_000
#: The purpose of a run that decides which project sessions belong to (layer 3 of `associate`): it is served
#: the title and the person's opening lines only, and the project's outcome and open tasks beside them.
JUDGE = "judge"
JUDGE_LINES, JUDGE_LINE_CHARS = 6, 600


class NotReadingError(RuntimeError):
    """This run may read nothing now: the rollout gate is off, its feature is disabled or it is not open."""


def candidates(run: BrainRun, after: int = 0):
    """The sources this run may read, in order, past `after` (the last `source` id it was served)."""
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
    return qs.filter(pk__gt=max(after, run.floor)).exclude(learned(run)).order_by("pk")


def learned(run: BrainRun) -> Q:
    """The sources a CLEAN run of this feature already read for this subject, less those a removal asked to be
    derived again (the remaining sources of a `stale` output with an open `RederiveRequest`)."""
    done = BrainRunRead.objects.filter(run__capability_id=run.capability_id, run__subject_type=run.subject_type,
                                       run__subject=run.subject, run__state=BrainRun.DONE).values("source_id")
    redo_outputs = (RederiveRequest.objects.filter(done_at__isnull=True, output__capability_id=run.capability_id,
                                                   output__subject_type=run.subject_type, output__subject=run.subject)
                    .annotate(key=Cast("output_id", CharField())).values("key"))
    redo = OutputLineage.objects.filter(output_kind="brain.output", output_id__in=redo_outputs).values("source_id")
    return Q(pk__in=done) & ~Q(pk__in=redo)


def _is_subjects(message, subject_id: int, alone: bool) -> bool:
    """Whether a USER row is the subject's own line: authored by them, or (no author recorded, a line typed
    straight into the CLI) only when the subject is the one person in the session (`alone`). An unknown author
    in a session others are in is nobody's, and is left out."""
    author = message.author
    if isinstance(author, dict) and author.get("user_id") is not None:
        return author["user_id"] == subject_id
    return alone


def _alone(source: BrainSource, subject_id: int) -> bool:
    """Is the subject the only person (user or contact) in the source?"""
    people = list(source.participants.values_list("user_id", "contact_id"))
    return bool(people) and all(u == subject_id and c is None for u, c in people)


def _judge_messages(session) -> tuple[list[dict], bool]:
    """The first lines a person typed, short: enough to say what a session was about, not what it said."""
    rows = (Message.objects.filter(session=session, role=Message.USER).order_by("turn_index")
            .values_list("turn_index", "plaintext"))
    out = []
    for index, text in rows.iterator(chunk_size=50):
        text = (text or "").strip()
        if not text or is_system_noise(text):
            continue
        out.append({"index": index, "role": Message.USER, "text": redact.redact_text(text)[0][:JUDGE_LINE_CHARS]})
        if len(out) >= JUDGE_LINES:
            break
    return out, False


def _project_context(run: BrainRun) -> dict:
    """The project a judge rules for: what done looks like and what is still open. Its own words, not a session's."""
    from apps.agents.models import AgentProject, AgentTask  # noqa: PLC0415 — as the other lazy imports here

    project = AgentProject.objects.filter(pk=run.subject).first()
    if project is None:
        return {}
    open_tasks = list(project.tasks.filter(status__in=AgentTask.LIVE_STATUSES)
                      .order_by("-updated_at").values_list("title", flat=True)[:15])
    return {"handle": project.handle, "name": project.name,
            "outcome": redact.redact_text(project.outcome or "")[0][:1500],
            "open_tasks": [redact.redact_text(t or "")[0][:200] for t in open_tasks]}


def _messages(run: BrainRun, source: BrainSource, session) -> tuple[list[dict], bool]:
    if run.purpose == JUDGE:
        return _judge_messages(session)
    about_person = run.capability.purpose_class in ABOUT_A_PERSON
    subject_id = int(run.subject) if about_person else None
    alone = _alone(source, subject_id) if about_person else False
    out, chars, truncated = [], 0, False
    replying_to_subject = False
    rows = Message.objects.filter(session=session, role__in=[Message.USER, Message.ASSISTANT]).order_by("turn_index")
    for m in rows.iterator(chunk_size=200):
        text = (m.plaintext or "").strip()
        if not text or (m.role == Message.USER and is_system_noise(text)):
            continue
        if about_person:
            if m.role == Message.USER:
                # The replies that follow are to whoever wrote this line; only the subject's own are kept.
                replying_to_subject = _is_subjects(m, subject_id, alone)
            if not replying_to_subject:
                continue
        text = redact.redact_text(text)[0]
        if len(text) > MAX_MESSAGE_CHARS:
            text, truncated = text[:MAX_MESSAGE_CHARS] + " …", True
        if len(out) >= MAX_MESSAGES or chars + len(text) > MAX_CHARS:
            return out, True
        row = {"index": m.turn_index, "role": m.role, "text": text}
        author = m.author
        if not about_person and m.role == Message.USER and isinstance(author, dict) and author.get("user_id"):
            row["author_user_id"] = author["user_id"]
        out.append(row)
        chars += len(text)
    return out, truncated


def _directives(source: BrainSource, cls: str) -> list[str]:
    users = list(source.participants.filter(user__isnull=False).values_list("user_id", flat=True))
    rows = LearningConsent.objects.filter(user_id__in=users, revoked_at__isnull=True)
    return sorted({d for c in rows if cls in (c.classes or []) for d in (c.directives or [])})


def read_next(run: BrainRun, *, after: int = 0, limit: int = MAX_SOURCES, turn_id: str = "") -> dict:
    """Serve the sources past `after` and record them. `next_after` is what to ask for next; `done` says nothing
    is left past it. The caller carries the position: reading a source twice discloses nothing new."""
    if not readers.enabled() or not run.capability.enabled or run.state != BrainRun.OPEN:
        raise NotReadingError("this run may not read")
    limit = max(1, min(int(limit), MAX_SOURCES))
    page = list(candidates(run, after).select_related("session")[:limit])
    served, used = [], 0
    with transaction.atomic():
        for i, source in enumerate(page):
            messages, truncated = _messages(run, source, source.session)
            size = sum(len(m["text"]) for m in messages)
            if served and used + size > MAX_CHARS:
                page = page[:i]                         # the call is full: this one and the rest come next call
                break
            used += size
            BrainRunRead.objects.get_or_create(run=run, source=source)
            readers.record_read(reader=None, capability=run.capability.slug, purpose=run.purpose,
                                session=source.session, sources=[source], turn_id=turn_id)
            served.append({"source": source.pk, "kind": source.kind, "workspace": source.workspace_id,
                           "title": redact.redact_text(source.session.title or "")[0],
                           "started_at": source.session.created_at, "messages": messages, "truncated": truncated,
                           "directives": _directives(source, run.capability.purpose_class)})
        revoked = _deliver_revocations(run)
    last = page[-1].pk if page else after
    out = {"run": run.pk, "learner": run.capability.slug, "purpose": run.purpose,
           "subject_type": run.subject_type, "subject": run.subject, "sources": served,
           "next_after": last, "done": not candidates(run, last).exists(), "revoked": revoked}
    if run.purpose == JUDGE and run.subject_type == "project":
        out["project"] = _project_context(run)
    return out


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
