"""Reply push: canopy POSTs an agent's reply to a relay host, signed.

A host that relays its people's conversations (``canopy_sdk.host.relay``) registers a
``reply_webhook_url`` on its host site. When a turn in one of its contacts'
conversations produces the agent's prose, canopy POSTs it there as a compact JWS
(``Content-Type: application/jwt``, ``typ: canopy-reply+jwt``) signed with canopy's OAuth
client key, the key behind canopy's client metadata (CIMD) that a host already trusts
for ``Canopy-Execution``. The host never polls.

    from canopy_sdk.host.replies import ReplyRefused, verify_reply

    def canopy_reply(request):                  # your reply_webhook_url
        try:
            reply = verify_reply(request.body.decode(), config=config)
        except ReplyRefused as exc:
            return HttpResponse(status=401)     # exc.code says why
        deliver_to_channel(reply.sub, reply.text, dedupe_key=(reply.turn, reply.seq))
        return HttpResponse(status=204)

* ``sub`` is the host's OWN id for the person (the ``sub`` it signed into their
  arrival); ``site`` is the host's host-site name; ``session`` / ``turn`` / ``seq``
  name the conversation, the turn and the post. canopy retries a post it could not
  deliver (a non-2xx answer, a timeout), so the host must treat ``(turn, seq)`` as the
  idempotency key: the same pair twice is the same reply.
* ``ended`` is true on a turn's last post. In ``final`` mode a turn has exactly one
  post, carrying all its prose; in ``stream`` mode each piece of prose is a post and
  the last (``ended``) carries no text. ``error`` (``failed``, ``cancelled``, …) is set
  on the last post of a turn that did not finish well.

``sign_reply`` is canopy's half, here so both sides read one definition.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .. import contract
from ..contract import ContractError, constant_time_equal
from ..jose import decode, new_jti, sign
from ..keys import public_jwk, public_key_for
from ..stores import MemoryJtiStore, Replayed
from .client_keys import ClientKeyResolver, MetadataError

log = logging.getLogger("canopy_sdk.host")

#: ``typ`` of a reply: distinct from every other token canopy signs.
REPLY_TYP = "canopy-reply+jwt"
#: How long a signed reply is good for. A retry is signed afresh.
REPLY_MAX_LIFETIME = 300
REPLY_REQUIRED_CLAIMS: tuple[str, ...] = (
    "iss", "aud", "sub", "site", "session", "turn", "seq", "text", "ended", "iat", "exp", "jti")
#: The content type canopy POSTs a reply with.
REPLY_CONTENT_TYPE = "application/jwt"
#: A reply's text is bounded by what a turn's prose can be; the token a little more.
MAX_REPLY_TOKEN_BYTES = 512 * 1024
#: The ``reply_mode`` a host site may choose.
REPLY_MODES = ("final", "stream")


class ReplyRefused(Exception):
    """The reply is not from canopy, not for this host, stale or already used.
    ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Reply:
    """A verified reply: what the agent said to the person, in one post."""

    #: The host's host-site name.
    site: str
    #: The host's own id for the person.
    sub: str
    #: canopy's ids for the conversation and the turn.
    session: str
    turn: str
    #: The post's place in the turn; with ``turn``, the idempotency key.
    seq: int
    text: str
    #: The turn's last post.
    ended: bool
    #: Set on the last post of a turn that did not finish well.
    error: str = ""
    claims: dict = field(repr=False, default_factory=dict)


def sign_reply(client_key, client_id: str, audience: str, *, sub: str, site: str, session: str,
               turn: str, seq: int, text: str, ended: bool, error: str = "",
               now: float | None = None) -> str:
    """canopy's signed reply for ONE post to ``audience`` (the host's
    ``reply_webhook_url``): ``iss`` = canopy's ``client_id``, five minutes, a fresh
    ``jti``. Signed again for every delivery attempt; ``(turn, seq)`` stays the same."""
    issued = int(time.time() if now is None else now)
    claims = {"iss": client_id, "aud": audience, "sub": sub, "site": site, "session": session,
              "turn": turn, "seq": int(seq), "text": text or "", "ended": bool(ended),
              "iat": issued, "exp": issued + REPLY_MAX_LIFETIME, "jti": new_jti()}
    if error:
        claims["error"] = str(error)
    return sign(claims, client_key, headers={"kid": public_jwk(client_key.public_key())["kid"],
                                             "typ": REPLY_TYP})


