"""A sensitive conversation's key: canopy-web can WRITE the conversation and never read
it back (spec 2026-10-23-sensitive-conversations-sealed-to-their-people §2).

**The key pair.** When a session becomes sensitive it gets a P-256 ECDH key pair
(`ensure_key`). The public half is `SessionKey.public_jwk`; the private half is wrapped
at once to the starter's vault key (`UserVaultKeyset.public_jwk`) and let go. canopy-web
saw it once because it made it — the same stance as `zk_seal` — and cannot open
anything sealed to it afterwards.

**Sealing** (`seal_words`): each stored message's words go into an ECIES envelope —
`apps/common/ecdh_es.py`, the vault's own `wrapEcdhEs` — with the conversation's public
key as recipient and `canopy-chat/v1/message|<conversation>|<turn_index>` as HKDF info
and AES-GCM associated data:

    cc1: + b64u(JSON {"c": conversation, "i": turn_index, "alg": "ecdh-es", "epk", "n", "ct"})

where `ct` opens to JSON `{"plaintext", "content"}`. `c` and `i` say what the envelope
was bound to when it was sealed (a fork carries the same envelope at a new index).

**Wraps**: `{"alg": "ecdh-es", "epk", "n", "ct"}` of the 32-byte private scalar, info
`canopy-chat/v1/wrap|<conversation>|<user id>`. One per person (`SessionKeyWrap`): the
starter's is made here; anyone added later gets one from a browser already in the
conversation, which unwraps its own and wraps it again (`add_wrap` checks the shape).

Byte compatibility with the browser (`frontend/src/chat/sealedChat.ts`) is pinned by
`docs/vault/chat-v1/chat_v1_vectors.json`, which `vectors()` produces and both
`tests/test_conversation_keys.py` and `sealedChat.test.ts` check.
"""
from __future__ import annotations

import json
import logging

from cryptography.hazmat.primitives.asymmetric import ec
from django.db import IntegrityError, transaction

from apps.common import ecdh_es
from apps.common.encryption import decrypt_secret
from apps.passkeys.api_vault import prf_input
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import links

from .models import Message, Session, SessionKey, SessionKeyWrap

log = logging.getLogger(__name__)

PREFIX = "cc1:"
#: What the person is told when they cannot hold one: a contact (no canopy account) or
#: a canopy user with no vault key.
NEEDS_VAULT_KEY = ("a sensitive conversation needs a canopy account with a vault key; "
                   "set yours up at {url}")


class CannotHold(ValueError):
    """This principal cannot start or hold a sensitive conversation. A ValueError, so a
    send door already turns it into a 422 carrying the sentence."""

    def __init__(self):
        super().__init__(NEEDS_VAULT_KEY.format(url=links.public_url("/settings#vault-key")))


def message_info(conversation, index: int) -> str:
    return f"canopy-chat/v1/message|{conversation}|{index}"


def wrap_info(conversation, user_id) -> str:
    return f"canopy-chat/v1/wrap|{conversation}|{user_id}"


