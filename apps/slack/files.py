"""Files people attach in Slack, turned into the same session attachments a web
chat makes — so the runner downloads them for the agent exactly as it does a
screenshot pasted into canopy-web (`origin_ref.attachments`, execute.fetch_attachments).

Slack serves a file's bytes from `url_private_download` to a bearer of the bot
token, and only with the `files:read` scope. Without that scope Slack does not
refuse: it answers **200 with an HTML sign-in page**, so a status check alone
would store a web page as `screenshot.png`. The content type is what decides.

Each file is best-effort: one that cannot be fetched or is not allowed becomes a
line in the prompt, never a failed message — the person still reaches the agent,
and the agent is told what it is missing.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT = 20


def _label(f: dict) -> str:
    return str(f.get("name") or f.get("title") or "file")


def store(installation, session, user, files) -> tuple[list[dict], list[str]]:
    """([attachment refs for origin_ref], [prompt notes for files not stored])."""
    from apps.canopy_sessions import attachment_storage
    from apps.canopy_sessions.models import Attachment

    refs: list[dict] = []
    missed: list[str] = []
    allowed = set(settings.ATTACHMENT_ALLOWED_CONTENT_TYPES)
    limit = settings.ATTACHMENT_MAX_UPLOAD_BYTES
    for f in files:
        name = _label(f)
        declared = str(f.get("mimetype") or "").split(";")[0].strip().lower()
        url = f.get("url_private_download") or f.get("url_private")
        if not attachment_storage.is_configured() or installation is None:
            missed.append(f"{name} (attachments are not configured)")
            continue
        if declared not in allowed:
            missed.append(f"{name} ({declared or 'unknown type'} is not an attachment type canopy accepts)")
            continue
        if int(f.get("size") or 0) > limit:
            missed.append(f"{name} (larger than the {limit // (1024 * 1024)}MB limit)")
            continue
        if not url:
            missed.append(f"{name} (Slack sent no download link)")
            continue
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {installation.bot_token}"},
                                timeout=TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("slack file download failed: %s", e)
            missed.append(f"{name} (could not be downloaded from Slack)")
            continue
        got = str(resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        body = resp.content or b""
        if got not in allowed:
            # The no-scope case: a 200 sign-in page. Say what fixes it.
            missed.append(f"{name} (Slack would not hand canopy the file — the app may need the "
                          "files:read permission and a re-install)")
            continue
        if not body or len(body) > limit:
            missed.append(f"{name} (empty or over the {limit // (1024 * 1024)}MB limit)")
            continue
        att = Attachment(session=session, uploaded_by=user if getattr(user, "pk", None) else None,
                         filename=attachment_storage.safe_filename(name), content_type=got,
                         size_bytes=len(body),
                         # Sent with THIS message, never swept into a later send.
                         sent_at=timezone.now())
        att.storage_key = attachment_storage.storage_key(session.id, att.id, att.filename)
        # Bytes first, row second — the same order as the web upload.
        attachment_storage.put(att.storage_key, body, got)
        att.save()
        refs.append({"id": str(att.id), "filename": att.filename, "content_type": att.content_type})
    notes = []
    if missed:
        notes.append("[Attached in Slack but not available to you: " + "; ".join(missed)
                     + ". Ask them to paste what you need as text.]")
    return refs, notes
