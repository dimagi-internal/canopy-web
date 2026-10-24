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
import uuid

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


def message_info(conversation, index: int, role: str | None = None) -> str:
    """The HKDF info and AES-GCM associated data of one message's envelope. Since the
    role is bound too (`r` in the envelope), a stored row cannot be served as another
    speaker's; an envelope without `r` predates that and binds conversation and index."""
    base = f"canopy-chat/v1/message|{conversation}|{index}"
    return f"{base}|{role}" if role else base


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


def ensure_key(session: Session, *, starter=None, offered: dict | None = None) -> SessionKey | None:
    """The conversation's key, made now if it has none. Idempotent; never raises for a
    missing vault key (the send door refuses, `require_key`).

    * already keyed → that key;
    * it carries on a conversation that has one (`parent_session`: a fork, a successor,
      a branch) and its creator is one of that conversation's people (`_carries_on`)
      → the same key pair and the same people's wraps (`copy_key`). Naming a private
      chat as `parent` is not a way to its key: someone not in it gets a key of their
      own;
    * a key pair its starter's BROWSER made (`offered`, checked by `check_offer`) →
      that one: canopy-web never sees its private half;
    * otherwise — no browser was there: a conversation made sensitive mid-turn, from
      Slack, by a host's grant — a new pair made here, wrapped to its starter
      (`created_by`, else `starter`, the person whose act made it sensitive) when they
      have a vault key; None when not."""
    key = key_of(session)
    if key is not None:
        return key
    parent = Session.objects.filter(pk=session.parent_session_id).first() if session.parent_session_id else None
    if parent is not None and key_of(parent) is not None and _carries_on(session, parent, starter):
        return copy_key(parent, session)
    if offered is not None:
        return _store_offer(session, offered)
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


def _carries_on(session: Session, parent: Session, starter=None) -> bool:
    """Whether `session` may carry `parent`'s key on: its creator (else `starter`) is
    one of `parent`'s own people (`sensitivity.is_conversation_person`) — its creator
    or a participant — never an agent admin or co-tenant who can merely see it."""
    from apps.harness import sensitivity  # noqa: PLC0415 — harness's gate reads this module lazily

    person = session.created_by if session.created_by_id else starter
    return person is not None and sensitivity.is_conversation_person(parent, user=person)


def require_key(session: Session, *, user=None, contact=None) -> SessionKey:
    """The key a send into a sensitive conversation seals under — made for its
    creator if it has none yet — or CannotHold: a contact never holds one, nobody
    can write words nobody could read back, and a sender who did not start it never
    becomes its key's holder by sending (a conversation nobody started — an agent's
    own thread — stays keyless, and takes no words)."""
    if contact is not None:
        raise CannotHold()
    key = ensure_key(session)
    if key is None:
        raise CannotHold()
    return key


def check_offer(user, offered) -> dict:
    """A conversation key pair made in `user`'s browser (`SessionCreateIn.conversation_key`):
    its public half, and its private half wrapped there to `user`'s CURRENT vault key.
    The server cannot open the wrap; it checks the public key is a P-256 point, the wrap
    is the shape and length of a wrapped scalar, and `recipient_jkt` names `user`'s
    vault key. Returns the cleaned offer, or raises WrapRefused."""
    keyset = _keyset(user)
    if keyset is None:
        raise WrapRefused("you have no vault key to hold a conversation key")
    if not isinstance(offered, dict):
        raise WrapRefused("a conversation key is {public_jwk, wrap, recipient_jkt}")
    jwk = offered.get("public_jwk")
    try:
        public = ecdh_es.jwk_of(ecdh_es.public_key(jwk if isinstance(jwk, dict) else {}))
    except (ValueError, TypeError):
        raise WrapRefused("public_jwk is not a P-256 public key") from None
    if offered.get("recipient_jkt") != ecdh_es.jkt(keyset.public_jwk):
        raise WrapRefused("that key is not wrapped to your current vault key; reload and try again")
    try:
        wrap = ecdh_es.check_wrap(offered.get("wrap"), plaintext_len=32)
    except ecdh_es.ShapeError as exc:
        raise WrapRefused(f"not a conversation key wrap: {exc}") from None
    try:
        conversation = uuid.UUID(str(offered.get("conversation")))
    except ValueError:
        raise WrapRefused("conversation must be a UUID") from None
    return {"conversation": conversation, "public_jwk": public, "wrap": wrap,
            "recipient_jkt": offered["recipient_jkt"], "user_id": user.pk}


