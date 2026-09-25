"""Secrets handed to a chat by reference. See `models.SessionSecret`.

Three rules, each load-bearing:

* **The value never enters the chat.** What the sharer's browser posts to the
  session is `reference_message()` — a name and a URI. Nothing here writes a
  `Message`, and nothing that returns to a browser carries a value.
* **Plaintext only to a bearer.** A session cookie is refused even for the
  person who shared it, so no page can ever render one (the same line
  `agents.api.resolve_agent_credentials` draws).
* **Only someone who could act in this chat anyway** — a writer of the session
  (`access.can_write`), or the session's agent calling as its own canopy login
  (`Agent.user`). The second leg is what lets the agent that is DOING the work
  spend it: an agent's own identity is usually not a participant of the chat it
  is running in.
"""
from __future__ import annotations

import datetime as dt
import re

from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret

from . import access
from .models import Session, SessionSecret

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
VALUE_MAX = 16_384
SCHEME = "canopy-secret://"
#: A shared secret is a hand-off, not a store: it dies this long after it was
#: shared (re-sharing the name restarts the clock). Enforced on every read, and
#: the rows are deleted by `purge_expired` on the runner heartbeat, so the
#: ciphertext does not outlive its use either (Jonathan, 2026-09-25).
TTL = dt.timedelta(minutes=30)


def expires_at(row: SessionSecret) -> dt.datetime:
    return row.updated_at + TTL


def purge_expired(now: dt.datetime | None = None) -> int:
    """Delete every secret past its TTL. One DELETE; safe to call on any read."""
    cutoff = (now or timezone.now()) - TTL
    deleted, _ = SessionSecret.objects.filter(updated_at__lte=cutoff).delete()
    return deleted


def normalize_name(name: str) -> str:
    """`gh token` → `GH_TOKEN`. Rejects anything that still is not env-var shaped."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_").upper()
    if not NAME_RE.match(cleaned):
        raise ValueError("name must start with a letter and use only letters, digits and _")
    return cleaned


def reference(session_id, name: str) -> str:
    return f"{SCHEME}{session_id}/{name}"


def reference_message(session_id, name: str, note: str = "") -> str:
    """What the chat sees instead of the secret. Tells the agent how to spend it."""
    ref = reference(session_id, name)
    minutes = int(TTL.total_seconds() // 60)
    lines = [f"🔒 I shared a secret, `{name}`, with this session. Its value is not in this chat, "
             f"and it is deleted {minutes} minutes from now."]
    if note.strip():
        lines.append(note.strip())
    lines.append(
        f"Use it without reading it: `canopy secret exec {ref} -- <command>` "
        f"(sets ${name} for that one command and masks the value in its output; "
        f"`--stdin` pipes it in instead)."
    )
    return "\n\n".join(lines)


def set_secret(session: Session, name: str, value: str, *, user=None) -> SessionSecret:
    name = normalize_name(name)
    value = (value or "").strip()
    if not value:
        raise ValueError("value is required")
    if len(value) > VALUE_MAX:
        raise ValueError(f"value is longer than {VALUE_MAX} characters")
    row, _ = SessionSecret.objects.update_or_create(
        session=session, name=name,
        defaults={
            "value_enc": encrypt_secret(value),
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "last_used_at": None,
        },
    )
    return row


def may_resolve(user, session: Session) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    agent_user_id = getattr(getattr(session, "agent", None), "user_id", None)
    if agent_user_id is not None and agent_user_id == user.pk:
        return True
    return access.can_write(user, session)


def resolve_secret(session: Session, name: str, *, user) -> str | None:
    """The plaintext, or None when there is no such secret. Callers gate first."""
    purge_expired()  # an expired secret is refused even if no heartbeat swept it yet
    row = SessionSecret.objects.filter(session=session, name=name).first()
    if row is None:
        return None
    SessionSecret.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
    try:
        from apps.events import services as events

        events.record([{
            "source": "canopy_sessions.secrets",
            "kind": "session.secret.resolved",
            "level": "info",
            "key": f"{session.pk}:{name}:{user.pk}",
            "summary": f"{name} resolved for session {str(session.pk)[:8]} by {user.email or user.pk}",
            "payload": {"session": str(session.pk), "name": name, "user": user.pk},
        }], workspace=session.workspace)
    except Exception:  # noqa: BLE001 — an audit hiccup must not deny the hand-off
        pass
    return decrypt_secret(row.value_enc)
