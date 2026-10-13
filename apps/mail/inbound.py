# apps/mail/inbound.py
"""One SES receipt notification → one outcome per recipient.

Order matters: verdicts first (spam/virus never reach a person or an agent),
then each recipient independently through domains.resolve.

Retry discipline (SNS retries any 5xx for ~6 h): only TRANSIENT failures may
surface as `Transient`; a deterministic bug is recorded as `error:<Type>` and
the notification is acknowledged. Deliver/forward dedupe makes redelivery safe
for the recipients that already succeeded.
"""
from __future__ import annotations

import logging

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


def deliver_agent(route, parsed, raw_key: str) -> str:
    from . import agent_mail

    return agent_mail.deliver(route, parsed, raw_key)


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


def _route_one(rcpt: str, parsed, raw: bytes, key: str) -> str:
    domain, route = domains.resolve(rcpt)
    if domain is None:
        return "dropped:unknown-domain"
    if route is None:
        return "dropped:no-route"
    if route.kind == MailRoute.AGENT:
        return deliver_agent(route, parsed, key)
    if route.kind == MailRoute.FORWARD:
        return deliver_forward(route, parsed, raw, rcpt)
    return "dropped:drop-route"


def handle(notification: dict) -> list[dict]:
    receipt = notification.get("receipt") or {}
    recipients = [r.lower() for r in receipt.get("recipients") or []]
    bad = [k for k in ("spamVerdict", "virusVerdict")
           if (receipt.get(k) or {}).get("status") == "FAIL"]
    if bad:
        reason = "spam" if "spamVerdict" in bad else "virus"
        logger.info("mail dropped (%s) for %s", reason, recipients)
        return [{"recipient": r, "outcome": f"dropped:{reason}"} for r in recipients]
    key = _object_key(notification)
    try:
        raw = ses.get_object(key)
    except ses.MailOff:
        logger.warning("mail inbound ignored: MAIL_ENABLED is off")
        return [{"recipient": r, "outcome": "dropped:mail-off"} for r in recipients]
    parsed = parse.parse(raw)
    out: list[dict] = []
    transient = False
    for rcpt in recipients:
        try:
            outcome = _route_one(rcpt, parsed, raw, key)
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
