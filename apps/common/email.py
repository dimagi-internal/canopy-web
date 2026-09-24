"""Outbound email: Django email backends for Amazon SES, and for "not configured".

canopy sends through the labs account's SES identity (`labs.connect.dimagi.com`),
which connect-labs stood up (identity + DKIM, production access, the
`labs-jj-email` configuration set, and `ses:SendEmail` on the shared ECS task
role — connect-labs `infra/labs-email.yml`). Nothing here holds a key: boto3
takes credentials from the task role.

Talks to SESv2 directly rather than through django-anymail: boto3 is already a
dependency and a raw send is one call, so a second package buys nothing.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class SesEmailBackend(BaseEmailBackend):
    """Sends each message as raw MIME via `sesv2.send_email`.

    Every send carries the configuration set, so SES routes its bounce,
    complaint and delivery-delay events somewhere a human can see. A send
    without it is invisible, and an unwatched bounce rate is how a sending
    domain's reputation dies.
    """

    _client = None

    def _ses(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("sesv2", region_name=settings.CANOPY_SES_REGION)
        return self._client

    def send_messages(self, email_messages) -> int:
        sent = 0
        for message in email_messages or []:
            kwargs = {
                "FromEmailAddress": message.from_email,
                "Destination": {"ToAddresses": list(message.recipients())},
                "Content": {"Raw": {"Data": message.message().as_bytes()}},
            }
            if settings.CANOPY_SES_CONFIGURATION_SET:
                kwargs["ConfigurationSetName"] = settings.CANOPY_SES_CONFIGURATION_SET
            # Raises on failure: the caller decides what a failed send means
            # (services.email_invite logs it and reports "failed").
            self._ses().send_email(**kwargs)
            sent += 1
        return sent


class NotConfiguredEmailBackend(BaseEmailBackend):
    """Delivery is off: log it and report NOTHING sent.

    Deliberately not Django's console backend, which reports success for mail
    it threw away — a caller asking "did the invite go out?" must hear no.
    """

    def send_messages(self, email_messages) -> int:
        for message in email_messages or []:
            logger.warning(
                "email not sent (CANOPY_EMAIL_ENABLED is off): %r to %s",
                message.subject, ", ".join(message.recipients()),
            )
        return 0
