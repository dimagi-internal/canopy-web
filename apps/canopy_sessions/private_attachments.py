"""Files in a SENSITIVE conversation: the agent's copy for one turn, the people's sealed.

A private conversation's words reach the agent as its turn's prompt, in clear, in
transit: canopy-web holds the prompt only until the turn ends. A file is treated the
same way, not as an archive (spec
2026-10-23-sensitive-conversations-sealed-to-their-people §2, "Files"):

* **The agent's copy is transient.** The person's browser uploads the file for one
  turn (`api.upload_attachment`). canopy-web stores it, under a key that names no file
  (`plain_key`), only until the turn that claimed it (`send.claim_pending_attachments`
  → `bind_turn`) fetches it — the runner's download route serves it ONCE, to the box
  holding that turn, and deletes it as it does (`take_for_runner`) — or ends
  (`closing.close_turn` → `forget_turn`), whichever comes first. One never sent, or
  whose turn never ran, goes on the sensitive deadline (`sweep`). Forgetting deletes
  the object and blanks the row's `filename`, `storage_key` and exact type.
* **The people's copy is sealed in their browser** to the conversation's public key
  with the construction every message is sealed with (ECDH-ES + HKDF-SHA256 +
  AES-256-GCM, `frontend/src/vault/sealedFiles.ts`), bound to the conversation, the
  attachment and its uploader: the name, type and size in `sealed_name` (`cf1:`), the
  bytes as a blob (`cfb1`) at `sealed_blob_key`. canopy-web checks the envelopes'
  SHAPE (`check_sealed_name`, `check_sealed_blob`) and cannot open either. A send
  claims only a file whose people's copy is stored, so nothing reaches the agent that
  its people cannot open again.
* **No name anywhere else.** The turn's `origin_ref` names the file by id alone; the
  durable row keeps a size bucket (`size_bucket`) and a type family (`family`); no log
  line names a file. The runner learns the name from the one download.

docs/architecture/tenancy.md § Sensitive conversations ("Files").
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import struct

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from ninja.errors import HttpError

from apps.harness.models import Turn

from . import attachment_storage
from .models import Attachment, Message

logger = logging.getLogger(__name__)

NAME_PREFIX = "cf1:"
BLOB_MAGIC = b"cfb1"
#: The most a sealed name may be: a 255-character name and its type, sealed.
MAX_SEALED_NAME = 4096
MAX_BLOB_HEADER = 2048
#: The size buckets a sensitive file's row records, never its own size.
_BUCKETS = (64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 16 * 1024 * 1024)


def name_info(conversation, attachment_id, uploader_id) -> str:
    return f"canopy-chat/v1/file-name|{conversation}|{attachment_id}|{uploader_id}"


def blob_info(conversation, attachment_id, uploader_id) -> str:
    return f"canopy-chat/v1/file-bytes|{conversation}|{attachment_id}|{uploader_id}"


def size_bucket(n: int) -> int:
    """The bucket a file of `n` bytes is recorded as: the first bound at or above it."""
    for bound in _BUCKETS:
        if n <= bound:
            return bound
    return _BUCKETS[-1] * ((n + _BUCKETS[-1] - 1) // _BUCKETS[-1])


def family(content_type: str) -> str:
    """`image/*` for an image, else `application/octet-stream`: enough to choose a
    preview, nothing about the file."""
    return "image/*" if (content_type or "").startswith("image/") else "application/octet-stream"


def plain_key(session_id, attachment_id) -> str:
    """The agent's copy: a key that names the session and attachment, never the file."""
    return f"sessions/{session_id}/{attachment_id}/plain"


def blob_key(session_id, attachment_id) -> str:
    return f"sessions/{session_id}/{attachment_id}/sealed"


# ── the people's copy: shapes canopy-web can check and cannot open ──────────────


class SealedRefused(ValueError):
    """A sealed copy that is not this attachment's, or not an envelope at all."""


