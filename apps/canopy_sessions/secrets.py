"""Secrets a person hands to ONE chat. See `models.SessionSecret`.

The person shares a secret from the chat's session menu; nothing is posted into
the chat. They just mention it by name ("put GH_TOKEN in canopy's Actions
secrets"), and the agent running that chat finds and spends it with
`canopy secret list` / `canopy secret exec NAME -- <command>`.

The rules, each load-bearing:

* **The value never enters the chat.** Nothing here writes a `Message`, and
  nothing a browser can reach returns a value — the list is names and times.
* **Only the session driving the chat can use it.** It proves which chat it is
  with the CHAT KEY canopy issued when its runner claimed the chat's turn
  (`models.ChatKey`, `/api/session-secrets/key`): a permission canopy hands out,
  rather than one inferred from who is calling. Another session of the same
  agent, or the same agent in another chat, holds a different key and gets a
  404 (Jonathan, 2026-09-25: "only accessible to this session").

  It replaced (2026-09-26) an inference from two indirect facts — the caller
  was the chat's agent login (`Agent.user`, shared by every turn of that agent)
  and named the chat's Claude session id (a value that can be found rather than
  given).
* **Plaintext only to a bearer.** A cookie is refused even for the sharer.
* **Thirty minutes.** A hand-off, not a store — see `TTL`.

Honest limit: processes of one OS user can read each other's files, so this
cannot stop a hostile local process that goes looking for another chat's key
file. What it stops is every ordinary path by which a secret
meant for this conversation reaches a different one.
"""
from __future__ import annotations

import datetime as dt
import re

from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret

from .models import Session, SessionSecret

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
VALUE_MAX = 16_384
#: A shared secret dies this long after it was shared (re-sharing the name
#: restarts the clock). Enforced on every read, and the rows are deleted by
#: `purge_expired` on the runner heartbeat, so the ciphertext does not outlive
#: its use either (Jonathan, 2026-09-25).
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


def live_secrets(session: Session):
    purge_expired()  # an expired secret is invisible even if no heartbeat swept it yet
    return session.secrets.select_related("created_by")


def resolve_secret(session: Session, name: str, *, user) -> str | None:
    """The plaintext, or None when there is no such live secret. Callers gate first."""
    row = live_secrets(session).filter(name=name).first()
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