_default_keys: ClientKeyResolver | None = None
_default_replays = MemoryJtiStore()


def _keys() -> ClientKeyResolver:
    global _default_keys
    if _default_keys is None:
        _default_keys = ClientKeyResolver()
    return _default_keys


def verify_reply(token, *, config, audience: str | None = None,
                 client_keys: ClientKeyResolver | None = None, replay_store=None) -> Reply:
    """Verify a reply canopy POSTed to this host. ``config`` is the host's ``HostConfig``
    (its ``canopy_client_id`` names the canopy it trusts, its ``app_name`` itself);
    ``audience`` is the URL the reply was POSTed to (default ``config.reply_url``).
    ``client_keys`` (default: one per process) reads canopy's keys from its client
    metadata; ``replay_store`` (default: in memory, per process) spends the ``jti``.
    Returns the ``Reply`` or raises ``ReplyRefused``."""
    if isinstance(token, bytes):
        token = token.decode("ascii", errors="replace")
    token = str(token or "").strip()
    if not token or len(token) > MAX_REPLY_TOKEN_BYTES:
        raise ReplyRefused("malformed", "not a compact JWS of acceptable size")
    client_id = getattr(config, "canopy_client_id", "")
    site = getattr(config, "app_name", "")
    aud = audience or getattr(config, "reply_url", "")
    if not client_id or not site or not aud:
        raise ReplyRefused("not_configured",
                           "this host trusts no canopy for replies (canopy_client_id, app_name, reply_url)")
    try:
        header = _header(token)
        if header.get("typ") != REPLY_TYP:
            raise ContractError("bad_typ", "not a canopy reply")
        jwk = (client_keys or _keys()).key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(token, key, algorithms=[header["alg"]], audience=[aud],
                        required=REPLY_REQUIRED_CLAIMS, leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, REPLY_MAX_LIFETIME, what="a reply")
        jti = contract.check_jti(claims)
    except MetadataError as exc:
        log.warning("canopy client metadata unusable: %s", exc)
        raise ReplyRefused("client_metadata", "canopy's keys could not be read") from exc
    except ContractError as exc:
        raise ReplyRefused(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise ReplyRefused("bad_issuer", "the reply is not from the canopy this host trusts")
    if not constant_time_equal(str(claims.get("site") or ""), site):
        raise ReplyRefused("wrong_site", "the reply is for another host site")
    seq = claims.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 \
            or not isinstance(claims.get("ended"), bool) or not isinstance(claims.get("text"), str):
        raise ReplyRefused("malformed", "seq, ended and text must be a number, a boolean and a string")
    store = replay_store if replay_store is not None else _default_replays
    try:
        store.consume([(f"reply:{client_id}", jti, int(claims["exp"]) + contract.LEEWAY_SECONDS)])
    except Replayed as exc:
        raise ReplyRefused("replayed", "this reply has already been used") from exc
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer fails CLOSED
        log.warning("jti store failed; refusing the reply", exc_info=True)
        raise ReplyRefused("jti_store_error", "the reply could not be recorded as used") from exc
    return Reply(site=str(claims["site"]), sub=str(claims["sub"]), session=str(claims["session"]),
                 turn=str(claims["turn"]), seq=int(seq), text=claims["text"], ended=claims["ended"],
                 error=str(claims.get("error") or ""), claims=dict(claims))


def _header(token: str) -> Mapping:
    # `unverified_header` bounds a JWS at `contract.MAX_JWT_BYTES`; a reply carries a
    # turn's prose and is bounded by the caller instead.
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise ContractError("malformed", "not a compact JWS") from exc
    if header.get("alg") not in contract.GRANT_ALGORITHMS:
        raise ContractError("bad_alg", f"algorithm {header.get('alg')!r} is not accepted")
    return header


__all__ = ["REPLY_CONTENT_TYPE", "REPLY_MAX_LIFETIME", "REPLY_MODES", "REPLY_TYP", "Reply",
           "ReplyRefused", "sign_reply", "verify_reply"]
