"""Sealed AWS roles: the labs role keys canopy-web carries but cannot use.

The custodial path (`api_aws.py`) assumes a role with canopy-web's own task role, so whoever
runs code inside canopy-web can do the same, approval or not. Here each role has its OWN AWS
access key (an IAM user, `canopy-sealed/<role>`, that may assume only that role; connect-labs
`infra/canopy-sealed-keys.yml`), and canopy-web stores it only as a `zks1` value sealed in a
person's browser (`AwsSealedKey`). Two holders:

* **A fenced role's approvers** (`agent` null): the key is sealed to their vault keys. When one
  approves a SEALED grant (`AgentAwsRoleRequest.delivery`), their browser opens the key on a
  passkey touch, asks AWS STS itself for a session as the agent (`sts_request`), seals the
  session to the agent's machines (`agent_devices`) and stores it on the grant
  (`sealed_session`). The approver must hold the key (`may_mint`), so `agent.aws_role`'s
  approvers for a sealed grant are its holders who may approve.
* **An agent standing for the role** (`agent` set, `settings.CANOPY_AWS_ROLE_AGENTS`): the key
  is sealed to the machines that run that agent, which open it and ask STS themselves
  (`canopy aws credentials`, the canopy CLI).

canopy-web checks each value's SHAPE and that it is sealed only to who it should be
(`check_holders`, `check_devices`); it never decrypts anything. The browser, not this list,
decides which device keys to trust: it seals only to keys endorsed by someone it trusts
(`frontend/src/vault/trust.ts::assess`), so a canopy-web that names its own key gets nothing.
"""
from __future__ import annotations

import datetime as dt
import re

from django.contrib.auth import get_user_model
from django.utils import timezone

from . import aws_grants
from apps.harness import device_keys
from apps.passkeys.models import UserVaultKeyset

from .credentials import zk, zk_endorse
from .credentials.zk import ZkError
from .credentials.zk_seal import check_sealed_value, sealed_targets
from .models import AgentAwsRoleRequest, AwsSealedKey

#: The names the seals bind (`zk-v1` §6: binding, name, kv); kv is always 0.
KEY_NAME = "AWS_ACCESS_KEY"
SESSION_NAME = "AWS_SESSION"
KV = 0
#: How long a session a browser asks for at most: the longest grant (`MAX_MINUTES`). STS
#: answers with no more than the role's MaxSessionDuration; the browser then asks for an hour.
MAX_SESSION_SECONDS = AgentAwsRoleRequest.MAX_MINUTES * 60
_STS_NAME = re.compile(r"[^\w+=,.@-]")


class SealedError(ValueError):
    """A sealed value canopy-web refuses to store; the message is shown as is."""


def key_binding(role: str, agent=None) -> str:
    """What a role key's seal is bound to: the approvers' copy, or one standing agent's."""
    return f"aws-role:{role}" if agent is None else f"aws-role:{role}@{agent.slug}"


def session_binding(grant: AgentAwsRoleRequest) -> str:
    return f"aws-grant:{grant.pk}"


def sts_name(text: str) -> str:
    """A value STS accepts as a session name / source identity (2-64 of [\\w+=,.@-])."""
    out = _STS_NAME.sub("-", text)[:64]
    return out if len(out) >= 2 else (out + "--")[:2]


def holders_key(role: str) -> AwsSealedKey | None:
    return AwsSealedKey.objects.filter(role=role, agent__isnull=True).first()


def standing_key(role: str, agent) -> AwsSealedKey | None:
    return AwsSealedKey.objects.filter(role=role, agent=agent).first()


def holder_ids(row: AwsSealedKey | None) -> set[int]:
    """The people a role key is sealed to."""
    if row is None:
        return set()
    return {int(t) for kind, t in sealed_targets(row.value_zk) if kind == "member" and t.isdecimal()}


def may_hold(user) -> bool:
    """Who a role's approver key may be sealed to: a canopy superuser with a vault key (the
    people who may approve every agent's AWS grant; `kinds/agent_aws_role.py`)."""
    return bool(getattr(user, "is_superuser", False) and getattr(user, "is_active", False)
                and UserVaultKeyset.objects.filter(user=user).exists())


def may_mint(user, role: str) -> bool:
    """Does `user` hold the approvers' key for `role`, so their browser can make its session?"""
    return getattr(user, "pk", None) in holder_ids(holders_key(role))