def _store_offer(session: Session, offer: dict) -> SessionKey | None:
    """Keep a browser-made key pair (`check_offer`) as the conversation's key. Kept even
    for a conversation not sensitive yet: if it becomes so later, its words are sealed to
    this key, not to one canopy-web made."""
    if offer.get("user_id") != session.created_by_id:
        log.warning("session %s: a conversation key offered by someone other than its starter", session.pk)
        return None
    try:
        with transaction.atomic():
            key = SessionKey.objects.create(session=session, conversation=offer["conversation"],
                                            public_jwk=offer["public_jwk"])
            SessionKeyWrap.objects.create(session=session, user_id=session.created_by_id,
                                          recipient_jkt=offer["recipient_jkt"], wrap=offer["wrap"])
    except IntegrityError:
        key = SessionKey.objects.filter(session_id=session.pk).first()
    if key is not None:
        session._conversation_key = key
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


def seal_words(session: Session, index: int, words: dict, role: str | None = None) -> str | None:
    """`words` sealed to the conversation's public key at `index`, or None when the
    conversation has no key (its starter has no vault key): then nothing of the words
    is kept, rather than kept where canopy-web could read them."""
    key = key_of(session)
    if key is None:
        log.warning("sensitive session %s has no conversation key; its words at %s are not kept",
                    session.pk, index)
        return None
    return envelope(key.conversation, key.public_jwk, index, words, role=role)


def envelope(conversation, public_jwk: dict, index: int, words: dict, *, role: str | None = None,
             **pinned) -> str:
    body = json.dumps(words, sort_keys=True, separators=(",", ":")).encode()
    sealed = ecdh_es.wrap(body, public_jwk, message_info(conversation, index, role), **pinned)
    out = {"c": str(conversation), "i": int(index), **({"r": role} if role else {}), **sealed}
    return PREFIX + ecdh_es.b64u(json.dumps(out, separators=(",", ":")).encode())


def is_envelope(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal_existing(session: Session) -> int:
    """Every row `session` stored IN CLEAR before it became sensitive, as a sensitive
    conversation stores it (`sensitivity.seal`): metadata columns, the words sealed to
    the conversation's public key — which canopy-web holds, so it can seal what it
    can no longer read back — and a tool's result dropped. With no key (its starter
    has no vault key) the words are not kept at all. Run by
    `services.add_runner_requirements` in the transaction that made it sensitive, so
    no reader ever sees the conversation sensitive with its old rows open. Returns
    how many rows it changed."""
    from apps.harness import sensitivity  # noqa: PLC0415 — harness's gate reads this module lazily

    changed = []
    for m in Message.objects.filter(session_id=session.pk, sealed__isnull=True).iterator():
        content = m.content if isinstance(m.content, dict) else {}
        if content.get("redacted") == sensitivity.REDACTED or content.get("withheld"):
            continue  # already metadata
        m.plaintext, new_content, m.sealed = sensitivity.seal(
            session, m.turn_index, m.role, m.plaintext, content)
        if content.get("client_id"):
            new_content["client_id"] = content["client_id"]
        m.content = new_content
        changed.append(m)
    if changed:
        Message.objects.bulk_update(changed, ["plaintext", "content", "sealed"], batch_size=500)
    return len(changed)


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
        "index_offsets": index_offsets(session),
    }


