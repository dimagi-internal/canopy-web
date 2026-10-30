"""Endorsements — who vouches that a key may read an agent (zk-v1 §7).

Spec: docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md §4.

A `zks1` value is sealed to public keys canopy-web names. A writer who trusts
that list is trusting the server, so a forged key row would read every value
sealed after it. An endorsement closes that: a person signs, in their browser
and with their own vault key, "this key may read this agent" when they wrap the
vault key to it. The writer's browser (or the CLI) seals only to recipients
endorsed by a key it already trusts — its own, or a person it pinned.

canopy-web verifies the signature here so junk is refused, but its check is not
what the design relies on: the WRITER's is. This module never signs anything.
"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from django.db import transaction

from apps.harness import device_keys
from apps.passkeys.models import UserVaultKeyset

from ..models import AgentVaultEndorsement
from . import vault_members, zk
from .zk import ZkError, _shape, b64u_bytes

MAX_ENDORSEMENTS = 64


def endorse_msg(agent_id, kind: str, target: str, key_jkt: str) -> str:
    return f"canopy-vault/v1/endorse|{agent_id}|{kind}|{target}|{key_jkt}"


def verify(agent_id, *, kind: str, target: str, key_jkt: str, sig: str, endorser_jwk: dict) -> bool:
    """Is `sig` the endorser's §7 signature over this (agent, kind, target, key)?"""
    try:
        raw = b64u_bytes(sig, "sig")
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        zk.public_key(endorser_jwk).verify(der, endorse_msg(agent_id, kind, target, key_jkt).encode(),
                                         ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, ZkError):
        return False
    return True


def recipients(agent) -> list[dict]:
    """Every principal a `zks1` value for `agent` may be sealed to, with the
    public key and its thumbprint — `zk_seal.recipients_for`, labelled."""

    out = [{"kind": "device", "target": k.ecdh_jkt, "label": f"{k.runner.name} · {k.label or k.kind}",
            "public_jwk": k.ecdh_public_jwk, "key_jkt": device_keys.thumbprint(k.ecdh_public_jwk)}
           for k in zk.eligible_device_keys(agent)]
    out += [{"kind": "member", "target": str(ks.user_id), "label": ks.user.email,
             "public_jwk": ks.public_jwk, "key_jkt": device_keys.thumbprint(ks.public_jwk)}
            for ks in zk.eligible_members(agent)]
    return out


def endorsements(agent) -> list[dict]:
    """Every endorsement of a CURRENT recipient's CURRENT key, with the
    endorser's public key so a client can verify it without a second read."""

    live = {(r["kind"], r["target"], r["key_jkt"]) for r in recipients(agent)}
    # Vouched for by someone who is a member NOW: an endorser who left vouches for nobody.
    endorsers = vault_members.agent_member_ids(agent)
    rows = [e for e in AgentVaultEndorsement.objects.filter(agent=agent, endorser_id__in=endorsers)
            .select_related("endorser")
            if (e.kind, e.target, e.key_jkt) in live]
    keys = {ks.user_id: ks.public_jwk for ks in
            UserVaultKeyset.objects.filter(user_id__in={e.endorser_id for e in rows})}
    return [{"kind": e.kind, "target": e.target, "key_jkt": e.key_jkt, "by": str(e.endorser_id),
             "by_email": e.endorser.email, "by_jkt": e.endorser_jkt, "by_public_jwk": keys.get(e.endorser_id),
             "sig": e.sig}
            for e in rows]


def add(agent, *, user, items: list, own_machines_only: bool = False) -> int:
    """Store the caller's endorsements. Each must be signed by the caller's OWN
    vault key, name a current recipient by its current key, and verify.

    `own_machines_only`: the caller is a member of the vault, not a manager, and may
    vouch only for a machine that works for THEM (spec 2026-10-22 §1: "a machine is
    trusted through the person who approved it, who must be a member of the vault")."""

    if len(items) > MAX_ENDORSEMENTS:
        raise _shape(f"at most {MAX_ENDORSEMENTS} endorsements")
    # Only a person vouches: an agent login's test vault key (`login_test_key`) never
    # endorses a key to read anything.
    if vault_members.not_a_person([user.pk]):
        raise ZkError(403, "not_a_person",
                      "an agent's login is not a person, and vouches for no key")
    if own_machines_only:
        mine = {k.ecdh_jkt for k in zk.eligible_device_keys(agent) if vault_members.person_of_device(k) == user.pk}
        for i, it in enumerate(items):
            if it.kind != "device" or str(it.target) not in mine:
                raise ZkError(403, "zk_not_your_machine",
                              f"endorsements[{i}]: a member of {agent.slug}'s vault vouches only for their own "
                              "machine; a manager of the vault confirms anyone else")
    ks = UserVaultKeyset.objects.filter(user=user).first()
    if ks is None:
        raise ZkError(409, "zk_no_keyset", "set up your vault key first (Settings → Vault key)")
    my_jkt = device_keys.thumbprint(ks.public_jwk)
    live = {(r["kind"], r["target"]): r["key_jkt"] for r in recipients(agent)}
    rows = []
    for i, it in enumerate(items):
        what = f"endorsements[{i}]"
        kind, target, key_jkt, sig = it.kind, str(it.target), it.key_jkt, it.sig
        if it.by_jkt != my_jkt:
            raise ZkError(422, "zk_not_your_key", f"{what}: sign with your own vault key")
        if live.get((kind, target)) != key_jkt:
            raise ZkError(422, "zk_invalid_target",
                          f"{what}: {kind} {target} is not a current reader of {agent.slug} with that key")
        if not verify(agent.pk, kind=kind, target=target, key_jkt=key_jkt, sig=sig, endorser_jwk=ks.public_jwk):
            raise ZkError(422, "zk_bad_signature", f"{what}: the signature does not verify")
        rows.append((kind, target, key_jkt, sig))
    with transaction.atomic():
        for kind, target, key_jkt, sig in rows:
            AgentVaultEndorsement.objects.update_or_create(
                agent=agent, kind=kind, target=target, endorser=user,
                defaults={"key_jkt": key_jkt, "endorser_jkt": my_jkt, "sig": sig})
    return len(rows)
