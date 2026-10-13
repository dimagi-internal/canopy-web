# tests/test_mail_ses.py
from __future__ import annotations

import io
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from apps.mail import ses


@pytest.fixture()
def on(settings):
    settings.MAIL_ENABLED = True
    settings.MAIL_INBOUND_BUCKET = "canopy-inbound-mail-test"
    settings.MAIL_CONFIGURATION_SET = "canopy-tenant-mail"
    settings.MAIL_SES_REGION = "us-east-1"


@pytest.fixture()
def sesv2(monkeypatch):
    client = boto3.client("sesv2", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(ses, "_sesv2", lambda: client)
    with Stubber(client) as stub:
        yield stub


@pytest.fixture()
def s3(monkeypatch):
    client = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(ses, "_s3", lambda: client)
    with Stubber(client) as stub:
        yield stub


@pytest.fixture()
def s3_signing(monkeypatch):
    """A real S3 client with dummy credentials: presigning is local, no network."""
    client = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(ses, "_s3", lambda: client)
    return client


# ---- MailOff: every public function refuses when mail is off ----------------

@pytest.mark.parametrize("call", [
    lambda: ses.create_identity("d.example", mail_from="bounce.d.example"),
    lambda: ses.identity_verified("d.example"),
    lambda: ses.delete_identity("d.example"),
    lambda: ses.send_raw(from_addr="a@b.c", to=["d@e.f"], raw=b"x"),
    lambda: ses.get_object("incoming/abc"),
    lambda: ses.keep("incoming/abc"),
    lambda: ses.put_object("kept/abc", b"x", "text/plain"),
    lambda: ses.presign("incoming/abc", "f.pdf"),
], ids=["create_identity", "identity_verified", "delete_identity", "send_raw",
        "get_object", "keep", "put_object", "presign"])
def test_everything_refuses_when_mail_is_off(settings, call):
    settings.MAIL_ENABLED = False
    with pytest.raises(ses.MailOff):
        call()


# ---- SES ---------------------------------------------------------------------

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


def test_delete_identity_deletes_the_sesv2_identity(on, sesv2):
    sesv2.add_response("delete_email_identity", {}, {"EmailIdentity": "marshellis.com"})
    ses.delete_identity("marshellis.com")


# ---- S3 ----------------------------------------------------------------------

def test_get_object_returns_the_body_bytes(on, s3):
    body = StreamingBody(io.BytesIO(b"hello"), 5)
    s3.add_response("get_object", {"Body": body},
                    {"Bucket": "canopy-inbound-mail-test", "Key": "incoming/abc"})
    assert ses.get_object("incoming/abc") == b"hello"


def test_keep_copies_incoming_to_kept_and_returns_the_new_key(on, s3):
    s3.add_response("copy_object", {},
                    {"Bucket": "canopy-inbound-mail-test", "Key": "kept/abc",
                     "CopySource": {"Bucket": "canopy-inbound-mail-test", "Key": "incoming/abc"}})
    assert ses.keep("incoming/abc") == "kept/abc"


def test_put_object_falls_back_to_octet_stream_when_no_content_type(on, s3):
    s3.add_response("put_object", {},
                    {"Bucket": "canopy-inbound-mail-test", "Key": "kept/abc", "Body": b"x",
                     "ContentType": "application/octet-stream"})
    ses.put_object("kept/abc", b"x", "")


def _disposition_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["response-content-disposition"][0]


def test_presign_sanitises_an_attacker_controlled_filename(on, s3_signing):
    url = ses.presign("incoming/abc", 'a"b\r\nc é.pdf')
    assert _disposition_of(url) == (
        "attachment; filename=\"a_b__c _.pdf\"; "
        "filename*=UTF-8''a%22b%0D%0Ac%20%C3%A9.pdf"
    )


def test_presign_empty_filename_falls_back_to_attachment(on, s3_signing):
    url = ses.presign("incoming/abc", "")
    assert _disposition_of(url) == 'attachment; filename="attachment"; filename*=UTF-8\'\'attachment'
