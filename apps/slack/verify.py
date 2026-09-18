"""Slack request signing (ported from ace-web ``apps/slack/verify.py``).

Slack signs every request with ``v0=hmac_sha256(secret, "v0:" + ts + ":" + body)``.
The timestamp bounds replay: anything more than five minutes old is refused.
"""
from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    pass


MAX_AGE_SECONDS = 5 * 60


def verify_slack_signature(*, secret: str, body: bytes, timestamp: str, signature: str,
                           now: float | None = None) -> None:
    if not secret:
        raise SignatureError("no signing secret configured")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("bad timestamp") from None
    if abs((now if now is not None else time.time()) - ts) > MAX_AGE_SECONDS:
        raise SignatureError("stale timestamp")
    base = b"v0:" + str(ts).encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise SignatureError("signature mismatch")


def sign(*, secret: str, body: bytes, timestamp: str) -> str:
    """The signature Slack would send. Used by tests and the e2e script."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
