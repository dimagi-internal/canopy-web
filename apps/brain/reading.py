"""What a brain run reads (`brain_read`): the sources `eligible_sources` allows, scrubbed, once each.

A run walks its feature's eligible sources in order, a few at a time (the caller passes `after`). For each it gets the
stored messages (user and assistant text only, harness noise dropped, secrets scrubbed by `common.redact`), never
a tool's raw output, never a sensitive or a contact's conversation (those are not eligible). For a feature that
learns ABOUT a person, only that person's own lines come with the assistant's replies: other people's words in the
same session are left out. Every source served is recorded (`BrainRunRead`, and a `brain.read` event) and the
directives of the people in it travel with it. Nothing is read while `BRAIN_READS_ENABLED` is off."""
from __future__ import annotations

from canopy_transcript import is_system_noise
from django.db import transaction

from apps.canopy_sessions.models import Message
from apps.common import redact

from . import readers
from .eligibility import eligible_sources
from .models import ABOUT_A_PERSON, BrainRun, BrainRunRead, BrainSource, LearningConsent, SourceLink

#: Sources one `brain_read` serves at most, messages per source, characters per message and per call.
MAX_SOURCES, MAX_MESSAGES, MAX_MESSAGE_CHARS, MAX_CHARS = 5, 400, 6000, 150_000


class NotReadingError(RuntimeError):
    """This run may read nothing now: the rollout gate is off, its feature is disabled or it is not open."""


def candidates(run: BrainRun, after: int = 0):
    """The sources this run may read, in order, past `after` (the last `source` id it was served)."""
    cap = run.capability
    subject = int(run.subject) if cap.purpose_class in ABOUT_A_PERSON else None
    qs = eligible_sources(cap, run.purpose, subject)
    if run.subject_type == "project":
        qs = qs.filter(links__subject_type=SourceLink.PROJECT_SUBJECT, links__subject=run.subject,
                       links__tier=SourceLink.CORE)
    return qs.filter(pk__gt=after).order_by("pk")


def _is_subjects(message, subject_id: int, creator_id) -> bool:
    """Whether a USER row is the subject's own line: authored by them, or (no author recorded, a line typed
    straight into the CLI) the session's creator's, who is the subject only when they created it."""
    author = message.author
    if isinstance(author, dict) and author.get("user_id") is not None:
        return author["user_id"] == subject_id
    return creator_id == subject_id


def _messages(run: BrainRun, source: BrainSource, session) -> tuple[list[dict], bool]:
    about_person = run.capability.purpose_class in ABOUT_A_PERSON
    subject_id = int(run.subject) if about_person else None
    out, chars, truncated = [], 0, False
    rows = Message.objects.filter(session=session, role__in=[Message.USER, Message.ASSISTANT]).order_by("turn_index")
    for m in rows.iterator(chunk_size=200):
        text = (m.plaintext or "").strip()
        if not text or (m.role == Message.USER and is_system_noise(text)):
            continue
        if about_person and m.role == Message.USER and not _is_subjects(m, subject_id, session.created_by_id):
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
    served = []
    with transaction.atomic():
        for source in page:
            messages, truncated = _messages(run, source, source.session)
            BrainRunRead.objects.get_or_create(run=run, source=source)
            readers.record_read(reader=None, capability=run.capability.slug, purpose=run.purpose,
                                session=source.session, sources=[source], turn_id=turn_id)
            served.append({"source": source.pk, "kind": source.kind, "workspace": source.workspace_id,
                           "title": redact.redact_text(source.session.title or "")[0],
                           "started_at": source.session.created_at, "messages": messages, "truncated": truncated,
                           "directives": _directives(source, run.capability.purpose_class)})
    last = page[-1].pk if page else after
    return {"run": run.pk, "learner": run.capability.slug, "purpose": run.purpose,
            "subject_type": run.subject_type, "subject": run.subject, "sources": served,
            "next_after": last, "done": not candidates(run, last).exists()}