def index_offsets(session: Session) -> list[int] | None:
    """How far rows here may sit below the index their envelopes were sealed at. A fork
    renumbers the rows it copies (`placement.fork_at`) while each envelope keeps its own
    `i`, so a reader accepts `i == turn_index + d` for d in [0, *these]. [] for a
    conversation that is not a fork; None for a fork made before forks recorded it (the
    browser then checks only the conversation an envelope names)."""
    fork = (session.metadata or {}).get("fork")
    if not isinstance(fork, dict):
        return []
    value = fork.get("index_offsets")
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        return None
    return value


def keyset_for_browser(user) -> dict | None:
    """`user`'s own keyset (public half and seals), for their browser to open it with."""
    keyset = _keyset(user)
    return _keyset_out(user, keyset) if keyset else None


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


class CopyExists(WrapRefused):
    """They already hold a copy for their current vault key. Nobody else may write over
    it: the server cannot tell a good wrap from random bytes, so an overwrite could
    lock its holder out — the owner included — with nothing showing it."""

    def __init__(self, user=None):
        super().__init__(f"{getattr(user, 'email', None) or 'they'} already hold a copy of this "
                         "conversation's key for their current vault key; if it no longer opens, "
                         "they set it aside themselves (People → my copy does not open) and "
                         "then someone in it gives them a fresh one")


def add_wrap(session: Session, user, wrap, *, recipient_jkt: str, by) -> SessionKeyWrap:
    """Store `wrap`, made in `by`'s browser, as `user`'s copy of the conversation key.
    The server cannot open it; it checks what it can: the shape, that it is the length
    of a wrapped P-256 scalar, and that it names `user`'s CURRENT vault key.

    It is written only where `user` holds no copy for that key (CopyExists otherwise):
    a copy for an OLDER vault key is provably stale (its `recipient_jkt` names a key
    they no longer have) and is replaced; a current one that does not open is set
    aside by its holder first (`set_aside_my_copy`)."""
    if key_of(session) is None:
        raise WrapRefused("this conversation has no key to share")
    keyset = _keyset(user)
    if keyset is None:
        raise WrapRefused(f"{getattr(user, 'email', 'they')} has no vault key yet, so they cannot "
                          "read a sensitive conversation; ask them to set one up first")
    current = ecdh_es.jkt(keyset.public_jwk)
    if recipient_jkt != current:
        raise WrapRefused("that wrap is not for their current vault key; reload and try again")
    try:
        clean = ecdh_es.check_wrap(wrap, plaintext_len=32)
    except ecdh_es.ShapeError as exc:
        raise WrapRefused(f"not a conversation key wrap: {exc}") from None
    with transaction.atomic():
        held = SessionKeyWrap.objects.select_for_update().filter(session=session, user=user).first()
        if held is not None and held.recipient_jkt == current:
            raise CopyExists(user)
        row, _ = SessionKeyWrap.objects.update_or_create(
            session=session, user=user,
            defaults={"wrap": clean, "recipient_jkt": recipient_jkt, "created_by": by})
    return row


def set_aside_my_copy(session: Session, user) -> bool:
    """`user`'s own act: their copy of this conversation's key does not open for them
    (broken, or wrapped to a key they lost), so they drop it and someone in it can give
    them a fresh one (`add_wrap`). Only the holder can; True if they held one."""
    return SessionKeyWrap.objects.filter(session=session, user_id=user.pk).delete()[0] > 0


def has_current_wrap(session: Session, user) -> bool:
    keyset = _keyset(user)
    return bool(keyset) and SessionKeyWrap.objects.filter(
        session=session, user_id=user.pk, recipient_jkt=ecdh_es.jkt(keyset.public_jwk)).exists()


def holds_copy(session: Session, user) -> bool:
    """Whether `user` was given a copy of the conversation's key (current or not). The
    live socket carries a sensitive conversation's words, so it admits only someone who
    holds one (`sensitivity.hears_live`): a participant added before the conversation
    became sensitive has none until someone in it wraps them one. True for a
    conversation with no key: nothing is sealed, so there is nothing to hold."""
    if key_of(session) is None:
        return True
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return SessionKeyWrap.objects.filter(session=session, user_id=user.pk).exists()


