"""Reply push: the agent's replies to a host site's contacts go to the host, signed.

A relay site (`docs/architecture/host-relay.md`) carries its people's conversations
over a channel of its own, and with a `reply_webhook_url` on its host site it never
polls: when a turn in one of its contacts' conversations produces the agent's prose,
canopy POSTs it there as a JWS signed with canopy's OAuth client key
(`canopy_sdk.host.replies`: `typ: canopy-reply+jwt`, five minutes, the host verifies
with `verify_reply`).

* **Where.** The site's reply webhook (`push_for`): the `reply_webhook_url` a person set on
  its host site, else the one the site declares in its signed `site.json` (`site.reply_url`),
  which canopy adopts only on the site's own registered origin (`site_needs.declared_reply`).
* **Which turns.** A turn on a RELAY session (`metadata["relay_channel"]`, stamped from the
  relay token that started it) whose contact came from a site (`Contact.app`) that is live,
  has a reply webhook, and whose conversation the session is (`metadata["embed_app"]`).
  The webhook is the site's, one per site whatever its surfaces: the host routes a reply by its
  `session`, and only a relay needs one, since a widget's visitor reads the reply in the panel.
  A sensitive conversation's words are never pushed (the same rule as Slack's mirror): its
  contact could not read them back anywhere either.
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
from dataclasses import dataclass

import requests
from canopy_sdk.host import replies as wire
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Max
from django.utils import timezone

from apps.canopy_sessions.services import EMBED_APP_KEY, RELAY_CHANNEL_KEY
from apps.harness import latency, sensitivity
from apps.harness.models import Turn, TurnEvent
from apps.tokens.models import AppCredential, HostReplyDelivery

from . import client_identity, outbound, site_needs

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


# --- where ------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyPush:
    """Where and how canopy pushes a site's replies, and who said so."""

    url: str
    #: `final` or `stream`.
    mode: str
    #: `person`: set on the host site by `set_by`; `site`: declared in its `site.json`.
    source: str
    set_by: str = ""


def push_for(app) -> ReplyPush | None:
    """The site's reply webhook: a person's setting wins, else what the site declares and
    canopy adopts (`site_needs.declared_reply`). None when it has neither."""
    if app is None:
        return None
    if app.reply_webhook_url:
        who = app.reply_webhook_set_by
        return ReplyPush(app.reply_webhook_url, app.reply_mode, "person",
                         set_by=(who.email or who.get_username()) if who is not None else "")
    declared = site_needs.declared_reply(app)
    return ReplyPush(declared.url, declared.mode, "site") if declared.adopted else None


def _url_of(app) -> str:
    push = push_for(app)
    return push.url if push is not None else ""


# --- which turns ------------------------------------------------------------------


def _target(turn) -> tuple[AppCredential, ReplyPush] | None:
    """The site to push `turn`'s replies to, and where, or None."""
    session = getattr(turn, "chat_session", None)
    if session is None or not session.contact_id:
        return None
    contact = session.contact
    if not contact.app_id:
        return None
    app = (AppCredential.objects.select_related("reply_webhook_set_by")
           .filter(pk=contact.app_id, revoked_at__isnull=True).first())
    if app is None:
        return None
    if (session.metadata or {}).get(EMBED_APP_KEY) != app.name:
        return None
    if not (session.metadata or {}).get(RELAY_CHANNEL_KEY):
        return None
    if sensitivity.is_sensitive(turn):
        return None
    # Last: a site with no webhook of a person's reads its declared one, which may fetch.
    push = push_for(app)
    return (app, push) if push is not None else None


def _prose(row) -> str:
    return str((row.payload or {}).get("text") or "").strip() if row.kind == "assistant" else ""


def on_rows(turn, rows) -> int:
    """`stream` mode: one post per assistant row. Returns how many were queued."""
    if not getattr(turn, "chat_session_id", None):
        return 0
    target = _target(turn)
    if target is None or target[1].mode != AppCredential.REPLY_STREAM:
        return 0
    app = target[0]
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
    target = _target(turn)
    if target is None:
        return None
    app, push = target
    rows = TurnEvent.objects.filter(turn=turn)
    last = rows.aggregate(m=Max("seq"))["m"] or 0
    taken = HostReplyDelivery.objects.filter(turn=turn).aggregate(m=Max("seq"))["m"]
    seq = max(last, (taken + 1) if taken is not None else 0)
    text = "" if push.mode == AppCredential.REPLY_STREAM else \
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


def sign(row: HostReplyDelivery, *, now: float | None = None, url: str | None = None) -> str:
    """The reply as canopy's signed statement, for this attempt, addressed (`aud`) to `url`
    (default: the site's reply webhook now)."""
    turn = row.turn
    session = turn.chat_session
    return client_identity.sign_reply(
        _url_of(row.app) if url is None else url, sub=session.contact.external_id, site=row.app.name,
        session=str(session.pk), turn=str(turn.pk), seq=row.seq, text=row.text, ended=row.ended,
        error=row.error, now=now)


def _post(url: str, token: str) -> int:
    outbound.check_url(url, what="reply webhook URL")
    resp = requests.post(url, data=token.encode(), timeout=TIMEOUT_S, allow_redirects=False,
                         headers={"Content-Type": wire.REPLY_CONTENT_TYPE})
    return resp.status_code


def _mark_latency(turn_id, *, posted, acknowledged) -> None:
    try:
        latency.mark(turn_id, reply_posted=posted, site_acknowledged=acknowledged)
    except Exception:  # noqa: BLE001 — a trace never costs a delivery
        logger.debug("reply latency mark failed for turn %s", turn_id, exc_info=True)


def attempt(row: HostReplyDelivery, *, now: dt.datetime | None = None, post=None) -> None:
    """One delivery attempt: signed afresh, POSTed, and what it answered recorded —
    delivered, rescheduled by `BACKOFF`, or given up on after `MAX_ATTEMPTS`."""
    now = now or timezone.now()
    with transaction.atomic():
        row = (HostReplyDelivery.objects.select_for_update(of=("self",))
               .select_related("app__reply_webhook_set_by", "turn__chat_session__contact").get(pk=row.pk))
        if row.delivered_at or row.abandoned_at or row.next_attempt_at > now:
            return
        url = _url_of(row.app) if row.app.revoked_at is None else ""
        try:
            if not url:
                raise outbound.OutboundError("the site no longer takes reply push")
            status = (post or _post)(url, sign(row, now=now.timestamp(), url=url))
            ok, note = 200 <= status < 300, f"HTTP {status}"
            if ok and row.ended:
                # The reply's arrival at the site: when canopy began the POST that the
                # site took, and when it answered (`harness.latency`).
                _mark_latency(row.turn_id, posted=now, acknowledged=timezone.now())
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
