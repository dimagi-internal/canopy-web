"""Mint and resolve chat keys — see `models.ChatKey` for why they exist."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from django.utils import timezone

from .models import ChatKey, Session

PREFIX = "chk_"
#: A chat's session can sit idle for days between messages and still hold the
#: key it connected with; each new claim mints a fresh one.
LIFETIME = dt.timedelta(days=7)
#: The request header that carries it — beside the caller's own bearer token,
#: which still says WHO is calling; this says which chat they are acting for.
HEADER = "X-Canopy-Chat-Key"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(session: Session) -> str:
    """A key for this chat. Returned once, never stored raw. Old keys past their
    lifetime are removed as new ones are made, so the table does not grow."""
    now = timezone.now()
    ChatKey.objects.filter(expires_at__lte=now).delete()
    raw = PREFIX + secrets.token_urlsafe(32)
    ChatKey.objects.create(session=session, token_hash=_hash(raw), expires_at=now + LIFETIME)
    return raw


def resolve(raw: str | None) -> Session | None:
    """The chat a raw key stands for, or None. Fails closed on anything odd."""
    raw = (raw or "").strip()
    if not raw.startswith(PREFIX):
        return None
    key = (ChatKey.objects.select_related("session", "session__agent")
           .filter(token_hash=_hash(raw), expires_at__gt=timezone.now()).first())
    return key.session if key is not None else None


def from_request(request) -> Session | None:
    """The chat named by a request's chat-key header, if any."""
    return resolve(request.headers.get(HEADER) if request is not None else None)