def without_current_copy(session: Session, user_ids) -> set:
    """Which of `user_ids` hold no copy of the key for their CURRENT vault key: someone
    added before it became sensitive, or whose vault key changed since. One read for
    the people list (`ParticipantOut.needs_key`). Empty for a conversation with no key."""
    if key_of(session) is None:
        return set()
    ids = set(user_ids)
    current = {k.user_id: ecdh_es.jkt(k.public_jwk)
               for k in UserVaultKeyset.objects.filter(user_id__in=ids)}
    held = {w.user_id: w.recipient_jkt for w in SessionKeyWrap.objects.filter(session=session, user_id__in=ids)}
    return {uid for uid in ids if uid not in current or held.get(uid) != current[uid]}


def my_copies(user) -> list[dict]:
    """Every conversation key `user` holds a copy of, for their browser to re-wrap
    before they replace their vault key (`PUT /my-keys` after). What is said is never
    here: a wrap, the conversation's public key, and enough to name it to its owner
    (the agent, when it started)."""
    rows = (SessionKeyWrap.objects.filter(user_id=user.pk)
            .select_related("session", "session__agent"))
    out = []
    for w in rows:
        key = SessionKey.objects.filter(session_id=w.session_id).first()
        if key is None:
            continue
        agent = w.session.agent
        out.append({
            "session_id": str(w.session_id), "conversation": str(key.conversation),
            "public_jwk": key.public_jwk, "wrap": w.wrap, "recipient_jkt": w.recipient_jkt,
            "agent_name": (agent.name if agent is not None else "") or "",
            "workspace": w.session.workspace_id or "",
            "started_at": w.session.created_at,
        })
    return out


def replace_my_copies(user, copies: list[dict]) -> int:
    """Store `copies` — `{session_id, wrap, recipient_jkt}`, each re-wrapped in the
    person's browser to their NEW vault key — over the copies they already hold. Only
    a copy they hold is replaced (this never puts them in a conversation), and only to
    their current vault key. Returns how many were stored; raises WrapRefused on the
    first bad one, storing none."""
    keyset = _keyset(user)
    if keyset is None:
        raise WrapRefused("you have no vault key")
    want = ecdh_es.jkt(keyset.public_jwk)
    clean = []
    for i, c in enumerate(copies):
        if c.get("recipient_jkt") != want:
            raise WrapRefused(f"copies[{i}] is not for your current vault key")
        try:
            clean.append((str(c.get("session_id")), ecdh_es.check_wrap(c.get("wrap"), plaintext_len=32)))
        except ecdh_es.ShapeError as exc:
            raise WrapRefused(f"copies[{i}]: not a conversation key wrap: {exc}") from None
    n = 0
    with transaction.atomic():
        for session_id, wrap in clean:
            n += SessionKeyWrap.objects.filter(session_id=session_id, user_id=user.pk).update(
                wrap=wrap, recipient_jkt=want, created_by=user)
    return n


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
        for m in legacy.filter(session_id=session_id).only("pk", "turn_index", "role", "sealed"):
            try:
                words = json.loads(decrypt_secret(m.sealed))
            except Exception:  # noqa: BLE001 — a row no key opens is kept as it is
                log_to.warning("message %s in %s does not open under the field key", m.pk, session_id)
                continue
            Message.objects.filter(pk=m.pk).update(
                sealed=envelope(key.conversation, key.public_jwk, m.turn_index, words, role=m.role))
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
            "message_info_with_role": message_info(conversation, index, "assistant"),
            "sealed_with_role": envelope(conversation, conv_jwk, index, words, role="assistant",
                                         _ephemeral=e_msg, _nonce=nonce_msg),
        },
    }
