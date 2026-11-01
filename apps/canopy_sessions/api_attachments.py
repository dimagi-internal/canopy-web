"""Files attached to a session, and a private conversation's sealed copies."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import File, Form
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.api.route_gates import gate
from apps.harness import sensitivity

from . import (
    access,
    attachment_storage,
    conversation_keys,
    private_attachments,
)
from .models import Attachment, Session
from .schemas import (
    AttachmentOut,
    SealedAttachmentOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _session_or_404,
    _site_scoped,
    _visible_slugs,
    router,
)

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


#: Why a private conversation's file was not taken (`upload_attachment`).
ATTACHMENTS_NOT_HERE = ("a file in a private conversation is sealed to its people in their browser: "
                        "attach it from canopy's chat page or the panel, not from a server")
ATTACHMENTS_KEYLESS = ("this private conversation has no key yet, so a file could not be sealed to "
                       "its people: send a message first")


@router.post(
    "/{session_id}/attachments",
    response={201: AttachmentOut},
    summary="Upload an attachment for this session",
)
@gate("session-acl", kind="conversation")  # write
def upload_attachment(
    request: HttpRequest, session_id: uuid.UUID, file: UploadedFile = File(...)
):
    """Store the bytes and return an id the caller passes to /send.

    UNBOUND on purpose (`message` null): the composer uploads while you are
    still typing, so the message it belongs to does not exist yet. Sending binds
    it. That ordering is also what lets the UI show a thumbnail before send.
    """
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    # A private conversation's file is two copies (`private_attachments`): this one, for
    # the agent, held only until its turn fetches it or ends, under a key that names no
    # file; and the people's, sealed in their browser (`POST …/sealed`). Only its own
    # people, at a browser, attach one: canopy's chat page, or the embedded panel's
    # frame (`_from_its_peoples_browser`) — never a site's backend, a token or MCP.
    sensitive = sensitivity.is_sensitive(session)
    if sensitive:
        if not _from_its_peoples_browser(request, session):
            raise HttpError(422, ATTACHMENTS_NOT_HERE)
        if conversation_keys.key_of(session) is None:
            raise HttpError(422, ATTACHMENTS_KEYLESS)
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    allowed = settings.ATTACHMENT_ALLOWED_CONTENT_TYPES
    if content_type not in allowed:
        # An allowlist: these bytes get opened by an agent and rendered inline by
        # a browser, so anything not explicitly understood is refused.
        raise HttpError(422, f"unsupported file type '{content_type or 'unknown'}'")
    if file.size is None or file.size <= 0:
        raise HttpError(422, "file is empty")
    if file.size > settings.ATTACHMENT_MAX_UPLOAD_BYTES:
        limit_mb = settings.ATTACHMENT_MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HttpError(422, f"file is larger than the {limit_mb}MB limit")

    attachment = Attachment(
        session=session,
        uploaded_by=request.user,
        filename=attachment_storage.safe_filename(file.name),
        content_type=content_type,
        size_bytes=file.size,
    )
    attachment.storage_key = attachment_storage.storage_key(
        session.id, attachment.id, attachment.filename
    )
    if sensitive:
        # The row and the key say nothing about the file: a size bucket, a type family,
        # an object key without its name. The name is held only for the runner's one
        # download (`private_attachments.take_for_runner`).
        attachment.sensitive = True
        attachment.size_bytes = private_attachments.size_bucket(file.size)
        attachment.content_type = private_attachments.family(content_type)
        attachment.storage_key = private_attachments.plain_key(session.id, attachment.id)
    # Bytes FIRST, row second: a row whose object is missing is a broken
    # thumbnail and a runner download that 500s, while an orphaned object is
    # invisible and sweepable. Fail in the harmless direction.
    attachment_storage.put(attachment.storage_key, file.read(), content_type)
    attachment.save()
    return 201, attachment


def _from_its_peoples_browser(request: HttpRequest, session) -> bool:
    """Whether this request is one of a private conversation's own people at a browser:
    `reads_content` on a chat surface, never a contact (who holds no key), and — when a
    site's delegated token carries it — from the panel's own frame, not the site's
    backend calling server to server (`sensitivity.request_is_panel_browser`, the rule
    the keyset goes out under). A file is attached, and its sealed copy stored, only so."""
    if getattr(request, "contact", None) is not None:
        return False
    if not sensitivity.reads_content(session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        return False
    return sensitivity.request_is_panel_browser(request)


def _sealed_attachment_or_404(request: HttpRequest, attachment_id: uuid.UUID) -> Attachment:
    """A private conversation's file, for one of its own people on its chat surfaces
    (`sensitivity.reads_content`, the gate its words go out through); 404 for anyone
    else, and for an ordinary conversation's file."""
    attachment = get_object_or_404(
        Attachment.objects.select_related("session").filter(
            session__in=_site_scoped(request, Session.objects.all()), sensitive=True),
        pk=attachment_id,
    )
    session = attachment.session
    if session.workspace_id not in _visible_slugs(request) or not access.can_read(request.user, session):
        raise HttpError(404, "attachment not found")
    if not sensitivity.is_sensitive(session) or not sensitivity.reads_content(
            session, user=request.user, chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(404, "attachment not found")
    return attachment


@router.post(
    "/attachments/{attachment_id}/sealed",
    response={204: None},
    summary="Store the people's sealed copy of a file in a private conversation",
)
@gate("session-acl", kind="conversation")  # its uploader, while unsent, one of its people
def put_sealed_attachment(request: HttpRequest, attachment_id: uuid.UUID,
                          sealed_name: Form[str], file: UploadedFile = File(...)):
    """The file's name and bytes, sealed in the uploader's browser to the
    conversation's key (`cf1:` name, `cfb1` blob), bound to the conversation, this
    attachment and its uploader. canopy-web checks the shape and cannot open either.
    Once per file, by its uploader, before it is sent; a send claims only a file
    whose sealed copy is stored."""
    attachment = _sealed_attachment_or_404(request, attachment_id)
    session = attachment.session
    if not _from_its_peoples_browser(request, session):
        raise HttpError(422, ATTACHMENTS_NOT_HERE)
    if attachment.uploaded_by_id != request.user.pk or not access.can_write(request.user, session):
        raise HttpError(403, "only the person who attached a file stores its sealed copy")
    if attachment.sent_at is not None:
        raise HttpError(409, "this attachment has already been sent")
    if attachment.sealed_blob_key:
        raise HttpError(409, "this attachment's sealed copy is already stored")
    key = conversation_keys.key_of(session)
    if key is None:
        raise HttpError(422, ATTACHMENTS_KEYLESS)
    blob = file.read()
    try:
        private_attachments.check_sealed_name(sealed_name, attachment, key.conversation)
        private_attachments.check_sealed_blob(
            blob, attachment, key.conversation,
            max_bytes=private_attachments.max_blob_bytes(settings.ATTACHMENT_MAX_UPLOAD_BYTES))
    except private_attachments.SealedRefused as exc:
        raise HttpError(422, str(exc)) from exc
    blob_key = private_attachments.blob_key(session.id, attachment.pk)
    attachment_storage.put(blob_key, blob, "application/octet-stream")
    won = Attachment.objects.filter(pk=attachment.pk, sealed_blob_key="", sent_at__isnull=True) \
        .update(sealed_blob_key=blob_key, sealed_name=sealed_name)
    if not won:
        raise HttpError(409, "this attachment's sealed copy is already stored")
    return 204, None


@router.get(
    "/attachments/{attachment_id}/sealed",
    summary="The sealed copy of a file in a private conversation",
)
@gate("session-acl")  # sensitivity.reads_content
def sealed_attachment_content(request: HttpRequest, attachment_id: uuid.UUID):
    """The people's copy: ciphertext their browser opens with the conversation key.
    Only for the conversation's own people on its chat surfaces."""
    attachment = _sealed_attachment_or_404(request, attachment_id)
    if not attachment.sealed_blob_key or (attachment.sent_at is None
                                          and attachment.uploaded_by_id != request.user.pk):
        raise HttpError(404, "attachment not found")
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")
    stored = attachment_storage.get(attachment.sealed_blob_key)
    response = HttpResponse(stored.body, content_type="application/octet-stream")
    response["Content-Disposition"] = "attachment"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    response["Cache-Control"] = "private, no-store"
    return response


@router.get(
    "/{session_id}/sealed-attachments",
    response=list[SealedAttachmentOut],
    summary="The sealed files sent in a private conversation",
)
@gate("session-acl")  # readable_sessions, then sensitivity.reads_content
def list_sealed_attachments(request: HttpRequest, session_id: uuid.UUID):
    """Each file sent in a private conversation, placed at the person's line it was sent
    with, with its sealed name for their browser to open. No name, no exact size.
    Empty for an ordinary conversation and for anyone `reads_content` does not admit."""
    session = _session_or_404(request, session_id)
    if not sensitivity.is_sensitive(session) or not sensitivity.reads_content(
            session, user=request.user, chat_surface=sensitivity.request_is_chat_surface(request)):
        return []
    return private_attachments.history(session)


@router.get(
    "/attachments/{attachment_id}/content",
    summary="Stream an attachment's bytes",
)
@gate("session-acl")  # can_read
def attachment_content(request: HttpRequest, attachment_id: uuid.UUID):
    """The bytes, for a person who can read the chat — the browser rendering a
    thumbnail. Gated on who can read the session, not on who uploaded it: a
    session is multiplayer, so a teammate who can read it sees what was shared
    in it. A runner does NOT come through here; it fetches what the turn it is
    serving carries via the harness route (turn_attachment_content)."""
    attachment = get_object_or_404(
        Attachment.objects.select_related("session").filter(
            session__in=_site_scoped(request, Session.objects.all())),
        pk=attachment_id,
    )
    # The session's own read rule: a teammate who can read the chat can see
    # what was shared in it; a co-tenant who cannot read it cannot either.
    if attachment.session.workspace_id not in _visible_slugs(request) or not access.can_read(
            request.user, attachment.session):
        raise HttpError(404, "attachment not found")
    # What was shared in a sensitive conversation is its words: only its own
    # people, on its chat surfaces, read it back (`sensitivity.reads_content`).
    if not sensitivity.reads_content(attachment.session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(404, "attachment not found")
    # A private conversation's file is never served in clear to anyone: its agent's
    # copy goes once to the box holding its turn, and its people open the sealed copy.
    if attachment.sensitive:
        raise HttpError(404, "attachment not found")
    return attachment_storage.http_response(attachment)


@router.delete("/attachments/{attachment_id}", response={204: None})
@gate("session-acl", kind="conversation")  # can_write
def delete_attachment(request: HttpRequest, attachment_id: uuid.UUID):
    """Remove an attachment you have not sent yet — the composer's "x" on a chip.

    Only while UNBOUND. Once it is part of a sent message it is transcript, and
    deleting it would leave the agent's reply referring to something nobody else
    can see.
    """
    attachment = get_object_or_404(
        Attachment.objects.select_related("session").filter(
            session__in=_site_scoped(request, Session.objects.all())),
        pk=attachment_id,
    )
    if attachment.session.workspace_id not in _visible_slugs(request) or not access.can_read(
            request.user, attachment.session):
        raise HttpError(404, "attachment not found")
    if not access.can_write(request.user, attachment.session):
        raise HttpError(403, "you can read this session but not act in it")
    if attachment.message_id is not None or (attachment.sensitive and attachment.sent_at is not None):
        raise HttpError(409, "this attachment has already been sent")
    if attachment_storage.is_configured():
        for key in (attachment.storage_key, attachment.sealed_blob_key):
            if key:
                attachment_storage.delete(key)
    attachment.delete()
    return 204, None
