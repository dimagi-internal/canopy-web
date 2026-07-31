"""Which mailboxes are due for a check, and why.

Two independent triggers, one place that decides:

* the **doorbell** — a ``check_inbox`` control frame from canopy-web, meaning
  Gmail has just told the server that mailbox changed. Bypasses the timer.
* the **timer** — ``inbox_poll_seconds`` (300s), unchanged. It is no longer the
  delivery mechanism but it stays the auditor: a message the timer finds is a
  message push failed to ring for, and that is what makes push failure loud
  instead of silent.

The stamp is now PER MAILBOX rather than one global stamp for the whole sweep.
A doorbell for eva must not reset hal's timer — with one shared stamp, a busy
mailbox would keep deferring every quiet one, and the quiet ones are exactly
where a silently-broken watch hides.

Thread-safety matters here: ``ring()`` is called from the wake-listener thread
while ``due()`` runs on the poll loop, so the pending set is guarded by a lock.
"""
from __future__ import annotations

import threading

#: Mailboxes rung by the doorbell since the last check, address-keyed.
_pending: set[str] = set()
_lock = threading.Lock()


def ring(mailbox: str) -> None:
    """A doorbell arrived for this mailbox. Called from the wake-listener thread."""
    if not mailbox:
        return
    with _lock:
        _pending.add(mailbox.strip().lower())


def take_pending() -> set[str]:
    """Drain and return the rung set. Draining rather than peeking is what makes
    a doorbell fire exactly one check: a frame that arrives DURING the check is
    kept for the next tick, which is correct — it announced a change we may have
    read a moment too early."""
    with _lock:
        pending = set(_pending)
        _pending.clear()
    return pending


def due(mailboxes: dict, stamps: dict, *, now: float, interval: float,
        rung: set[str] | None = None) -> list[str]:
    """The agent slugs whose mailbox should be checked this tick.

    ``mailboxes`` is ``Config.mailboxes`` ({slug: {account, client, ...}}),
    ``stamps`` is ``{slug: last_checked_epoch}``. A mailbox is due when it was
    rung, or when its own timer has elapsed.
    """
    rung = rung or set()
    out = []
    for slug, box in (mailboxes or {}).items():
        address = (box.get("account") or "").strip().lower()
        if address and address in rung:
            out.append(slug)
            continue
        if now - float(stamps.get(slug, 0.0)) >= interval:
            out.append(slug)
    return out


def discovered_by(slug: str, rung_slugs: set[str]) -> str:
    """How this check was triggered — the tag that makes the poll an auditor.

    The server compares it against the mailbox's watch state: a ``poll``-tagged
    turn on a mailbox with a live watch means push is registered but not
    delivering.
    """
    return "push" if slug in rung_slugs else "poll"
