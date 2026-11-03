"""Each agent mailbox canopy hosts holds at most MAIL_MAILBOX_CAP_BYTES.

A mailbox is one (workspace, address): every MailMessage copy with that `mailbox`, in or
out. Each copy carries `size_bytes`, set when it is stored. One inbound message keeps at most
MAIL_MAX_ATTACHMENT_BYTES of attachment content (`kept_attachments`). After each delivery or send,
a mailbox over its cap evicts its oldest mail until it fits — trash and spam first, then
archived threads, then read, then unread, oldest first within each — never the message just stored. An
evicted message's rows go (and a thread left empty), then its S3 objects once the delete
commits: its attachments, and its raw MIME unless another mailbox's copy still cites it
(one email to two agents is two rows sharing one `kept/` object).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Greatest
from django.utils import timezone

from . import ses
from .models import MailAttachment, MailDraft, MailMessage, MailThread, MailUpload

logger = logging.getLogger(__name__)

_CHUNK = 500  # rows per IN (...) — well under SQLite's variable limit
TRASH_DAYS = 30
UPLOAD_DAYS = 7  # a file picked and never sent, and no draft holds it


def message_size(*, text: str = "", html: str = "", attachment_sizes=(), raw_size: int = 0) -> int:
    return len((text or "").encode()) + len((html or "").encode()) + sum(attachment_sizes) + max(raw_size, 0)


def kept_attachments(sizes: list[int]) -> list[bool]:
    """Which of a message's attachments keep their content: each, in order, while the kept
    total stays within MAIL_MAX_ATTACHMENT_BYTES; one that would pass it is not kept."""
    limit = max(int(settings.MAIL_MAX_ATTACHMENT_BYTES or 0), 0)
    if not limit:
        return [True] * len(sizes)
    out, total = [], 0
    for size in sizes:
        fits = total + size <= limit
        out.append(fits)
        total += size if fits else 0
    return out


def cap_bytes() -> int:
    return max(int(settings.MAIL_MAILBOX_CAP_BYTES or 0), 0)


def used_bytes(workspace_id: int, address: str) -> int:
    return (MailMessage.objects.filter(workspace_id=workspace_id, mailbox=address)
            .aggregate(n=Sum("size_bytes"))["n"] or 0)


def _victims(workspace_id: int, address: str, *, keep: int, over: int) -> list[tuple]:
    tier = Case(When(Q(thread__trashed_at__isnull=False) | Q(thread__spam=True), then=Value(0)),
                When(thread__archived=True, then=Value(1)),
                When(thread__unread=False, then=Value(2)),
                default=Value(3), output_field=IntegerField())
    rows = (MailMessage.objects.filter(workspace_id=workspace_id, mailbox=address).exclude(pk=keep)
            .annotate(_tier=tier).order_by("_tier", "created_at", "pk")
            .values_list("pk", "size_bytes", "thread_id", "raw_key"))
    out = []
    for row in rows.iterator(chunk_size=_CHUNK):
        if over <= 0:
            break
        out.append(row)
        over -= row[1]
    return out


def _delete_objects(keys: list[str], address: str) -> None:
    try:
        ses.delete_objects(keys)
    except Exception as exc:  # noqa: BLE001 - an orphaned object costs storage, not correctness
        logger.warning("mail cap %s: %d S3 object(s) not deleted: %r", address, len(keys), exc)


def enforce_cap(workspace_id: int, address: str, *, keep: int) -> int:
    """Evict the mailbox's oldest mail until it is within its cap; returns how many messages
    went. `keep` (the message just stored) is never evicted, even when it alone is over."""
    cap = cap_bytes()
    if not cap or not address:
        return 0
    used = used_bytes(workspace_id, address)
    if used <= cap:
        return 0
    victims = _victims(workspace_id, address, keep=keep, over=used - cap)
    if not victims:
        logger.info("mail cap %s: %d bytes over a %d cap, nothing left to evict", address, used, cap)
        return 0
    pks = [v[0] for v in victims]
    per_thread = Counter(v[2] for v in victims)
    raws = {v[3] for v in victims if v[3]}
    with transaction.atomic():
        keys: list[str] = []
        for i in range(0, len(pks), _CHUNK):
            chunk = pks[i:i + _CHUNK]
            keys += MailAttachment.objects.filter(message_id__in=chunk).exclude(s3_key="").values_list(
                "s3_key", flat=True)
            MailMessage.objects.filter(pk__in=chunk).delete()  # cascades its attachment rows
        cited = set(MailMessage.objects.filter(raw_key__in=raws).values_list("raw_key", flat=True))
        keys += sorted(raws - cited)
        for thread_id, n in per_thread.items():
            MailThread.objects.filter(pk=thread_id).update(message_count=Greatest(F("message_count") - n, 0))
        emptied, _ = MailThread.objects.filter(pk__in=list(per_thread), messages__isnull=True).delete()
        freed = sum(v[1] for v in victims)
        if keys:
            transaction.on_commit(lambda: _delete_objects(keys, address))
    logger.info("mail cap %s: evicted %d message(s), %d emptied thread(s), %d S3 object(s), "
                "%d bytes freed (%d of %d used)", address, len(pks), emptied, len(keys), freed,
                used - freed, cap)
    return len(pks)


def delete_threads(thread_ids: list[int]) -> int:
    """Delete threads for good: their messages, attachments and S3 objects (a `kept/` raw
    another mailbox's copy still cites stays). Returns how many threads went."""
    if not thread_ids:
        return 0
    with transaction.atomic():
        keys = list(MailAttachment.objects.filter(message__thread_id__in=thread_ids).exclude(s3_key="")
                    .values_list("s3_key", flat=True))
        raws = set(MailMessage.objects.filter(thread_id__in=thread_ids).exclude(raw_key="")
                   .values_list("raw_key", flat=True))
        MailThread.objects.filter(pk__in=thread_ids).delete()
        cited = set(MailMessage.objects.filter(raw_key__in=raws).values_list("raw_key", flat=True))
        keys += sorted(raws - cited)
        if keys:
            transaction.on_commit(lambda: _delete_objects(keys, "purge"))
    return len(thread_ids)


def purge_expired() -> None:
    """The daily tick: trash past TRASH_DAYS, and uploads no message or draft ever took."""
    now = timezone.now()
    delete_threads(list(MailThread.objects.filter(trashed_at__lt=now - timedelta(days=TRASH_DAYS))
                        .values_list("pk", flat=True)[:_CHUNK]))
    held = {str(u) for ups in MailDraft.objects.values_list("uploads", flat=True) for u in ups or []}
    stale = [u for u in MailUpload.objects.filter(created_at__lt=now - timedelta(days=UPLOAD_DAYS))[:_CHUNK]
             if str(u.uuid) not in held]
    if stale:
        keys = [u.s3_key for u in stale]
        MailUpload.objects.filter(pk__in=[u.pk for u in stale]).delete()
        _delete_objects(keys, "uploads")