def holder_candidates() -> list[dict]:
    """Everyone a role's approver key may be sealed to, with their vault public key."""
    users = get_user_model().objects.filter(is_superuser=True, is_active=True)
    keysets = {ks.user_id: ks for ks in UserVaultKeyset.objects.filter(user__in=users).select_related("user")}
    return [{"user_id": str(uid), "email": ks.user.email, "public_jwk": ks.public_jwk,
             "jkt": device_keys.thumbprint(ks.public_jwk)}
            for uid, ks in sorted(keysets.items())]


def agent_devices(agent) -> dict:
    """The machines that run `agent`, as `ZkRecipientsOut` (devices only) with the endorsements
    of their keys, so the browser verifies each against a key it trusts before sealing."""
    devices = [r for r in zk_endorse.recipients(agent) if r["kind"] == "device"]
    return {"agent": agent.slug, "agent_id": str(agent.pk), "kv": agent.zk_kv, "recipients": devices,
            "endorsements": [e for e in zk_endorse.endorsements(agent) if e["kind"] == "device"],
            "declared": [], "stored": []}


def device_targets(agent) -> set[str]:
    return {k.ecdh_jkt for k in zk.eligible_device_keys(agent)}


def _check(value: str, what: str) -> set[tuple[str, str]]:
    try:
        check_sealed_value(value, what)
    except ZkError as exc:
        raise SealedError(getattr(exc, "message", None) or str(exc)) from exc
    return sealed_targets(value)


def check_holders(value: str) -> set[int]:
    """A role's approver key, sealed only to people who may hold it. Returns their ids."""
    targets = _check(value, KEY_NAME)
    if any(kind != "member" for kind, _ in targets):
        raise SealedError("a role's approver key is sealed to people only, never to a machine")
    ids = {int(t) for _, t in targets if t.isdecimal()}
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=ids)}
    if len(ids) != len(targets) or any(not may_hold(users.get(i)) for i in ids):
        raise SealedError("a role's approver key is sealed only to canopy superusers with a vault key")
    return ids


def check_devices(value: str, agent, what: str) -> None:
    """A value for `agent`'s machines, sealed only to the device keys that run it now."""
    targets = _check(value, what)
    allowed = device_targets(agent)
    if any(kind != "device" or t not in allowed for kind, t in targets):
        raise SealedError(f"{what} is sealed only to the machines that run {agent.slug} now; "
                          "read the recipients again and seal to those")


def sts_request(agent, role: str, *, caller: str, seconds: int) -> dict:
    """What the browser or the agent's machine asks STS: the session stamped with the agent,
    exactly as the custodial path stamps it, so CloudTrail reads the same either way."""
    return {"role_arn": aws_grants.roles().get(role, ""), "role_session_name": sts_name(f"canopy-{agent.slug}"),
            "source_identity": sts_name(f"canopy-agent-{agent.slug}"),
            "tags": [{"Key": "agent", "Value": agent.slug}, {"Key": "caller", "Value": sts_name(caller)},
                     {"Key": "delivery", "Value": "sealed"}],
            "duration_seconds": max(900, min(int(seconds), MAX_SESSION_SECONDS)), "region": "us-east-1"}


def session_ok(grant: AgentAwsRoleRequest, now=None) -> bool:
    now = now or timezone.now()
    return bool(grant.sealed_session and grant.sealed_session_expires_at and grant.sealed_session_expires_at > now)


def store_session(grant: AgentAwsRoleRequest, value: str, expires_at: dt.datetime) -> AgentAwsRoleRequest:
    """The approver's browser hands over the session it made, sealed to the agent's machines.
    The session cannot outlast the grant; a later one replaces it."""
    check_devices(value, grant.agent, SESSION_NAME)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    if grant.expires_at is not None and expires_at > grant.expires_at + dt.timedelta(minutes=5):
        raise SealedError("the session ends after the grant does; ask STS for no longer than the grant's time left")
    grant.sealed_session, grant.sealed_session_expires_at = value, expires_at
    grant.save(update_fields=["sealed_session", "sealed_session_expires_at"])
    return grant


def live_sealed_grant(agent, role: str, now=None) -> AgentAwsRoleRequest | None:
    now = now or timezone.now()
    return (AgentAwsRoleRequest.objects.filter(agent=agent, role=role, status=AgentAwsRoleRequest.APPROVED,
                                               delivery=AgentAwsRoleRequest.SEALED, revoked_at__isnull=True,
                                               expires_at__gt=now)
            .order_by("-expires_at").first())
