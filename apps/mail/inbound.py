# apps/mail/inbound.py
"""One SES receipt notification → one outcome per recipient.

Order matters: verdicts first (spam/virus never reach a person or an agent),
then each recipient independently through domains.resolve.

Retry discipline (SNS retries any 5xx for ~40 min): only TRANSIENT failures may
surface as `Transient`; a deterministic bug is recorded as `error:<Type>` and
the notification is acknowledged. Deliver/forward dedupe makes redelivery safe
for the recipients that already succeeded.
"""
from __future__ import annotations

import logging
from email.utils import getaddresses

from botocore.exceptions import BotoCoreError, ClientError
from django.db import InterfaceError, OperationalError

from . import domains, parse, ses
from .models import MailRoute

logger = logging.getLogger(__name__)

TRANSIENT = (ClientError, BotoCoreError, OperationalError, InterfaceError)


class Transient(Exception):  # noqa: N818 - carries the outcomes of a notification to redeliver
    def __init__(self, outcomes: list[dict]):
        super().__init__("transient failure; redeliver")
        self.outcomes = outcomes


def deliver_agent(route, parsed, raw_key: str, *, verdicts: dict | None = None) -> str:
    from . import agent_mail

    return agent_mail.deliver(route, parsed, raw_key, verdicts=verdicts)


def deliver_forward(route, parsed, raw: bytes, recipient: str) -> str:
    from . import forward

    return forward.forward(route, parsed, raw, recipient)


def _object_key(notification: dict) -> str:
    action = (notification.get("receipt") or {}).get("action") or {}
    if action.get("objectKey"):
        return action["objectKey"]
    prefix = action.get("objectKeyPrefix") or ""
    message_id = (notification.get("mail") or {}).get("messageId") or ""
    return f"{prefix}{message_id}" if message_id else ""


_VERDICTS = {"spf": "spfVerdict", "dkim": "dkimVerdict", "dmarc": "dmarcVerdict",
             "spam": "spamVerdict", "virus": "virusVerdict"}


def receipt_verdicts(receipt: dict) -> dict[str, str]:
    """The receipt's verdict statuses, lowercase name → status (only those present)."""
    out = {}
    for name, k in _VERDICTS.items():
        status = (receipt.get(k) or {}).get("status")
        if isinstance(status, str) and status:
            out[name] = status[:32]
    return out


def plan(rcpt: str) -> tuple[str, MailRoute | None]:
    """Where one recipient's copy goes: (kind, route) for a live agent/forward route,
    else (`dropped:<why>`, None). Shared by delivery and the replay dry run."""
    domain, route = domains.resolve(rcpt)
    if domain is None:
        return "dropped:unknown-domain", None
    if route is None:
        return "dropped:no-route", None
    if route.kind in (MailRoute.AGENT, MailRoute.FORWARD):
        return route.kind, route
    return "dropped:drop-route", None


def _route_one(rcpt: str, parsed, raw: bytes, key: str, verdicts: dict) -> str:
    kind, route = plan(rcpt)
    if route is None:
        return kind
    if kind == MailRoute.AGENT:
        return deliver_agent(route, parsed, key, verdicts=verdicts)
    return deliver_forward(route, parsed, raw, rcpt)


def valid_key(key: str) -> bool:
    """Only SES's own `incoming/` objects are ever read: never `kept/`, never a traversal."""
    return key.startswith("incoming/") and ".." not in key


def blocked(verdicts: dict) -> str | None:
    """`spam` or `virus` when SES failed the message: it never reaches a person or an agent."""
    if verdicts.get("spam") == "FAIL":
        return "spam"
    if verdicts.get("virus") == "FAIL":
        return "virus"
    return None


def deliver(parsed, raw: bytes, key: str, recipients: list[str], *, verdicts: dict) -> list[dict]:
    """One parsed message → one outcome per recipient, each isolated from the others.
    Raises Transient (carrying every outcome) if any recipient failed transiently."""
    out: list[dict] = []
    transient = False
    for rcpt in recipients:
        try:
            outcome = _route_one(rcpt, parsed, raw, key, verdicts)
        except ses.MailOff:
            outcome = "dropped:mail-off"
        except TRANSIENT as exc:
            logger.warning("mail %s -> %s transient failure: %r", parsed.message_id, rcpt, exc)
            outcome, transient = f"error:{type(exc).__name__}", True
        except Exception as exc:  # noqa: BLE001 - isolate one recipient from the rest
            logger.exception("mail %s -> %s failed", parsed.message_id, rcpt)
            outcome = f"error:{type(exc).__name__}"
        out.append({"recipient": rcpt, "outcome": outcome})
    logger.info("mail %s: %s", parsed.message_id, out)
    if transient:
        raise Transient(out)
    return out