def _keyset(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return UserVaultKeyset.objects.filter(user_id=user.pk).first()


def can_hold(*, user=None, contact=None) -> bool:
    """Whether this principal may start or hold a sensitive conversation: a canopy
    user with a vault key. A contact cannot, for now."""
    return contact is None and _keyset(user) is not None


def check_holder(*, user=None, contact=None) -> None:
    if not can_hold(user=user, contact=contact):
        raise CannotHold()


def key_of(session: Session) -> SessionKey | None:
    cached = getattr(session, "_conversation_key", None)
    if cached is not None:
        return cached
    key = SessionKey.objects.filter(session_id=session.pk).first()
    if key is not None:
        session._conversation_key = key
    return key


def ensure_key(session: Session, *, starter=None) -> SessionKey | None:
    """The conversation's key, made now if it has none. Idempotent; never raises for a
    missing vault key (the send door refuses, `require_key`).

    * already keyed → that key;
    * it carries on a conversation that has one (`parent_session`: a fork, a successor,
      a branch) → the same key pair and the same people's wraps (`copy_key`);
    * otherwise a new pair, wrapped to its starter (`created_by`, else `starter`, the
      person whose act made it sensitive) when they have a vault key; None when not."""
    key = key_of(session)
    if key is not None:
        return key
    parent = Session.objects.filter(pk=session.parent_session_id).first() if session.parent_session_id else None
    if parent is not None and key_of(parent) is not None:
        return copy_key(parent, session)
    person = session.created_by if session.created_by_id else starter
    keyset = _keyset(person)
    if keyset is None:
        return None
    scalar, public_jwk = ecdh_es.generate()
    try:
        with transaction.atomic():
            key = SessionKey.objects.create(session=session, conversation=session.pk, public_jwk=public_jwk)
            SessionKeyWrap.objects.create(
                session=session, user_id=person.pk, recipient_jkt=ecdh_es.jkt(keyset.public_jwk),
                wrap=ecdh_es.wrap(scalar, keyset.public_jwk, wrap_info(session.pk, person.pk)))
    except IntegrityError:
        # A concurrent writer made it first: theirs is the conversation's key.
        key = SessionKey.objects.filter(session_id=session.pk).first()
    finally:
        del scalar
    if key is not None:
        session._conversation_key = key
    return key


def require_key(session: Session, *, user=None, contact=None) -> SessionKey:
    """The key a send into a sensitive conversation seals under — made for this sender
    if the conversation has none yet — or CannotHold: a contact never holds one, and
    nobody can write words nobody could read back."""
    if contact is not None:
        raise CannotHold()
    key = ensure_key(session, starter=user)
    if key is None:
        raise CannotHold()
    return key


def copy_key(source: Session, target: Session) -> SessionKey | None:
    """`target` carries `source`'s conversation on (a fork): the same key pair, bound to
    the same conversation, and every wrap `source` has. Whatever key `target` was given
    on its own at birth is replaced."""
    key = key_of(source)
    if key is None:
        return None
    with transaction.atomic():
        SessionKeyWrap.objects.filter(session=target).delete()
        SessionKey.objects.filter(session=target).delete()
        copied = SessionKey.objects.create(session=target, conversation=key.conversation,
                                           public_jwk=key.public_jwk)
        SessionKeyWrap.objects.bulk_create([
            SessionKeyWrap(session=target, user_id=w.user_id, wrap=w.wrap,
                           recipient_jkt=w.recipient_jkt, created_by_id=w.created_by_id)
            for w in SessionKeyWrap.objects.filter(session=source)])
    target._conversation_key = copied
    return copied


def seal_words(session: Session, index: int, words: dict) -> str | None:
    """`words` sealed to the conversation's public key at `index`, or None when the
    conversation has no key (its starter has no vault key): then nothing of the words
    is kept, rather than kept where canopy-web could read them."""
    key = key_of(session)
    if key is None:
        log.warning("sensitive session %s has no conversation key; its words at %s are not kept",
                    session.pk, index)
        return None
    return envelope(key.conversation, key.public_jwk, index, words)


def envelope(conversation, public_jwk: dict, index: int, words: dict, **pinned) -> str:
    body = json.dumps(words, sort_keys=True, separators=(",", ":")).encode()
    sealed = ecdh_es.wrap(body, public_jwk, message_info(conversation, index), **pinned)
    out = {"c": str(conversation), "i": int(index), **sealed}
    return PREFIX + ecdh_es.b64u(json.dumps(out, separators=(",", ":")).encode())


def is_envelope(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


# ── who holds it ────────────────────────────────────────────────────────────


def reader_key(session: Session, user) -> dict | None:
    """What a reader's browser needs to open the conversation: its public key, the
    conversation it is bound to, and the reader's OWN wrap (None when they were never
    given one, or their vault key changed since — `current` says which). Asked only
    for a reader `sensitivity.reads_content` admitted. None for a conversation with
    no key."""
    key = key_of(session)
    if key is None:
        return None
    mine = None
    keyset = _keyset(user)
    if user is not None and getattr(user, "is_authenticated", False):
        mine = SessionKeyWrap.objects.filter(session=session, user_id=user.pk).first()
    current = bool(mine and keyset and mine.recipient_jkt == ecdh_es.jkt(keyset.public_jwk))
    return {
        "conversation": str(key.conversation),
        "public_jwk": key.public_jwk,
        "wrap": mine.wrap if mine else None,
        "recipient_jkt": mine.recipient_jkt if mine else "",
        "current": current,
        "keyset": _keyset_out(user, keyset) if keyset else None,
        "needs_migration": needs_migration(session),
    }


def _keyset_out(user, keyset) -> dict:
    """The reader's own vault keyset (public half and its seals), so a surface that
    has no browser session — the embedded panel — can unlock it in the browser."""
    return {"user_id": str(user.pk), "prf_input": prf_input(user.pk), "exists": True,
            "public_jwk": keyset.public_jwk, "seals": list(keyset.seals or [])}


def recipient(user) -> dict | None:
    """A person's current vault public key, to wrap the conversation key to."""
    keyset = _keyset(user)
    if keyset is None:
        return None
    return {"user_id": user.pk, "public_jwk": keyset.public_jwk, "jkt": ecdh_es.jkt(keyset.public_jwk)}


class WrapRefused(ValueError):
    pass


def add_wrap(session: Session, user, wrap, *, recipient_jkt: str, by) -> SessionKeyWrap:
    """Store `wrap`, made in `by`'s browser, as `user`'s copy of the conversation key.
    The server cannot open it; it checks what it can: the shape, that it is the length
    of a wrapped P-256 scalar, and that it names `user`'s CURRENT vault key."""
    if key_of(session) is None:
        raise WrapRefused("this conversation has no key to share")
    keyset = _keyset(user)
    if keyset is None:
        raise WrapRefused(f"{getattr(user, 'email', 'they')} has no vault key yet, so they cannot "
                          "read a sensitive conversation; ask them to set one up first")
    if recipient_jkt != ecdh_es.jkt(keyset.public_jwk):
        raise WrapRefused("that wrap is not for their current vault key; reload and try again")
    try:
        clean = ecdh_es.check_wrap(wrap, plaintext_len=32)
    except ecdh_es.ShapeError as exc:
        raise WrapRefused(f"not a conversation key wrap: {exc}") from None
    row, _ = SessionKeyWrap.objects.update_or_create(
        session=session, user=user,
        defaults={"wrap": clean, "recipient_jkt": recipient_jkt, "created_by": by})
    return row


def has_current_wrap(session: Session, user) -> bool:
    keyset = _keyset(user)
    return bool(keyset) and SessionKeyWrap.objects.filter(
        session=session, user_id=user.pk, recipient_jkt=ecdh_es.jkt(keyset.public_jwk)).exists()


def drop_wrap(session: Session, user_id) -> None:
    SessionKeyWrap.objects.filter(session=session, user_id=user_id).delete()


# ── rows sealed before keys were per conversation ──────────────────────────


def needs_migration(session: Session) -> bool:
    """Whether the session still has words under the field key."""
    return (Message.objects.filter(session_id=session.pk, sealed__isnull=False)
            .exclude(sealed__startswith=PREFIX).exists())


def reseal_legacy(*, Session=Session, Message=Message, SessionKey=SessionKey,
                  SessionKeyWrap=SessionKeyWrap, UserVaultKeyset=UserVaultKeyset, log_to=log) -> dict:
    """Move every row still under the field key (`apps/common/encryption.py`) to its
    conversation's key, wrapped to its starter: the one time canopy-web opens the
    words, because it can (spec §2, "Existing rows"). A session whose starter has no
    vault keyset keeps the field key and is logged as needing migration — run
    `manage.py reseal_sensitive_conversations` once they have set one up.

    Takes its models so the data migration can pass its historical ones."""
    legacy = Message.objects.filter(sealed__isnull=False).exclude(sealed__startswith=PREFIX)
    done = {"sessions": 0, "rows": 0, "pending": []}
    for session_id in legacy.values_list("session_id", flat=True).distinct():
        session = Session.objects.get(pk=session_id)
        key = SessionKey.objects.filter(session_id=session_id).first()
        if key is None:
            keyset = (UserVaultKeyset.objects.filter(user_id=session.created_by_id).first()
                      if session.created_by_id else None)
            if keyset is None:
                done["pending"].append(str(session_id))
                log_to.warning("sensitive session %s needs migration: its starter has no vault key",
                               session_id)
                continue
            scalar, public_jwk = ecdh_es.generate()
            key = SessionKey.objects.create(session_id=session_id, conversation=session_id,
                                            public_jwk=public_jwk)
            SessionKeyWrap.objects.create(
                session_id=session_id, user_id=session.created_by_id,
                recipient_jkt=ecdh_es.jkt(keyset.public_jwk),
                wrap=ecdh_es.wrap(scalar, keyset.public_jwk, wrap_info(session_id, session.created_by_id)))
            del scalar
        for m in legacy.filter(session_id=session_id).only("pk", "turn_index", "sealed"):
            try:
                words = json.loads(decrypt_secret(m.sealed))
            except Exception:  # noqa: BLE001 — a row no key opens is kept as it is
                log_to.warning("message %s in %s does not open under the field key", m.pk, session_id)
                continue
            Message.objects.filter(pk=m.pk).update(
                sealed=envelope(key.conversation, key.public_jwk, m.turn_index, words))
            done["rows"] += 1
        done["sessions"] += 1
    return done


# ── the cross-language vector ───────────────────────────────────────────────


def vectors() -> dict:
    """`docs/vault/chat-v1/chat_v1_vectors.json`: fixed keys and randomness through the
    server's own wrap and seal, for the browser to open byte for byte."""
    def priv(hex32):
        return ec.derive_private_key(int(hex32, 16), ec.SECP256R1())

    conversation, user_id, index = "0b7c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3", "7", 12
    conv, person = priv("5a" * 32), priv("3c" * 32)
    e_wrap, e_msg = priv("2e" * 32), priv("4d" * 32)
    nonce_wrap, nonce_msg = bytes.fromhex("000102030405060708090a0b"), bytes.fromhex("0c0d0e0f1011121314151617")
    scalar = conv.private_numbers().private_value.to_bytes(32, "big")
    person_jwk, conv_jwk = ecdh_es.jwk_of(person.public_key()), ecdh_es.jwk_of(conv.public_key())
    words = {"plaintext": "The patient's name is Ada.", "content": {"text": "The patient's name is Ada."}}
    return {
        "format": "canopy-chat/v1",
        "inputs": {
            "conversation": conversation, "user_id": user_id, "turn_index": index,
            "conversation_priv_scalar_hex": "5a" * 32, "conversation_pub_jwk": conv_jwk,
            "user_priv_scalar_hex": "3c" * 32, "user_pub_jwk": person_jwk,
            "ephemeral_wrap_scalar_hex": "2e" * 32, "ephemeral_message_scalar_hex": "4d" * 32,
            "nonce_wrap_hex": nonce_wrap.hex(), "nonce_message_hex": nonce_msg.hex(),
            "words": words,
        },
        "outputs": {
            "wrap_info": wrap_info(conversation, user_id),
            "message_info": message_info(conversation, index),
            "user_jkt": ecdh_es.jkt(person_jwk),
            "wrap": ecdh_es.wrap(scalar, person_jwk, wrap_info(conversation, user_id),
                                 _ephemeral=e_wrap, _nonce=nonce_wrap),
            "sealed": envelope(conversation, conv_jwk, index, words, _ephemeral=e_msg, _nonce=nonce_msg),
        },
    }
