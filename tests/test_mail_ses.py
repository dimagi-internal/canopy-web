# tests/test_mail_ses.py
from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from apps.mail import ses


@pytest.fixture()
def on(settings):
    settings.MAIL_ENABLED = True
    settings.MAIL_INBOUND_BUCKET = "canopy-inbound-mail-test"
    settings.MAIL_CONFIGURATION_SET = "canopy-tenant-mail"


@pytest.fixture()
def sesv2(monkeypatch):
    client = boto3.client("sesv2", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(ses, "_sesv2", lambda: client)
    with Stubber(client) as stub:
        yield stub


def test_everything_refuses_when_mail_is_off(settings):
    settings.MAIL_ENABLED = False
    with pytest.raises(ses.MailOff):
        ses.send_raw(from_addr="a@b.c", to=["d@e.f"], raw=b"x")


def test_create_identity_returns_dkim_tokens_and_sets_mail_from(on, sesv2):
    sesv2.add_response("create_email_identity",
                       {"IdentityType": "DOMAIN", "VerifiedForSendingStatus": False,
                        "DkimAttributes": {"Tokens": ["t1", "t2", "t3"]}},
                       {"EmailIdentity": "marshellis.com", "ConfigurationSetName": "canopy-tenant-mail"})
    sesv2.add_response("put_email_identity_mail_from_attributes", {},
                       {"EmailIdentity": "marshellis.com", "MailFromDomain": "bounce.marshellis.com",
                        "BehaviorOnMxFailure": "USE_DEFAULT_VALUE"})
    assert ses.create_identity("marshellis.com", mail_from="bounce.marshellis.com") == ["t1", "t2", "t3"]


def test_send_raw_always_carries_the_tenant_configuration_set(on, sesv2):
    sesv2.add_response("send_email", {"MessageId": "ses-1"},
                       {"FromEmailAddress": "ada@marshellis.com",
                        "Destination": {"ToAddresses": ["x@y.z"]},
                        "Content": {"Raw": {"Data": b"raw"}},
                        "ConfigurationSetName": "canopy-tenant-mail"})
    assert ses.send_raw(from_addr="ada@marshellis.com", to=["x@y.z"], raw=b"raw") == "ses-1"


def test_identity_verified_reads_dkim_status(on, sesv2):
    sesv2.add_response("get_email_identity",
                       {"DkimAttributes": {"Status": "SUCCESS"}, "VerifiedForSendingStatus": True},
                       {"EmailIdentity": "marshellis.com"})
    assert ses.identity_verified("marshellis.com") is True