def _unique(addrs) -> list[str]:
    return list(dict.fromkeys(a.strip().lower() for a in addrs if a and a.strip()))


def handle(notification: dict) -> list[dict]:
    receipt = notification.get("receipt") or {}
    recipients = _unique(receipt.get("recipients") or [])
    kind = notification.get("notificationType")
    if (kind is not None and kind != "Received") or not recipients:
        return []
    verdicts = receipt_verdicts(receipt)
    reason = blocked(verdicts)
    if reason:
        logger.info("mail dropped (%s) for %s", reason, recipients)
        return [{"recipient": r, "outcome": f"dropped:{reason}"} for r in recipients]
    key = _object_key(notification)
    if not key:
        return []
    if not valid_key(key):
        logger.warning("mail inbound refused object key %r", key)
        return [{"recipient": r, "outcome": "dropped:bad-key"} for r in recipients]
    try:
        raw = ses.get_object(key)
    except ses.MailOff:
        logger.warning("mail inbound ignored: MAIL_ENABLED is off")
        return [{"recipient": r, "outcome": "dropped:mail-off"} for r in recipients]
    return deliver(parse.parse(raw), raw, key, recipients, verdicts=verdicts)


# --- Replay (manage.py mail_replay): the same path, from an object still in S3.

_HEADER_VERDICTS = {"spam": "x-ses-spam-verdict", "virus": "x-ses-virus-verdict"}


def header_verdicts(parsed) -> dict[str, str]:
    """The spam/virus verdicts SES writes into the stored message's headers (the receipt
    notification, which also carried spf/dkim/dmarc, is gone by the time of a replay)."""
    found: dict[str, str] = {}
    for k, v in parsed.headers:
        found.setdefault(k.lower(), v)  # the FIRST: SES prepends its own; a sender's copy sits below
    return {name: found[h].strip()[:32] for name, h in _HEADER_VERDICTS.items() if found.get(h, "").strip()}


def default_recipients(parsed) -> list[str]:
    """To, Cc and Delivered-To addresses on one of our mail domains (Bcc recipients are not
    in the message: name them explicitly)."""
    delivered = [a for _, a in getaddresses([v for k, v in parsed.headers if k.lower() == "delivered-to"])]
    return [a for a in _unique([*parsed.to, *parsed.cc, *delivered])
            if "@" in a and domains._domain_for(a.rpartition("@")[2]) is not None]


def _clear_dropped_forward(rcpt: str, parsed) -> bool:
    """Remove a DROPPED forward claim (never a FORWARDED one) so a replay can forward it."""
    from .models import MailForwardLog

    kind, route = plan(rcpt)
    if kind != MailRoute.FORWARD:
        return False
    n, _ = MailForwardLog.objects.filter(domain=route.domain, message_id=parsed.message_id, recipient=rcpt,
                                         status=MailForwardLog.DROPPED).delete()
    return bool(n)


def replay(key: str, recipients: list[str] | None = None, *, dry_run: bool = False,
           reforward: bool = False) -> list[dict]:
    """Re-handle a raw `incoming/` object. Raises ValueError for a refused key or no
    recipients, ses.MailOff when mail is off, and Transient like `deliver`. With
    `reforward`, a forward that was logged as DROPPED is cleared first so it is retried;
    an already-FORWARDED copy is never sent again."""
    if not valid_key(key):
        raise ValueError(f"refused key {key!r}: only incoming/ objects, no '..'")
    raw = ses.get_object(key)
    parsed = parse.parse(raw)
    rcpts = _unique(recipients or []) or default_recipients(parsed)
    if not rcpts:
        raise ValueError("no recipient on one of our domains: name one with --recipient")
    verdicts = header_verdicts(parsed)
    reason = blocked(verdicts)
    if reason:
        return [{"recipient": r, "outcome": f"dropped:{reason}"} for r in rcpts]
    if reforward and not dry_run:
        for r in rcpts:
            if _clear_dropped_forward(r, parsed):
                logger.info("mail replay cleared the dropped forward of %s to %s", parsed.message_id, r)
    if dry_run:
        out = []
        for r in rcpts:
            kind, route = plan(r)
            target = "" if route is None else (
                f" {route.agent.slug}" if kind == MailRoute.AGENT else f" to {route.forward_to.email}")
            out.append({"recipient": r, "outcome": f"would:{kind}{target}"})
        return out
    return deliver(parsed, raw, key, rcpts, verdicts=verdicts)
