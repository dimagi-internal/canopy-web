"""Reply push: the agent's replies to a host site's contacts go to the host, signed.

A relay site (`docs/architecture/host-relay.md`) carries its people's conversations
over a channel of its own, and with a `reply_webhook_url` on its host site it never
polls: when a turn in one of its contacts' conversations produces the agent's prose,
canopy POSTs it there as a JWS signed with canopy's OAuth client key
(`canopy_sdk.host.replies`: `typ: canopy-reply+jwt`, five minutes, the host verifies
with `verify_reply`).

* **Which turns.** A turn on a session whose contact came from a site (`Contact.app`)
  that is live, has a `reply_webhook_url`, and whose conversation the session is
  (`metadata["embed_app"]`). A sensitive conversation's words are never pushed (the
  same rule as Slack's mirror): its contact could not read them back anywhere either.
* **What.** `final` (the default): one post when the turn ends, with all of that turn's
  assistant prose and `ended: true`. `stream`: a post per assistant row as it lands, then
  an `ended` post with no text. A turn that did not finish well carries `error` (its
  status) on its `ended` post, so the host can tell the person something went wrong.
* **Exactly once per (turn, seq).** A `HostReplyDelivery` row is inserted before the
  first attempt under a unique `(turn, seq)` and one `ended` row per turn; a
  re-delivered signal finds it and stops. Each attempt signs afresh; the host dedupes
  on `(turn, seq)`.
* **Retries.** The first attempt runs on a background thread once the turn's write has
  committed; a failure is retried by the off-request clock (`maybe_deliver`, every
  `TICK_SECONDS`) at `BACKOFF`, then given up on. The text is emptied once a row is
  delivered or given up on.
* **Outbound.** Every POST goes through `outbound.check_url` (https, no private address
  space, checked at send because a name can be re-pointed) and never follows a redirect.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

import requests
from canopy_sdk.host import replies as wire
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Max
from django.utils import timezone

from apps.canopy_sessions.services import EMBED_APP_KEY
from apps.harness import sensitivity
from apps.harness.models import Turn, TurnEvent
from apps.tokens.models import AppCredential, HostReplyDelivery

from . import client_identity, outbound

logger = logging.getLogger(__name__)

#: The waits between attempts; after the last wait's attempt fails, the reply is given up
#: on — 6 attempts over about 42 minutes. A reply is conversational: one that arrives an
#: hour late is rarely worth more than the host knowing it never came.
BACKOFF = [dt.timedelta(seconds=s) for s in (10, 30, 120, 600, 1800)]
MAX_ATTEMPTS = len(BACKOFF) + 1
TIMEOUT_S = 5
#: How often the off-request clock looks for a due retry.
TICK_SECONDS = 15
#: The turn statuses that are not a good end, as the `error` an `ended` post carries.
_ERRORS = {"failed", "cancelled", "lost", "missed"}


# --- which turns ------------------------------------------------------------------


def _target(turn) -> AppCredential | None:
    """The site to push `turn`'s replies to, or None."""
    session = getattr(turn, "chat_session", None)
    if session is None or not session.contact_id:
        return None
    contact = session.contact
    if not contact.app_id:
        return None
    app = AppCredential.objects.filter(pk=contact.app_id, revoked_at__isnull=True).first()
    if app is None or not app.reply_webhook_url:
        return None
    if (session.metadata or {}).get(EMBED_APP_KEY) != app.name:
        return None
    if sensitivity.is_sensitive(turn):
        return None
    return app


def _prose(row) -> str:
    return str((row.payload or {}).get("text") or "").strip() if row.kind == "assistant" else ""


def on_rows(turn, rows) -> int:
    """`stream` mode: one post per assistant row. Returns how many were queued."""
    if not getattr(turn, "chat_session_id", None):
        return 0
    app = _target(turn)
    if app is None or app.reply_mode != AppCredential.REPLY_STREAM:
        return 0
    queued = 0
    for row in rows:
        text = _prose(row)
        if text and _record(app, turn, seq=row.seq, text=text) is not None:
            queued += 1
    return queued


def on_finished(turn) -> HostReplyDelivery | None:
    """The turn ended: its `ended` post (in `final` mode, with all its prose)."""
    turn = Turn.objects.select_related("chat_session__contact").filter(pk=turn.pk).first()
    if turn is None or turn.status not in Turn.TERMINAL:
        return None
    app = _target(turn)
    if app is None:
        return None
    rows = TurnEvent.objects.filter(turn=turn)
    last = rows.aggregate(m=Max("seq"))["m"] or 0
    taken = HostReplyDelivery.objects.filter(turn=turn).aggregate(m=Max("seq"))["m"]
    seq = max(last, (taken + 1) if taken is not None else 0)
    text = "" if app.reply_mode == AppCredential.REPLY_STREAM else \
        "\n\n".join(t for t in (_prose(r) for r in rows.filter(kind="assistant").order_by("seq")) if t)
    error = turn.status if turn.status in _ERRORS else ""
    return _record(app, turn, seq=seq, text=text, ended=True, error=error)


