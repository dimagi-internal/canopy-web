# apps/mail/sns.py
"""SNS HTTPS-delivery verification. Verified BEFORE any field is trusted.

https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""
from __future__ import annotations

import base64
import http.client
import logging
import re
import ssl
import urllib.error
import urllib.request
from functools import lru_cache
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_CERT_PATH = re.compile(r"/SimpleNotificationService-[0-9a-f]+\.pem")
_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                 "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                "TopicArn", "Type"],
}


class Invalid(Exception):  # noqa: N818 - name is mandated by the mail spec
    pass


class Unavailable(Exception):  # noqa: N818 - a transient failure: the caller answers 500 so SNS retries
    pass


def _classify(exc: BaseException, what: str) -> Exception:
    """An HTTP 4xx is the sender's fault (an attacker-chosen path): Invalid. Anything else
    — 5xx, a network error, a timeout, TLS — is transient: Unavailable, so SNS retries."""
    if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
        return Invalid(f"{what}: HTTP {exc.code}")
    return Unavailable(f"{what}: {type(exc).__name__}")


_TRANSIENT = (OSError, ssl.SSLError, http.client.HTTPException)  # URLError/HTTPError/TimeoutError are OSErrors


@lru_cache(maxsize=8)
def _fetch_cert(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - host pinned by _sns_url in verify()
        return r.read()


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5):  # noqa: S310 - host pinned in confirm()
        pass


def _region(topic_arn: str) -> str:
    parts = topic_arn.split(":")
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "sns" or not re.fullmatch(r"[a-z0-9-]+", parts[3]):
        raise Invalid("malformed topic ARN")
    return parts[3]


def _sns_url(url, topic_arn: str, *, path_ok, query_ok: bool = False) -> None:
    """Exact host rule: https://sns.<topic region>.amazonaws.com, no userinfo/port/query/fragment."""
    if not isinstance(url, str):
        raise Invalid("not a URL")
    if not url.isprintable() or any(c.isspace() for c in url):  # urlsplit silently strips \n, \t
        raise Invalid("control characters in URL")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError as exc:
        raise Invalid("malformed URL") from exc
    if (u.scheme != "https" or u.hostname != f"sns.{_region(topic_arn)}.amazonaws.com"
            or u.username is not None or u.password is not None or port is not None
            or (u.query and not query_ok) or u.fragment or "@" in u.netloc or not path_ok(u.path)):
        raise Invalid("URL is not the pinned SNS endpoint")


def _string_to_sign(msg: dict) -> bytes:
    keys = _FIELDS.get(msg.get("Type", ""))
    if keys is None:
        raise Invalid("unknown SNS message type")
    return "".join(f"{k}\n{msg[k]}\n" for k in keys if k in msg).encode()


def verify(msg: dict, *, topic_arn: str) -> None:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if not isinstance(msg, dict):
        raise Invalid("not an SNS message")
    if not topic_arn or msg.get("TopicArn") != topic_arn:
        raise Invalid("not our topic")
    url = msg.get("SigningCertURL", "")
    _sns_url(url, topic_arn, path_ok=lambda p: bool(_CERT_PATH.fullmatch(p)))
    algo = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(msg.get("SignatureVersion")))  # noqa: S303
    if algo is None:
        raise Invalid("unsupported signature version")
    try:
        pem = _fetch_cert(url)
    except _TRANSIENT as exc:
        logger.warning("could not fetch SNS signing cert %s: %r", url, exc)
        raise _classify(exc, "signing cert") from exc
    try:
        cert = x509.load_pem_x509_certificate(pem)
        cert.public_key().verify(base64.b64decode(msg.get("Signature", "")),
                                 _string_to_sign(msg), padding.PKCS1v15(), algo)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise Invalid("bad signature") from exc


def confirm(msg: dict) -> None:
    url = msg.get("SubscribeURL", "")
    _sns_url(url, msg.get("TopicArn", ""), path_ok=lambda p: p.startswith("/"), query_ok=True)
    try:
        _get(url)
    except _TRANSIENT as exc:
        logger.warning("could not confirm SNS subscription: %r", exc)
        raise _classify(exc, "subscribe") from exc
