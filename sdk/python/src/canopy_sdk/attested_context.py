"""Context a host ATTESTS on one message: what the host knows that the agent should
read beside the person's words, signed by the host and kept apart from them.

A relay host (``canopy_sdk.host.relay``) sends the person's words as the message's
``text`` and, separately, what it knows about this moment as a compact JWS in the
send's ``context`` field: the person's current stage, the task they are on, the last
thing the host told them. canopy checks the signature against the site's own keys,
keeps the context on that turn only, and hands it to the agent as a labelled block
(the envelope's ``host_context``, ``attested: true``), never inside the prompt, so a
person cannot write words that read as the host's and the host's facts never read
as the person's.

Both halves live here so the host and canopy read ONE definition:

* the host signs with ``sign_context`` (``typ: canopy-context+jwt``, its own key,
  ``iss`` = its host-site name, ``sub`` = the person's id at the host — the
  ``sub`` it signed into their arrival —, ``session`` = canopy's id for the
  conversation, at most 60 seconds, a single-use ``jti``, ``format`` and ``body``);
* canopy checks with ``verify_context``.

The body is DATA, not instructions: canopy labels it so, and an agent must treat it
as the host's statement of fact, never as a request.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence

from . import contract
from .contract import ContractError, constant_time_equal
from .jose import new_jti, sign
from .keys import load_private_key, public_jwk

#: ``typ`` of the host's signed context: distinct from every other token either side
#: signs, so none can be replayed as another.
CONTEXT_TYP = "canopy-context+jwt"
#: Used once, at the moment of the send.
CONTEXT_MAX_LIFETIME = 60
#: The longest ``body``, in characters.
MAX_CONTEXT_BODY = 16_384
#: What the body is: Markdown for a model to read, or a JSON document.
FORMATS = frozenset({"markdown", "json"})
CONTEXT_REQUIRED_CLAIMS: tuple[str, ...] = (
    "iss", "sub", "session", "iat", "exp", "jti", "format", "body")
#: A signed context is larger than its body by the claims and the signature.
MAX_CONTEXT_TOKEN_BYTES = MAX_CONTEXT_BODY * 4 + 2048


class ContextError(Exception):
    """A context that is not one canopy keeps. ``code`` names why: ``mismatch`` (another
    person's or conversation's), ``replayed``, or a malformed/unverified token's code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _check_body(fmt, body) -> tuple[str, str]:
    if fmt not in FORMATS:
        raise ContextError("bad_format", f"format must be one of {', '.join(sorted(FORMATS))}")
    if not isinstance(body, str) or not body.strip():
        raise ContextError("bad_body", "the context needs a body")
    if len(body) > MAX_CONTEXT_BODY:
        raise ContextError("too_large", f"the body is limited to {MAX_CONTEXT_BODY} characters")
    if fmt == "json":
        try:
            json.loads(body)
        except ValueError as exc:
            raise ContextError("bad_body", "a json context's body must be a JSON document") from exc
    return fmt, body


def sign_context(site_name: str, site_key, *, sub: str, session: str, format: str, body: str,
                 now: float | None = None) -> str:
    """The host's signed context for ONE send into conversation ``session``, about the
    person the host knows as ``sub``. Raises ``ContextError`` on a body canopy would not
    keep (``bad_format``, ``bad_body``, ``too_large``)."""
    name = str(site_name or "").strip()
    if not name:
        raise ContextError("not_configured", "the context needs the site's host-site name")
    if not str(sub or "").strip() or not str(session or "").strip():
        raise ContextError("incomplete", "the context names the person (sub) and the conversation (session)")
    fmt, body = _check_body(format, body)
    key = load_private_key(site_key, what="the site signing key")
    issued = int(time.time() if now is None else now)
    claims = {"iss": name, "sub": str(sub).strip(), "session": str(session).strip(), "iat": issued,
              "exp": issued + CONTEXT_MAX_LIFETIME, "jti": new_jti(), "format": fmt, "body": body}
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": CONTEXT_TYP})


def verify_context(token: str, keys: Sequence, *, site: str, sub: str, session: str,
                   spend_jti: Callable[[dict], None] | None = None,
                   algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                   leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """canopy's check of a host's context against that host's ``keys``: OUR algorithm
    list, ``typ``, the signature, ``iss`` = ``site``, the life capped at 60s, a ``jti``,
    the body's bounds, then ``sub`` and ``session`` = the conversation's own
    (``ContextError("mismatch")`` otherwise), then the ``jti`` spent once
    (``spend_jti``). Returns ``{format, body, ...claims}``."""
    import jwt

    if not isinstance(token, str) or len(token) > MAX_CONTEXT_TOKEN_BYTES:
        raise ContextError("malformed", "not a compact JWS of acceptable size")
    try:
        header = _header(token, algorithms)
    except ContractError as exc:
        raise ContextError(exc.code, exc.message) from exc
    if header.get("typ") != CONTEXT_TYP:
        raise ContextError("bad_typ", f"a host context must have typ {CONTEXT_TYP}")
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms), leeway=leeway,
                                options={"require": list(CONTEXT_REQUIRED_CLAIMS), "verify_aud": False,
                                         "verify_exp": True})
            break
        except jwt.ExpiredSignatureError as exc:
            raise ContextError("expired", "the context has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise ContextError("incomplete", f"the context is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise ContextError("bad_signature", f"no key registered for this site verifies the context ({last})")
    try:
        contract.check_lifetime(claims, CONTEXT_MAX_LIFETIME, leeway=leeway, what="a host context")
        contract.check_jti(claims)
    except ContractError as exc:
        raise ContextError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), str(site or "")):
        raise ContextError("bad_issuer", "the context is not from this conversation's site")
    fmt, body = _check_body(claims.get("format"), claims.get("body"))
    if not constant_time_equal(str(claims.get("sub") or ""), str(sub or "")) \
            or not constant_time_equal(str(claims.get("session") or ""), str(session or "")):
        raise ContextError("mismatch", "the context is about another person or conversation")
    if spend_jti is not None:
        spend_jti(claims)
    return {**claims, "format": fmt, "body": body}


def _header(token: str, algorithms) -> dict:
    # `unverified_header` bounds a JWS at `contract.MAX_JWT_BYTES`, which a 16k body
    # outgrows; the size is bounded by the caller.
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise ContractError("malformed", "not a compact JWS") from exc
    if header.get("alg") not in algorithms:
        raise ContractError("bad_alg", f"algorithm {header.get('alg')!r} is not accepted")
    return header


__all__ = ["CONTEXT_MAX_LIFETIME", "CONTEXT_TYP", "ContextError", "FORMATS", "MAX_CONTEXT_BODY",
           "sign_context", "verify_context"]
