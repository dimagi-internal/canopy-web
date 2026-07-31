"""Django Ninja router for /api/inbound — the push doorbell.

``auth=None`` and self-enforcing, following ``POST /api/auth/token-exchange``
exactly: that route is the precedent for an unauthenticated endpoint that
verifies a credential itself and is explicitly allowlisted in
``apps/common/middleware.py``.

Two response rules that look odd and are deliberate:

* An unverified push gets **404, not 403**. A probe learns nothing about whether
  the endpoint exists — the same rule the storyboard and walkthrough token gates
  follow.
* A push we cannot act on (unknown mailbox, nobody online) still gets **200**. A
  4xx tells Pub/Sub to REDELIVER, so a mailbox we do not own would be retried
  forever. The refusal belongs in the event log, where someone can see it, not
  on the wire where it becomes a retry storm.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.inbound import services
from apps.inbound.schemas import PushEnvelopeIn, PushResultOut
from apps.inbound.verify import VerificationError, verify_push

logger = logging.getLogger(__name__)

router = Router(tags=["inbound"])


def _decode(message: dict) -> dict:
    """Pull ``{emailAddress, historyId}`` out of a Pub/Sub message.

    The payload is base64 in ``message.data``. A malformed one is not an error
    worth raising: Pub/Sub would redeliver it forever, and there is nothing to
    fix on our side.
    """
    raw = message.get("data") or ""
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {}
    try:
        parsed = json.loads(decoded)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.post(
    "/gmail/",
    response=PushResultOut,
    auth=None,
    summary="Gmail Pub/Sub push (doorbell — carries no mail)",
)
def gmail_push(request: HttpRequest, payload: PushEnvelopeIn) -> dict:
    """Ring the runner that holds this mailbox's credentials.

    This endpoint never reads mail. A Gmail notification carries
    ``{emailAddress, historyId}`` and no content, so the runner — which owns the
    per-agent ``gog`` OAuth clients — does the read it already does on its poll.
    """
    try:
        verify_push(request)
    except VerificationError as exc:
        logger.warning("inbound gmail push refused: %s", exc)
        raise HttpError(404, "not found") from None

    body = _decode(payload.message or {})
    address = body.get("emailAddress") or ""
    if not address:
        logger.warning("inbound gmail push carried no emailAddress")
        return {"ok": False, "reason": "no_address", "rang": []}

    result = services.handle_push(address, str(body.get("historyId") or ""))
    return {
        "ok": bool(result.get("ok")),
        "reason": result.get("reason", ""),
        "rang": result.get("rang", []),
    }