def _record(app, turn, *, seq: int, text: str, ended: bool = False, error: str = ""):
    """Insert the delivery row (once), and attempt it once the write commits."""
    try:
        with transaction.atomic():
            row = HostReplyDelivery.objects.create(app=app, turn=turn, seq=seq, text=text,
                                                   ended=ended, error=error)
    except IntegrityError:
        return None  # already queued: a re-delivered signal, or a concurrent append
    transaction.on_commit(lambda: _deliver_soon(row.pk))
    return row


def _deliver_soon(delivery_id: int) -> None:
    threading.Thread(target=_attempt_by_id, args=(delivery_id,), daemon=True).start()


def _attempt_by_id(delivery_id: int) -> None:
    try:
        row = HostReplyDelivery.objects.filter(pk=delivery_id).first()
        if row is not None:
            attempt(row)
    except Exception:  # noqa: BLE001
        logger.exception("host reply delivery %s failed", delivery_id)
    finally:
        close_old_connections()


# --- one attempt --------------------------------------------------------------------


def sign(row: HostReplyDelivery, *, now: float | None = None) -> str:
    """The reply as canopy's signed statement, for this attempt."""
    turn = row.turn
    session = turn.chat_session
    return client_identity.sign_reply(
        row.app.reply_webhook_url, sub=session.contact.external_id, site=row.app.name,
        session=str(session.pk), turn=str(turn.pk), seq=row.seq, text=row.text, ended=row.ended,
        error=row.error, now=now)


def _post(url: str, token: str) -> int:
    outbound.check_url(url, what="reply webhook URL")
    resp = requests.post(url, data=token.encode(), timeout=TIMEOUT_S, allow_redirects=False,
                         headers={"Content-Type": wire.REPLY_CONTENT_TYPE})
    return resp.status_code


def attempt(row: HostReplyDelivery, *, now: dt.datetime | None = None, post=None) -> None:
    """One delivery attempt: signed afresh, POSTed, and what it answered recorded —
    delivered, rescheduled by `BACKOFF`, or given up on after `MAX_ATTEMPTS`."""
    now = now or timezone.now()
    with transaction.atomic():
        row = (HostReplyDelivery.objects.select_for_update(of=("self",))
               .select_related("app", "turn__chat_session__contact").get(pk=row.pk))
        if row.delivered_at or row.abandoned_at or row.next_attempt_at > now:
            return
        url = row.app.reply_webhook_url or ""
        try:
            if not url or row.app.revoked_at is not None:
                raise outbound.OutboundError("the site no longer takes reply push")
            status = (post or _post)(url, sign(row, now=now.timestamp()))
            ok, note = 200 <= status < 300, f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 — any failure is a retry
            ok, note = False, f"{type(exc).__name__}: {exc}"[:200]
        row.attempts += 1
        row.last_status = note
        fields = ["attempts", "last_status"]
        if ok:
            row.delivered_at = now
            row.text = ""
            fields += ["delivered_at", "text"]
        elif row.attempts >= MAX_ATTEMPTS or not url:
            row.abandoned_at = now
            row.text = ""
            fields += ["abandoned_at", "text"]
            logger.warning("host reply %s to %s given up after %s attempt(s): %s",
                           row.pk, row.app.name, row.attempts, note)
        else:
            row.next_attempt_at = now + BACKOFF[row.attempts - 1]
            fields.append("next_attempt_at")
        row.save(update_fields=fields)


def deliver_due(now: dt.datetime | None = None, limit: int = 50) -> int:
    """Attempt every delivery that is due. The off-request clock's job."""
    now = now or timezone.now()
    due = list(HostReplyDelivery.objects.filter(
        delivered_at__isnull=True, abandoned_at__isnull=True, next_attempt_at__lte=now)
        .order_by("next_attempt_at")[:limit])
    for row in due:
        try:
            attempt(row, now=now)
        except Exception:  # noqa: BLE001
            logger.exception("host reply delivery %s failed", row.pk)
    return len(due)


def maybe_deliver() -> None:
    """The off-request clock's entry point (`HostGrantsConfig.ready`). Never raises."""
    try:
        deliver_due()
    except Exception:  # noqa: BLE001
        logger.exception("host reply deliveries failed")


# --- what was said goes with the conversation --------------------------------------


def forget_session(turn_ids) -> None:
    """A person deleted a conversation's words: its replies not yet delivered keep none."""
    HostReplyDelivery.objects.filter(turn_id__in=list(turn_ids)).update(text="")
