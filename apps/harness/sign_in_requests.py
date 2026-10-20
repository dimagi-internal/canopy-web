"""A runner asks its owner to approve a cloud sign-in from their phone.

An agent turn on a box hits an expired AWS SSO session and stops. Before this
it could only say "run `aws sso login --profile labs`" and wait for somebody to
reach a terminal on that box. AWS's device-code flow does not need the
terminal: the CLI on the box prints a URL with the code already filled in, and
anyone holding that URL approves the sign-in from any browser, a phone included,
while the CLI waits. So the box runs the real CLI, and this module carries its
URL to the owner as a push notification. Tapping the notification opens the AWS
approval page and the CLI on the box finishes on its own.

The AWS credential never leaves the box. What passes through is a URL whose
code is single-use and expires in about ten minutes, and canopy-web keeps it
for exactly that long as a `SignInRequest` row, so the supervisor feed can show
the owner every request still open. A push the push service accepted is not a
push the phone showed (2026-10-19), and before the row a missed notification
left the owner no other way to find out a box was waiting. The box reports the
outcome (`resolve`), which clears the URL; a box that never reports leaves a
row that reads as expired once the code would have.

Who it reaches. The push goes to the runner's OWNER and nobody else. The route
is gated `runner`, which means the caller already holds the owner's token. So
the only person who can put an approval request in front of the owner is one
who can already act as the owner. That is the bound that matters for a device
code: approving one grants AWS access to whoever STARTED it, so a phone
notification anyone could trigger would be a phishing channel with canopy's
name on it.

What it opens. The host must be an AWS device-authorization page. The push URL
is opened by the service worker without the person first seeing it, so a free
URL here would be an open redirect delivered to a lock screen.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.utils import timezone

from .models import SignInRequest

logger = logging.getLogger(__name__)

# IAM Identity Center's two device pages: the org's own start portal
# (`<org>.awsapps.com/start/#/device?user_code=…`, what `aws sso login
# --use-device-code` prints for an sso-session profile) and the regional OIDC
# page (`device.sso.<region>.amazonaws.com/?user_code=…`).
_AWS_DEVICE_HOSTS = (
    re.compile(r"^[a-z0-9][a-z0-9-]*\.awsapps\.com$"),
    re.compile(r"^device\.sso\.[a-z0-9-]+\.amazonaws\.com$"),
)

PROVIDERS = ("aws",)
# AWS device codes expire after about ten minutes.
SIGN_IN_TTL = 600
# How long a resolved or expired request is kept as the audit line.
KEEP_DAYS = 30


class SignInRequestError(ValueError):
    """The request names a URL this route will not put on someone's phone."""


def validate_url(provider: str, url: str) -> str:
    if provider not in PROVIDERS:
        raise SignInRequestError(f"unknown provider {provider!r}; supported: {', '.join(PROVIDERS)}")
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SignInRequestError("a sign-in URL must be an https URL")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise SignInRequestError("a sign-in URL must not carry credentials or a port")
    host = parsed.hostname.lower()
    if not any(p.match(host) for p in _AWS_DEVICE_HOSTS):
        raise SignInRequestError(f"{host} is not an AWS sign-in host")
    return url


def push_sign_in_request(runner, *, provider: str, url: str, label: str = "",
                         requested_by: str = "", reason: str = "") -> tuple[SignInRequest | None, int]:
    """Record the request and push the approval link to the runner's owner.
    Returns the row (None for a runner with no owner) and the number of devices
    the push reached. 0 means no phone was sent it, and the caller has to treat
    that as "fall back to another channel", not as success: the owner may not
    have enabled notifications on any device, or push may not be configured on
    this deployment. The row is on the supervisor feed either way.

    `reason` is required. The notification must say who is asking, on which
    box, and why, so the owner can decide from the lock screen without opening
    the session to find out what an approval is for (Jonathan, 2026-10-09). A
    credential grant with no stated purpose is also the shape a person should
    learn to refuse.
    """
    from apps.push.services import send_to_user

    url = validate_url(provider, url)
    reason = " ".join((reason or "").split())[:200]
    if not reason:
        raise SignInRequestError("a sign-in request must say why it is needed (reason)")
    owner = runner.owner
    if owner is None:
        return None, 0
    label = (label or "").strip()[:60]
    who = (requested_by or "").strip()[:60] or "An agent"
    what = f"AWS sign-in ({label})" if label else "AWS sign-in"
    title = f"{who} needs {what}"
    body = f"On {runner.name}: {reason}. Tap to approve. The link expires in about 10 minutes."
    # High urgency wakes a dozing phone; the TTL matches the device code's own
    # life, since an approval link delivered after it expires is useless.
    now = timezone.now()
    SignInRequest.objects.filter(runner=runner, created_at__lt=now - timedelta(days=KEEP_DAYS)).delete()
    row = SignInRequest.objects.create(
        runner=runner, owner=owner, provider=provider, url=url, label=label,
        requested_by=who, reason=reason, expires_at=now + timedelta(seconds=SIGN_IN_TTL))
    sent = send_to_user(owner, title=title, body=body, url=url,
                        ttl=SIGN_IN_TTL, urgency="high")
    SignInRequest.objects.filter(pk=row.pk).update(sent=sent)
    row.sent = sent
    # The audit trail for "who put an approval in front of me": an approved
    # device code is a credential grant, so every request is logged whether or
    # not it reached a device.
    logger.info("sign-in request: id=%s runner=%s owner=%s provider=%s label=%r by=%r reason=%r sent=%d",
                row.pk, runner.pk, owner.pk, provider, label, who, reason, sent)
    return row, sent


def resolve(runner, request_id, outcome: str) -> SignInRequest | None:
    """The box reports how its sign-in ended. Clears the URL, since the code in
    it is spent or dead either way. None if this runner has no such request.
    Reporting twice keeps the first outcome."""
    if outcome not in SignInRequest.OUTCOMES:
        raise SignInRequestError(f"unknown outcome {outcome!r}; one of: {', '.join(SignInRequest.OUTCOMES)}")
    row = SignInRequest.objects.filter(runner=runner, pk=request_id).first()
    if row is None:
        return None
    if row.status == SignInRequest.PENDING:
        row.status, row.url, row.resolved_at = outcome, "", timezone.now()
        row.save(update_fields=["status", "url", "resolved_at"])
        logger.info("sign-in request resolved: id=%s runner=%s outcome=%s", row.pk, runner.pk, outcome)
    return row


def open_for(user) -> list[SignInRequest]:
    """The requests still waiting on `user`, newest first. A pending row past
    its expiry is closed here, URL and all: its box stopped waiting, or died
    before it could say so."""
    now = timezone.now()
    SignInRequest.objects.filter(owner=user, status=SignInRequest.PENDING, expires_at__lte=now).update(
        status=SignInRequest.EXPIRED, url="", resolved_at=now)
    # Still the runner's owner too: a box handed to someone else stops putting
    # its old requests in front of the person it left.
    return list(SignInRequest.objects.filter(owner=user, runner__owner=user, status=SignInRequest.PENDING)
                .select_related("runner"))