def _unb64u(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(c not in _B64U for c in value):
        raise SealedRefused("not base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SealedRefused("not base64url") from exc


_B64U = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _check_binding(body, attachment: Attachment, conversation) -> None:
    if not isinstance(body, dict):
        raise SealedRefused("not an envelope")
    want = {"c": str(conversation), "a": str(attachment.pk), "u": str(attachment.uploaded_by_id)}
    for k, v in want.items():
        if body.get(k) != v:
            raise SealedRefused("this envelope is sealed for another file")
    if body.get("alg") != "ecdh-es":
        raise SealedRefused("not an ecdh-es envelope")
    for k in ("epk", "n"):
        _unb64u(body.get(k))
    if len(_unb64u(body["epk"])) != 65 or len(_unb64u(body["n"])) != 12:
        raise SealedRefused("not an ecdh-es envelope")


def check_sealed_name(value: str, attachment: Attachment, conversation) -> str:
    """`value` when it is a `cf1:` envelope sealed for THIS attachment (conversation,
    attachment id, uploader); else SealedRefused. canopy-web checks its shape only."""
    if not isinstance(value, str) or not value.startswith(NAME_PREFIX) or len(value) > MAX_SEALED_NAME:
        raise SealedRefused("not a sealed name")
    try:
        body = json.loads(_unb64u(value[len(NAME_PREFIX):]))
    except ValueError as exc:
        raise SealedRefused("not a sealed name") from exc
    _check_binding(body, attachment, conversation)
    if len(_unb64u(body.get("ct"))) < 17:
        raise SealedRefused("not a sealed name")
    return value


def check_sealed_blob(blob: bytes, attachment: Attachment, conversation, *, max_bytes: int) -> None:
    """A `cfb1` blob sealed for THIS attachment: magic, a header naming it, and a
    ciphertext with its tag; never bigger than `max_bytes`. SealedRefused otherwise."""
    if len(blob) > max_bytes:
        raise SealedRefused("the sealed copy is larger than the limit")
    if len(blob) < 8 or blob[:4] != BLOB_MAGIC:
        raise SealedRefused("not a sealed file")
    (n,) = struct.unpack(">I", blob[4:8])
    if n <= 0 or n > MAX_BLOB_HEADER or len(blob) < 8 + n + 16:
        raise SealedRefused("not a sealed file")
    try:
        header = json.loads(blob[8:8 + n].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SealedRefused("not a sealed file") from exc
    _check_binding(header, attachment, conversation)


def max_blob_bytes(max_upload: int) -> int:
    """The largest sealed copy of a file at the upload limit: its padding (an eighth at
    most, `sealedFiles.ts::paddedLength`), the tag and the header."""
    return max_upload + max_upload // 8 + 4096 + 16 + 8 + MAX_BLOB_HEADER


# ── the agent's copy: held for one turn ─────────────────────────────────────────


def _delete_objects(keys) -> None:
    keys = [k for k in keys if k]
    if not keys or not attachment_storage.is_configured():
        return
    for key in keys:
        try:
            attachment_storage.delete(key)
        except Exception:  # noqa: BLE001 — logged by id-free key only, never a name
            logger.warning("could not delete a private conversation's file object %s", key, exc_info=True)


def _forget(rows) -> int:
    """Delete the agent's copy of each of `rows` (the object, on commit) and blank what
    the row said about the file. The people's copy stays."""
    pairs = [(r.pk, r.storage_key) for r in rows if r.storage_key or r.filename]
    if not pairs:
        return 0
    Attachment.objects.filter(pk__in=[pk for pk, _ in pairs]).update(filename="", storage_key="")
    keys = [k for _, k in pairs]
    transaction.on_commit(lambda: _delete_objects(keys))
    return len(pairs)


def bind_turn(attachment_ids, turn) -> None:
    """The turn a send's files were claimed for: the one turn whose box may fetch them."""
    if attachment_ids and turn is not None:
        Attachment.objects.filter(pk__in=list(attachment_ids), sensitive=True, turn__isnull=True) \
            .update(turn=turn)


def take_for_runner(attachment: Attachment):
    """The agent's copy, served once: the row is blanked first (so a second request
    finds nothing), the bytes read, and the object deleted however the read went.
    Returns the HttpResponse, or None when it was already taken."""
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")
    key, name = attachment.storage_key, attachment.filename
    if not key:
        return None
    won = Attachment.objects.filter(pk=attachment.pk, storage_key=key).update(filename="", storage_key="")
    if not won:
        return None
    try:
        stored = attachment_storage.get(key)
    finally:
        _delete_objects([key])
    response = HttpResponse(stored.body, content_type=stored.content_type)
    # The runner names the file on its box from this, once; safe_filename made it.
    response["Content-Disposition"] = f'inline; filename="{name or "attachment"}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    response["Cache-Control"] = "no-store"
    return response


def forget_turn(turn) -> int:
    """A turn ended: whatever of its files its box did not fetch is deleted now."""
    return _forget(list(Attachment.objects.filter(turn=turn, sensitive=True)
                        .exclude(storage_key="", filename="")))


def sweep(now=None) -> int:
    """The sensitive deadline (`CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS`) for files: one
    never sent goes whole (both copies), and the agent's copy of one sent goes when its
    turn is over or was never made. Returns how many rows it touched."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=int(getattr(settings, "CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS", 3600)))
    unsent = list(Attachment.objects.filter(sensitive=True, sent_at__isnull=True, created_at__lt=cutoff))
    if unsent:
        keys = [k for a in unsent for k in (a.storage_key, a.sealed_blob_key)]
        Attachment.objects.filter(pk__in=[a.pk for a in unsent]).delete()
        transaction.on_commit(lambda: _delete_objects(keys))
    held = (Attachment.objects.filter(sensitive=True, sent_at__isnull=False)
            .exclude(storage_key="", filename=""))
    stale = [a for a in held.select_related("turn")
             if a.turn is None and a.sent_at < cutoff
             or a.turn is not None and a.turn.status in Turn.TERMINAL]
    return len(unsent) + _forget(stale)


def delete_all(rows) -> None:
    """Both copies' objects, on commit, for rows being deleted."""
    keys = [k for a in rows for k in (a.storage_key, a.sealed_blob_key)]
    transaction.on_commit(lambda: _delete_objects(keys))


# ── history: what the people's browsers open ────────────────────────────────────


def history(session) -> list[dict]:
    """The sealed files sent in `session`, each placed at the turn index of the person's
    line it was sent with (its Message, or the transcript row its turn delivered).
    For a reader `sensitivity.reads_content` admits; carries no name."""
    rows = list(Attachment.objects.filter(session=session, sensitive=True, sent_at__isnull=False)
                .exclude(sealed_name__isnull=True).exclude(sealed_blob_key="")
                .order_by("created_at"))
    by_message = {m: i for m, i in Message.objects.filter(
        pk__in=[a.message_id for a in rows if a.message_id]).values_list("pk", "turn_index")}
    turns = [a.turn_id for a in rows if a.turn_id and a.message_id is None]
    by_turn: dict = {}
    for tid, idx in (Message.objects.filter(session=session, role=Message.USER, source_turn_id__in=turns)
                     .order_by("turn_index").values_list("source_turn_id", "turn_index")):
        by_turn.setdefault(tid, idx)
    return [{
        "id": a.pk,
        "turn_index": by_message.get(a.message_id) if a.message_id else by_turn.get(a.turn_id),
        "uploaded_by": a.uploaded_by_id,
        "family": family(a.content_type),
        "size_bucket": a.size_bytes,
        "sealed_name": a.sealed_name,
    } for a in rows]
