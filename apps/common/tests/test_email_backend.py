"""The SES backend hands SES a raw MIME message tagged with the configuration
set, and the not-configured backend reports nothing sent (never a false yes)."""
from __future__ import annotations

from django.core.mail import EmailMessage
from django.test import override_settings

from apps.common.email import NotConfiguredEmailBackend, SesEmailBackend


class _FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "m-1"}


@override_settings(CANOPY_SES_CONFIGURATION_SET="labs-jj-email")
def test_ses_backend_sends_raw_mime_with_the_configuration_set():
    backend = SesEmailBackend()
    fake = _FakeSes()
    backend._client = fake
    msg = EmailMessage("Hello", "Body", "Canopy <noreply@labs.example>", ["b@partner.org"])

    assert backend.send_messages([msg]) == 1

    (call,) = fake.calls
    assert call["ConfigurationSetName"] == "labs-jj-email"
    assert call["Destination"] == {"ToAddresses": ["b@partner.org"]}
    assert call["FromEmailAddress"] == "Canopy <noreply@labs.example>"
    assert b"Subject: Hello" in call["Content"]["Raw"]["Data"]


def test_not_configured_backend_reports_nothing_sent():
    msg = EmailMessage("Hello", "Body", "x@y", ["b@partner.org"])
    assert NotConfiguredEmailBackend().send_messages([msg]) == 0
