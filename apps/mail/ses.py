"""The ONE module that calls AWS for mail. Everything else talks to these functions,
so tests stub one seam and nothing else in the app knows boto3 exists.

Credentials come from the ECS task role (deploy/aws/canopy-mail.cfn.yaml).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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


def _sesv2_send():
    """The client for sends only: agent_mail.send holds the domain row lock across the
    call, so it must fail fast rather than ride botocore's default 60 s timeouts and retries."""
    import boto3
    from botocore.config import Config

    return boto3.client("sesv2", region_name=settings.MAIL_SES_REGION,
                        config=Config(connect_timeout=5, read_timeout=20,
                                      retries={"max_attempts": 2, "mode": "standard"}))


def _s3():
    import boto3

    return boto3.client("s3", region_name=settings.MAIL_SES_REGION)


class IdentityExists(Exception):  # noqa: N818 - reads as the condition
    """SES already holds this identity (ours or someone else's). We never touch it."""


def create_identity(domain: str, *, mail_from: str) -> list[str]:
    """Create a NEW SES identity. One that already exists in the shared account is not
    ours to adopt or alter: IdentityExists is raised and nothing is changed."""
    from botocore.exceptions import ClientError

    _guard()
    try:
        out = _sesv2().create_email_identity(
            EmailIdentity=domain, ConfigurationSetName=settings.MAIL_CONFIGURATION_SET)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "AlreadyExistsException":
            raise IdentityExists(domain) from None
        raise
    try:
        _sesv2().put_email_identity_mail_from_attributes(
            EmailIdentity=domain, MailFromDomain=mail_from, BehaviorOnMxFailure="USE_DEFAULT_VALUE")
    except Exception:
        try:  # we created it a moment ago, so removing it is ours to do
            _sesv2().delete_email_identity(EmailIdentity=domain)
        except Exception:  # noqa: S110 - best effort; the original error is what matters
            pass
        raise
    return list((out.get("DkimAttributes") or {}).get("Tokens") or [])


@dataclass(frozen=True)
class IdentityStatus:
    """What SES says about one identity. `dkim` and `mail_from` are SES's own status words
    (PENDING, SUCCESS, FAILED, TEMPORARY_FAILURE, NOT_STARTED); "" when SES gave none."""
    dkim: str
    mail_from: str
    tokens: list[str]
    key_length: str = ""
    """Easy DKIM's NextSigningKeyLength (RSA_1024_BIT / RSA_2048_BIT), "" when SES gave none."""


def identity_status(domain: str) -> IdentityStatus:
    _guard()
    out = _sesv2().get_email_identity(EmailIdentity=domain)
    dkim = out.get("DkimAttributes") or {}
    mail_from = out.get("MailFromAttributes") or {}
    return IdentityStatus(dkim=str(dkim.get("Status") or ""),
                          mail_from=str(mail_from.get("MailFromDomainStatus") or ""),
                          tokens=list(dkim.get("Tokens") or []),
                          key_length=str(dkim.get("NextSigningKeyLength")
                                         or dkim.get("CurrentSigningKeyLength") or ""))


def identity_verified(domain: str) -> bool:
    return identity_status(domain).dkim == "SUCCESS"


def restart_dkim(domain: str, *, key_length: str = "") -> list[str]:
    """Start Easy DKIM verification over on an identity SES gave up on (DKIM FAILED: the
    records were not found within its 72 h window, and SES never looks again by itself).

    Re-puts the identity's own signing origin (Easy DKIM, `AWS_SES`) with the key length
    it already has — SES refuses the call without one ("Signing key length parameter is
    null or empty") — so no different key is asked for; SES sets the status back to
    PENDING. Returns the tokens SES answers with: the caller compares them with the ones
    it stored, since only those are published in DNS."""
    _guard()
    out = _sesv2().put_email_identity_dkim_signing_attributes(
        EmailIdentity=domain, SigningAttributesOrigin="AWS_SES",
        SigningAttributes={"NextSigningKeyLength": key_length or "RSA_2048_BIT"})
    return list(out.get("DkimTokens") or [])


def restart_mail_from(domain: str, *, mail_from: str) -> None:
    """Start MAIL FROM verification over by putting the same attributes again."""
    _guard()
    _sesv2().put_email_identity_mail_from_attributes(
        EmailIdentity=domain, MailFromDomain=mail_from, BehaviorOnMxFailure="USE_DEFAULT_VALUE")


def delete_identity(domain: str) -> None:
    _guard()
    _sesv2().delete_email_identity(EmailIdentity=domain)


def send_raw(*, from_addr: str, to: list[str], raw: bytes) -> str:
    _guard()
    out = _sesv2_send().send_email(
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


def wire_message_id(ses_message_id: str) -> str:
    """The Message-ID header SES actually sends: it replaces the one canopy wrote with
    `<ses-id@email.amazonses.com>` (us-east-1; `<ses-id@REGION.amazonses.com>` elsewhere).
    That is what a recipient's reply cites in In-Reply-To/References."""
    region = settings.MAIL_SES_REGION
    host = "email.amazonses.com" if region == "us-east-1" else f"{region}.amazonses.com"
    return f"<{ses_message_id}@{host}>"


_SES_HOST = re.compile(r"(?:[a-z0-9-]+\.)*amazonses\.com")


def ses_id_of(header_id: str) -> str:
    """The SES message id inside a Message-ID SES assigned (`<id@…amazonses.com>`), else ""."""
    local, _, host = header_id.strip("<>").rpartition("@")
    return local if local and _SES_HOST.fullmatch(host.lower()) else ""
