"""The ONE module that calls AWS for mail. Everything else talks to these functions,
so tests stub one seam and nothing else in the app knows boto3 exists.

Credentials come from the ECS task role (deploy/aws/canopy-mail.cfn.yaml).
"""
from __future__ import annotations

import re
from urllib.parse import quote

from django.conf import settings


class MailOff(Exception):  # noqa: N818 - name is mandated by the mail spec
    """MAIL_ENABLED is false: nothing was sent, created or read."""


def _guard() -> None:
    if not settings.MAIL_ENABLED:
        raise MailOff("mail domains are off on this deployment (MAIL_ENABLED)")


def _sesv2():
    import boto3

    return boto3.client("sesv2", region_name=settings.MAIL_SES_REGION)


def _s3():
    import boto3

    return boto3.client("s3", region_name=settings.MAIL_SES_REGION)


def create_identity(domain: str, *, mail_from: str) -> list[str]:
    _guard()
    out = _sesv2().create_email_identity(
        EmailIdentity=domain, ConfigurationSetName=settings.MAIL_CONFIGURATION_SET)
    _sesv2().put_email_identity_mail_from_attributes(
        EmailIdentity=domain, MailFromDomain=mail_from, BehaviorOnMxFailure="USE_DEFAULT_VALUE")
    return list((out.get("DkimAttributes") or {}).get("Tokens") or [])


def identity_verified(domain: str) -> bool:
    _guard()
    out = _sesv2().get_email_identity(EmailIdentity=domain)
    return (out.get("DkimAttributes") or {}).get("Status") == "SUCCESS"


def delete_identity(domain: str) -> None:
    _guard()
    _sesv2().delete_email_identity(EmailIdentity=domain)


def send_raw(*, from_addr: str, to: list[str], raw: bytes) -> str:
    _guard()
    out = _sesv2().send_email(
        FromEmailAddress=from_addr,
        Destination={"ToAddresses": list(to)},
        Content={"Raw": {"Data": raw}},
        ConfigurationSetName=settings.MAIL_CONFIGURATION_SET,
    )
    return out["MessageId"]


def get_object(key: str) -> bytes:
    _guard()
    return _s3().get_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=key)["Body"].read()


def keep(key: str) -> str:
    """Copy an `incoming/` object to `kept/` (no lifecycle expiry). Returns the new key."""
    _guard()
    new = "kept/" + key.split("/", 1)[-1]
    _s3().copy_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=new,
                      CopySource={"Bucket": settings.MAIL_INBOUND_BUCKET, "Key": key})
    return new


def put_object(key: str, data: bytes, content_type: str) -> None:
    _guard()
    _s3().put_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=key, Body=data,
                     ContentType=content_type or "application/octet-stream")


def _disposition(filename: str) -> str:
    """Content-Disposition for a download. `filename` comes from inbound mail, so it is
    attacker-controlled: the ASCII fallback drops anything that could break out of the
    quoted header, and filename* carries the real name percent-encoded."""
    name = filename or "attachment"
    fallback = re.sub(r'["\\]|[^\x20-\x7e]', "_", name) or "attachment"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


def presign(key: str, filename: str) -> str:
    _guard()
    return _s3().generate_presigned_url(
        "get_object", ExpiresIn=300,
        Params={"Bucket": settings.MAIL_INBOUND_BUCKET, "Key": key,
                "ResponseContentDisposition": _disposition(filename)})
