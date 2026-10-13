# apps/mail/sns.py
"""SNS HTTPS-delivery verification. Verified BEFORE any field is trusted.

https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""
from __future__ import annotations

import base64
import re
import urllib.request
from functools import lru_cache

_SNS_URL = r"https://sns\.[a-z0-9-]+\.amazonaws\.com/"
_CERT_URL = re.compile(_SNS_URL + r"[^?#]+\.pem$")
_SUBSCRIBE_URL = re.compile(_SNS_URL)
_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                 "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                "TopicArn", "Type"],
}


class Invalid(Exception):  # noqa: N818 - name is mandated by the mail spec
    pass


@lru_cache(maxsize=8)
def _fetch_cert(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - host pinned by _CERT_URL
        return r.read()


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5):  # noqa: S310 - host pinned in confirm()
        pass


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
    if not isinstance(url, str) or not _CERT_URL.match(url):
        raise Invalid("signing cert is not hosted by SNS")
    algo = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(msg.get("SignatureVersion")))  # noqa: S303
    if algo is None:
        raise Invalid("unsupported signature version")
    try:
        cert = x509.load_pem_x509_certificate(_fetch_cert(url))
        cert.public_key().verify(base64.b64decode(msg.get("Signature", "")),
                                 _string_to_sign(msg), padding.PKCS1v15(), algo)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise Invalid("bad signature") from exc


def confirm(msg: dict) -> None:
    url = msg.get("SubscribeURL", "")
    if not isinstance(url, str) or not _SUBSCRIBE_URL.match(url):
        raise Invalid("subscribe URL is not SNS")
    _get(url)
